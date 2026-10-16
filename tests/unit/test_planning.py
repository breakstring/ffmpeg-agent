import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

import media_service.planning as planning
from media_service.agent_models import AgentRequest, PlanningOutcome
from media_service.errors import ServiceError
from media_service.local_env import load_local_env
from media_service.models import capabilities as operation_capabilities
from media_service.parameters import AUDIO_ENCODERS, MUXERS, VIDEO_ENCODERS
from media_service.planning import PydanticPlanner, _safe_capabilities
from media_service.planning_trace import current_planning_trace, planning_trace


def model_settings(settings, mode="prompted"):
    return settings.model_copy(update={
        "llm_enabled": True,
        "llm_api_key": SecretStr("isolated-model-api-key"),
        "llm_model": "isolated-compatible-model",
        "llm_base_url": "https://model.example.invalid/v1",
        "llm_output_mode": mode,
        "planning_timeout_seconds": 1,
        "planning_max_output_tokens": 1024,
    })


def media_request():
    return AgentRequest.model_validate({
        "intent": "将图片调整到 320 像素宽",
        "inputs": {"photo": {
            "kind": "http_file",
            "url": "https://media.example.invalid/private?signature=source-secret",
            "headers": {"authorization": "Bearer source-header-secret"},
        }},
        "output_policy": {"storage_id": "privateoutput", "retention_seconds": 12345},
    })


def executable(*, source="photo"):
    return {
        "decision": "execute", "message": "图片调整计划", "plan": {
            "steps": [{"id": "resize", "operation": "image.resize.v1",
                       "inputs": {"image": {"source": source}}, "params": {"width": 320}}],
            "outputs": {"resized": {"step": "resize", "output": "image"}},
        },
    }


