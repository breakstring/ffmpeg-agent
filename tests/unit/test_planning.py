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
    with caplog.at_level(logging.DEBUG):
        async with mock_planner(settings, [provider_error]) as (planner, payloads):
            with pytest.raises(ServiceError) as error:
                await planner.plan(media_request(), {})
    assert len(payloads) == 1 and error.value.code == "PLANNING_FAILED"
    assert error.value.status == 502 and not error.value.retryable
    exposed = str(error.value) + caplog.text + json.dumps(error.value.public())
    for secret in ("private-provider-error-body", "source-secret", "source-header-secret",
                   "isolated-model-api-key"):
        assert secret not in exposed


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