def response(content):
    return httpx2.Response(200, json={
        "id": "isolated-completion", "object": "chat.completion", "created": 1,
        "model": "isolated-compatible-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    })


@asynccontextmanager
async def mock_planner(settings, replies, *, mode="prompted", wire_payloads=None):
    payloads = []

    async def respond(request):
        payloads.append(json.loads(request.content))
        if wire_payloads is not None:
            wire_payloads.append(request.content)
        assert len(payloads) <= len(replies), "planner exceeded expected request budget"
        reply = replies[len(payloads) - 1]
        if isinstance(reply, httpx2.Response):
            return reply
        return response(reply if isinstance(reply, str) else json.dumps(reply))

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as transport:
        client = AsyncOpenAI(
            api_key="isolated-model-api-key", base_url="https://model.example.invalid/v1",
            http_client=transport,
        )
        planner = PydanticPlanner(model_settings(settings, mode), client=client)
        try:
            yield planner, payloads
        finally:
            await planner.close()
            assert not transport.is_closed
            await client.close()


@pytest.mark.parametrize("mode,format_type", [("prompted", "json_object"),
                                              ("native", "json_schema")])
async def test_planner_uses_structured_output_without_tools_or_private_bindings(
        settings, mode, format_type):
    capabilities = {
        "operations": {"malicious": "ignored-operation-secret"},
        "limits": {"max_steps": 16, "provider_url": "ignored-limit-secret"},
        "engine": {"available_encoders": ["libx264", "/private/engine-path-secret"],
                   "available_muxers": ["mp4"], "ffmpeg": "/private/engine-path-secret"},
        "file_transfer": {"url": "https://private/transfer-secret"},
    }
    async with mock_planner(settings, [executable()], mode=mode) as (planner, payloads):
        result = await planner.plan(media_request(), capabilities)
    assert result.decision == "execute" and result.plan is not None
    bound = result.plan.bind(media_request())
    assert bound.steps[0].params["width"] == 320
    assert bound.output_policy.storage_id == "privateoutput"
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["response_format"]["type"] == format_type
    assert payload["max_tokens"] == 1024
    assert not ({"tools", "tool_choice", "enable_thinking", "thinking", "extra_body"} & payload.keys())
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "photo" in serialized and "image.resize.v1" in serialized
    for secret in ("isolated-model-api-key", "source-secret", "source-header-secret",
                   "privateoutput", "12345", "ignored-operation-secret", "ignored-limit-secret",
                   "engine-path-secret", "transfer-secret"):
        assert secret not in serialized


@pytest.mark.parametrize("timeout_seconds", [180, 300])
@pytest.mark.parametrize("injected", [False, True])
async def test_planner_applies_configured_timeout_to_actual_provider_request(
        settings, monkeypatch, timeout_seconds, injected):
    observed_timeouts = []

    async def respond(request):
        observed_timeouts.append(request.extensions["timeout"])
        return response(json.dumps(executable()))

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as transport:
        configured = model_settings(settings).model_copy(update={
            "planning_timeout_seconds": timeout_seconds,
        })
        client = None
        if injected:
            client = AsyncOpenAI(
                api_key="isolated-model-api-key", timeout=60, http_client=transport,
            )
        else:
            monkeypatch.setattr(
                planning, "AsyncOpenAI",
                lambda **options: AsyncOpenAI(**options, http_client=transport),
            )
        planner = PydanticPlanner(configured, client=client)
        try:
            assert (await planner.plan(media_request(), {})).decision == "execute"
            assert observed_timeouts == [{
                phase: timeout_seconds for phase in ("connect", "read", "write", "pool")
            }]
        finally:
            await planner.close()
            if client is not None:
                assert not transport.is_closed
                await client.close()


async def test_planner_only_sends_names_for_all_input_kinds(settings):
    request = AgentRequest.model_validate({
        "intent": "处理这些媒体",
        "inputs": {
            "shared": {"kind": "shared_file", "storage_id": "privatein", "key": "hidden/input.mov"},
            "http": {"kind": "http_file", "url": "https://private.invalid/signed-secret"},
            "upload": {"kind": "uploaded_file", "file_id": "file_" + "a" * 32},
            "artifact": {"kind": "artifact", "artifact_id": "art_" + "b" * 32,
                         "member_key": "hidden-member"},
        },
    })
    reply = {"decision": "needs_input", "message": "请描述所需的输出", "plan": None}
    async with mock_planner(settings, [reply]) as (planner, payloads):
        result = await planner.plan(request, {})
    assert result.decision == "needs_input"
    serialized = json.dumps(payloads)
    for secret in ("privatein", "hidden/input.mov", "signed-secret", "a" * 32,
                   "b" * 32, "hidden-member"):
        assert secret not in serialized
    assert all(name in serialized for name in request.inputs)


async def test_planner_preserves_operation_defaults_when_params_are_omitted(settings):
    reply = executable()
    reply["plan"]["steps"][0].pop("params")
    async with mock_planner(settings, [reply]) as (planner, _):
        result = await planner.plan(media_request(), {})
    assert result.plan is not None
    params = result.plan.steps[0].params
    assert params["width"] is None and params["height"] is None
    assert params["allow_upscale"] is False


def resolved_schema(node, definitions):
    if isinstance(node, list):
        return [resolved_schema(value, definitions) for value in node]
    if not isinstance(node, dict):
        return node
    result = {}
    if "$ref" in node:
        assert node["$ref"].startswith("#/$defs/")
        result = resolved_schema(definitions[node["$ref"][len("#/$defs/"):]], definitions)
    result.update({key: resolved_schema(value, definitions) for key, value in node.items()
                   if key not in {"$ref", "$defs", "title"}})
    return result


def test_model_capabilities_omit_only_schemas_already_present_in_output_contract():
    public = operation_capabilities()
    sent = _safe_capabilities({})["operations"]
    schema = PlanningOutcome.model_json_schema()
    branches = schema["$defs"]["Step"]["oneOf"]
    assert len(branches) == len(public) == len(sent) == 6
    for branch in branches:
        operation = branch["properties"]["operation"]["const"]
        assert sent[operation] == {key: value for key, value in public[operation].items()
                                   if key != "params_schema"}
        output_params = resolved_schema(branch["properties"]["params"], schema["$defs"])
        output_params.pop("default")
        published = public[operation]["params_schema"]
        assert output_params == resolved_schema(published, published.get("$defs", {}))


async def test_planner_reduces_actual_api_payload_without_changing_output_or_runtime(
        settings, monkeypatch):
    capabilities = {
        "limits": {"max_steps": 16, "max_pixels": 40000000, "max_frame_count": 100},
        "engine": {"available_encoders": list(VIDEO_ENCODERS.values()) + list(AUDIO_ENCODERS.values()),
                   "available_muxers": list(MUXERS.values())},
    }
    compact_capabilities = planning._safe_capabilities

    def repeated_capabilities(document):
        result = compact_capabilities(document)
        for name, operation in operation_capabilities().items():
            result["operations"][name]["params_schema"] = operation["params_schema"]
        return result

    baseline_wire = []
    with monkeypatch.context() as patch:
        patch.setattr(planning, "_safe_capabilities", repeated_capabilities)
        async with mock_planner(settings, [executable()], wire_payloads=baseline_wire) as (
                planner, baseline_payloads):
            baseline = await planner.plan(media_request(), capabilities)
    compact_wire = []
    async with mock_planner(settings, [executable()], wire_payloads=compact_wire) as (
            planner, compact_payloads):
        compact = await planner.plan(media_request(), capabilities)
    assert baseline == compact
    assert len(baseline_wire) == len(compact_wire) == 1
    assert len(compact_wire[0]) < len(baseline_wire[0]) * 0.65
    assert len(baseline_wire[0]) - len(compact_wire[0]) > 40000
    assert baseline_payloads[0]["messages"][:-1] == compact_payloads[0]["messages"][:-1]
    before = json.loads(baseline_payloads[0]["messages"][-1]["content"])
    after = json.loads(compact_payloads[0]["messages"][-1]["content"])
    for operation in before["capabilities"]["operations"].values():
        operation.pop("params_schema")
    assert before == after


@pytest.mark.parametrize("first", ["not valid JSON", {"decision": "execute"},
                                   executable(source="missing")])
async def test_planner_can_correct_one_structural_or_graph_error(settings, first):
    async with mock_planner(settings, [first, executable()]) as (planner, payloads):
        result = await planner.plan(media_request(), {})
    assert result.decision == "execute" and len(payloads) == 2
    assert "source-secret" not in json.dumps(payloads)
    assert "source-header-secret" not in json.dumps(payloads)


@pytest.mark.parametrize("replies", [
    ["not JSON", "still not JSON"],
    [executable(source="missing"), executable(source="missing")],
    ["not JSON", executable(source="missing")],
    [executable(source="missing"), "not JSON"],
])
async def test_planner_caps_all_corrections_at_two_model_requests(settings, replies):
    async with mock_planner(settings, replies) as (planner, payloads):
        with pytest.raises(ServiceError) as error:
            await planner.plan(media_request(), {})
    assert error.value.code == "PLANNING_INVALID_OUTPUT"
    assert error.value.status == 422 and not error.value.retryable
    assert len(payloads) == 2


@pytest.mark.parametrize("decision", ["needs_input", "unsupported"])
async def test_planner_returns_nonexecutable_outcome_without_retry(settings, decision):
    reply = {"decision": decision, "message": "缺少支持的信息", "plan": None}
    async with mock_planner(settings, [reply]) as (planner, payloads):
        result = await planner.plan(media_request(), {})
    assert result.decision == decision and result.plan is None and len(payloads) == 1


async def test_planner_does_not_retry_provider_errors_or_expose_error_body(settings, caplog):
    provider_error = httpx2.Response(503, json={"error": {
        "message": "private-provider-error-body", "type": "server_error", "code": "failed",
    }})
    with caplog.at_level(logging.DEBUG), planning_trace("agent_safe_test") as trace:
        with trace.stage("model_planning"):
            async with mock_planner(settings, [provider_error]) as (planner, payloads):
                with pytest.raises(ServiceError) as error:
                    await planner.plan(media_request(), {})
    assert len(payloads) == 1 and error.value.code == "PLANNING_FAILED"
    assert error.value.status == 502 and not error.value.retryable
    exposed = str(error.value) + caplog.text + json.dumps(error.value.public())
    for secret in ("private-provider-error-body", "source-secret", "source-header-secret",
                   "isolated-model-api-key", "isolated-compatible-model", "model.example.invalid"):
        assert secret not in exposed
    failure = [item for item in planning_events(caplog)
               if item["event"] == "planning_model_request" and item["status"] == "failed"]
    assert len(failure) == 1
    assert failure[0]["error_type"] == "provider_http"
    assert failure[0]["http_status"] == 503
    assert failure[0]["model_request"] == 1
    assert failure[0]["level"] == "WARNING"


def planning_events(caplog):
    return [json.loads(record.getMessage()) for record in caplog.records
            if record.name == "media_service.events"]


async def test_planning_trace_orders_model_correction_and_keeps_task_phase(settings, caplog):
    with caplog.at_level(logging.DEBUG), planning_trace("agent_safe_test") as trace:
        with trace.stage("model_planning"):
            async with mock_planner(settings, [executable(source="missing"), executable()]) as (
                    planner, payloads):
                outcome = await planner.plan(media_request(), {})
            assert trace.phase == "model_planning"
            assert trace.model_phase == "output_validate" and trace.model_request == 2
        with trace.stage("plan_validate"):
            outcome.plan.bind(media_request())
    assert current_planning_trace() is None
    events = planning_events(caplog)
    starts = [item for item in events if item["event"] == "planning_model_request"
              and item["status"] == "started"]
    assert [item["model_request"] for item in starts] == [1, 2]
    assert len(payloads) == 2
    stages = [item["model_phase"] for item in events
              if item["event"] == "planning_model_stage" and item["model_request"] == 2]
    assert stages == ["http_headers", "http_body", "response_parse", "output_validate"]
    assert any(item["event"] == "planning_output_validation" and item["status"] == "retry"
               for item in events)
    assert all(item["agent_task_id"] == "agent_safe_test" for item in events)
    assert all("request_id" not in item for item in events)
    for secret in ("source-secret", "source-header-secret", "isolated-model-api-key",
                   "isolated-compatible-model", "model.example.invalid", "320 像素"):
        assert secret not in caplog.text


@pytest.mark.parametrize("request_number", [1, 2])
@pytest.mark.parametrize("phase", ["http_headers", "http_body"])
async def test_planning_trace_preserves_waiting_phase_on_cancellation(
        settings, caplog, phase, request_number):
    started = asyncio.Event()
    stopped = asyncio.Event()
    calls = 0

    class BlockedBody(httpx2.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b"unreachable-private-response-body"

        async def aclose(self):
            stopped.set()

    async def respond(request):
        nonlocal calls
        calls += 1
        if calls < request_number:
            return response(json.dumps(executable(source="missing")))
        if phase == "http_body":
            return httpx2.Response(200, stream=BlockedBody())
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return response(json.dumps(executable()))

    with caplog.at_level(logging.DEBUG), planning_trace("agent_cancel_test") as trace:
        with trace.stage("model_planning"):
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as transport:
                original_hooks = {key: list(value) for key, value in transport.event_hooks.items()}
                client = AsyncOpenAI(api_key="isolated-key", http_client=transport)
                planner = PydanticPlanner(model_settings(settings), client=client)
                task = asyncio.create_task(planner.plan(media_request(), {}))
                await asyncio.wait_for(started.wait(), 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert stopped.is_set()
                assert trace.phase == "model_planning" and trace.model_phase == phase
                assert trace.model_request == calls == request_number
                await planner.close()
                assert not transport.is_closed and transport.event_hooks == original_hooks
                await client.close()
    cancelled = [item for item in planning_events(caplog)
                 if item["event"] == "planning_model_request" and item["status"] == "cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["model_phase"] == phase
    assert cancelled[0]["model_request"] == request_number
    assert cancelled[0]["error_type"] == "cancelled"
    assert cancelled[0]["level"] == "WARNING"
    assert "unreachable-private-response-body" not in caplog.text


@pytest.mark.parametrize("enabled,level,logged", [
    (False, logging.DEBUG, False), (True, logging.INFO, False), (True, logging.DEBUG, True),
])
async def test_planner_content_logging_requires_debug_and_switch_and_redacts(
        settings, caplog, enabled, level, logged):
    configured = model_settings(settings).model_copy(update={
        "log_model_content": enabled,
        "service_token": SecretStr("isolated-service-token"),
    })
    request = media_request().model_copy(update={
        "intent": "调整图片到 320 宽 isolated-model-api-key "
                  "https://private.invalid/input?signature=isolated-signed-secret",
    })
    reply = executable()
    reply["message"] = "准备调整 isolated-service-token " \
        "https://private.invalid/output?signature=response-secret"
    with caplog.at_level(level), planning_trace("agent_content_test"):
        async with mock_planner(configured, [reply]) as (planner, payloads):
            outcome = await planner.plan(request, {})
    assert outcome.decision == "execute" and len(payloads) == 1
    content_events = [item for item in planning_events(caplog) if item["event"].endswith("_content")]
    assert bool(content_events) is logged
    if logged:
        assert [item["event"] for item in content_events] == [
            "planning_model_request_content", "planning_model_response_content",
        ]
        assert "调整图片到 320 宽" in content_events[0]["content"]
        assert "准备调整" in content_events[1]["content"]
        assert all(item["model_request"] == 1 and item["level"] == "DEBUG"
                   for item in content_events)
        assert content_events[1]["http_status"] == 200
    for secret in ("isolated-model-api-key", "isolated-service-token", "private.invalid",
                   "isolated-signed-secret", "response-secret", "source-header-secret"):
        assert secret not in caplog.text


async def test_planner_redacts_http_failure_content_without_retry(settings, caplog):
    configured = model_settings(settings).model_copy(update={"log_model_content": True})
    reply = httpx2.Response(503, json={"error": {
        "message": "provider unavailable isolated-model-api-key "
                   "https://private.invalid/failed?signature=private-signature",
        "authorization": "Bearer private-header", "type": "server_error",
    }})
    with caplog.at_level(logging.DEBUG), planning_trace("agent_failure_content_test"):
        async with mock_planner(configured, [reply]) as (planner, payloads):
            with pytest.raises(ServiceError, match="PLANNING_FAILED"):
                await planner.plan(media_request(), {})
    assert len(payloads) == 1
    content = [item for item in planning_events(caplog)
               if item["event"] == "planning_model_response_content"]
    assert len(content) == 1 and content[0]["http_status"] == 503
    assert "provider unavailable" in content[0]["content"]
    for secret in ("isolated-model-api-key", "private.invalid", "private-signature", "private-header"):
        assert secret not in caplog.text


async def test_planner_sanitizes_unexpected_model_errors(settings):
    async def fail(messages, info):
        raise RuntimeError("private-model-runtime-error")

    planner = PydanticPlanner(model_settings(settings), model=FunctionModel(fail))
    with pytest.raises(ServiceError) as error:
        await planner.plan(media_request(), {})
    assert error.value.code == "PLANNING_FAILED"
    assert "private-model-runtime-error" not in str(error.value)
    await planner.close()


@pytest.mark.parametrize("timed_out", [False, True])
async def test_planner_propagates_cancellation_and_waits_for_transport_cleanup(settings, timed_out):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def blocked(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return response(json.dumps(executable()))

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(blocked)) as transport:
        client = AsyncOpenAI(api_key="isolated-key", http_client=transport)
        planner = PydanticPlanner(model_settings(settings), client=client)
        if timed_out:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.05):
                    await planner.plan(media_request(), {})
        else:
            task = asyncio.create_task(planner.plan(media_request(), {}))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert started.is_set() and stopped.is_set()
        await planner.close()
        await client.close()


async def test_planner_rejects_disabled_and_closed_states(settings):
    with pytest.raises(ServiceError) as disabled:
        PydanticPlanner(settings)
    assert disabled.value.code == "AGENT_DISABLED"

    async def unused(messages, info):
        raise AssertionError("closed planner must not invoke model")

    planner = PydanticPlanner(model_settings(settings), model=FunctionModel(unused))
    await planner.close()
    await planner.close()
    with pytest.raises(ServiceError) as closed:
        await planner.plan(media_request(), {})
    assert closed.value.code == "PLANNING_CLOSED"


async def test_planner_accepts_injected_model_without_network(settings):
    def generate(messages, info):
        assert info.output_tools == [] and info.function_tools == []
        return ModelResponse(parts=[TextPart(json.dumps(executable()))])

    planner = PydanticPlanner(model_settings(settings), model=FunctionModel(generate))
    assert (await planner.plan(media_request(), {})).decision == "execute"
    await planner.close()


def test_local_env_parses_legacy_and_canonical_fields_without_environment_mutation(tmp_path):
    path = tmp_path / ".env.local"
    path.write_text(
        "llm_provider=bailian\nllm_api_key='isolated-key'\n"
        "llm_default_model=legacy-model\nllm_base_url=https://legacy.invalid/v1\n"
        "MEDIA_LLM_MODEL=canonical-model\nMEDIA_LLM_ENABLED=true\n"
        "MEDIA_PLANNING_MAX_OUTPUT_TOKENS=2048\nIGNORED=value\n",
    )
    before = dict(os.environ)
    values = load_local_env(path, environ={})
    assert values == {
        "llm_api_key": SecretStr("isolated-key"), "llm_model": "canonical-model",
        "llm_base_url": "https://legacy.invalid/v1", "llm_enabled": True,
        "planning_max_output_tokens": 2048,
    }
    assert "isolated-key" not in repr(values)
    assert dict(os.environ) == before


def test_local_env_process_values_override_file_and_canonical_overrides_alias(tmp_path):
    path = tmp_path / ".env.local"
    path.write_text("MEDIA_LLM_MODEL=file-canonical\nMEDIA_LLM_API_KEY=file-key\n")
    values = load_local_env(path, environ={
        "llm_default_model": "process-legacy", "MEDIA_LLM_MODEL": "process-canonical",
        "llm_api_key": "process-key",
    })
    assert values["llm_model"] == "process-canonical"
    assert values["llm_api_key"].get_secret_value() == "process-key"


def test_local_env_validates_only_effective_values_after_precedence(tmp_path):
    path = tmp_path / ".env.local"
    path.write_text("MEDIA_PLANNING_CONCURRENCY=obsolete-invalid-value\n")
    assert load_local_env(path, environ={"MEDIA_PLANNING_CONCURRENCY": "3"}) == {
        "planning_concurrency": 3,
    }


def test_local_env_never_interpolates_or_executes_values(tmp_path):
    path = tmp_path / ".env.local"
    marker = tmp_path / "must-not-exist"
    path.write_text(f"MEDIA_LLM_API_KEY='${{ENV_SECRET}}'\nMEDIA_LLM_MODEL='$(touch {marker})'\n")
    values = load_local_env(path, environ={"ENV_SECRET": "secret-not-for-expansion"})
    assert values["llm_api_key"].get_secret_value() == "${ENV_SECRET}"
    assert values["llm_model"] == f"$(touch {marker})"
    assert not marker.exists()


@pytest.mark.parametrize("content", [
    "MEDIA_LLM_API_KEY='private-unclosed-secret\n",
    "MEDIA_PLANNING_CONCURRENCY=private-invalid-number\n",
    "MEDIA_PLANNING_CONCURRENCY=99\n",
])
def test_local_env_parse_failures_do_not_expose_values(tmp_path, caplog, content):
    path = tmp_path / "private-path-secret"
    path.write_text(content)
    with pytest.raises(ValueError) as error:
        load_local_env(path, environ={})
    exposed = str(error.value) + caplog.text
    assert "private-" not in exposed and "99" not in exposed


@pytest.mark.parametrize("content", [None, b"\xff", b"\0", b"x" * (256 * 1024 + 1)])
def test_local_env_invalid_files_fail_with_fixed_error(tmp_path, content):
    path = tmp_path / "private-missing-secret"
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(ValueError, match="^cannot read local environment configuration$"):
        load_local_env(path, environ={})
