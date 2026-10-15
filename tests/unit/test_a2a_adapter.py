import copy
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from a2a.server.context import ServerCallContext
from a2a.types import a2a_pb2 as pb
from a2a.utils.errors import ContentTypeNotSupportedError, InvalidParamsError, TaskNotFoundError
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict
from pydantic import SecretStr

from media_service.a2a import (
    MediaCallContextBuilder,
    MediaRequestHandler,
    _request,
    agent_card,
    data_part,
    register_a2a,
)
from media_service.errors import ServiceError

TASK_ID = "agent_" + "a" * 32
ARTIFACT_ID = "art_" + "b" * 32
BODY = {"intent": "检查媒体属性", "inputs": {
    "source": {"kind": "uploaded_file", "file_id": "file_" + "c" * 32},
}}


def send_request(body=None, message_id="fixture-message"):
    return pb.SendMessageRequest(message=pb.Message(
        message_id=message_id, role=pb.ROLE_USER,
        parts=[data_part(body or BODY)],
    ))


class FakeTasks:
    def __init__(self, status="queued"):
        self.result = {"agent_task_id": TASK_ID, "status": status,
                       "created_at": "2026-10-16T00:00:00Z",
                       "planning_deadline_at": "2026-10-16T00:01:00Z"}
        self.submissions = []
        self.cancellations = []

    async def submit(self, caller, key, request):
        self.submissions.append((caller, key, request))
        return TASK_ID, len(self.submissions) == 1

    async def get(self, caller, task_id):
        if caller != "test" or task_id != TASK_ID:
            raise ServiceError("NOT_FOUND", 404)
        return self.result

    async def cancel(self, caller, task_id):
        self.cancellations.append((caller, task_id))
        return await self.get(caller, task_id)


class FakeArtifacts:
    def __init__(self, kind="file", gone=False):
        self.kind = kind
        self.gone = gone

    async def metadata(self, caller, artifact_id):
        if self.gone:
            raise ServiceError("ARTIFACT_GONE", 410)
        base = f"https://example.invalid/v1/artifacts/{artifact_id}"
        http = {"metadata": base}
        if self.kind == "file":
            http["content"] = base + "/content"
        else:
            http |= {"members": base + "/members", "files_base": base + "/files/"}
        return {"artifact_id": artifact_id, "kind": self.kind, "media": {
            "content_type": "audio/mpeg"}, "size_bytes": 123, "sha256": "d" * 64,
            "access": {"http": http, "shared": {"key": "private-location"}}}


def service_for(settings, status="queued", kind="file", gone=False):
    return SimpleNamespace(settings=settings, agent_tasks=FakeTasks(status),
                           artifacts=FakeArtifacts(kind, gone))


def test_agent_card_optional_and_legacy_auth(settings):
    assert agent_card(settings).security_requirements
    settings.service_token = None
    assert not agent_card(settings).security_requirements
    settings.service_token = SecretStr("isolated-a2a-fixture-token-12345")
    card = agent_card(settings)
    assert card.security_schemes["bearer"].http_auth_security_scheme.scheme == "bearer"
    assert card.security_requirements
    assert not card.capabilities.streaming and not card.capabilities.push_notifications
    assert card.supported_interfaces[0].protocol_version == "1.0"
    assert card.supported_interfaces[0].protocol_binding == "JSONRPC"


def test_message_formats_and_forbidden_sources():
    assert _request(send_request()).intent == BODY["intent"]
    request = send_request({"inputs": BODY["inputs"]})
    request.message.parts.insert(0, pb.Part(text=BODY["intent"]))
    assert _request(request).inputs == _request(send_request()).inputs
    request.message.parts.append(pb.Part(raw=b"private bytes"))
    with pytest.raises(ContentTypeNotSupportedError):
        _request(request)
    with pytest.raises(InvalidParamsError):
        _request(send_request(BODY | {"shell_command": "not-executable"}))
    with pytest.raises(InvalidParamsError):
        _request(send_request(BODY | {"inputs": {"s": {"kind": "path", "path": "/private"}}}))


def test_data_part_integer_limits_remain_usable():
    body = BODY | {"output_policy": {"retention_seconds": 60}}
    assert _request(send_request(body)).output_policy.retention_seconds == 60


@pytest.mark.parametrize("status,expected", [
    ("queued", pb.TASK_STATE_WORKING), ("planning", pb.TASK_STATE_WORKING),
    ("planned", pb.TASK_STATE_WORKING), ("running", pb.TASK_STATE_WORKING),
    ("canceling", pb.TASK_STATE_WORKING), ("succeeded", pb.TASK_STATE_COMPLETED),
    ("partial", pb.TASK_STATE_FAILED), ("failed", pb.TASK_STATE_FAILED),
    ("canceled", pb.TASK_STATE_CANCELED), ("needs_input", pb.TASK_STATE_REJECTED),
    ("unsupported", pb.TASK_STATE_REJECTED),
])
async def test_projects_actual_status(settings, status, expected):
    service = service_for(settings, status)
    handler = MediaRequestHandler(service)
    task = await handler.on_get_task(pb.GetTaskRequest(id=TASK_ID),
                                     ServerCallContext(state={"caller": "test"}))
    assert task.status.state == expected
    if status in {"needs_input", "unsupported"}:
        assert "messageId" in task.status.message.parts[0].text
    if status == "partial":
        assert "部分" in task.status.message.parts[0].text


async def test_canceling_is_not_canceled(settings):
    service = service_for(settings, "canceling")
    handler = MediaRequestHandler(service)
    context = ServerCallContext(state={"caller": "test"})
    task = await handler.on_cancel_task(pb.CancelTaskRequest(id=TASK_ID), context)
    assert task.status.state == pb.TASK_STATE_WORKING
    service.agent_tasks.result["status"] = "canceled"
    task = await handler.on_cancel_task(pb.CancelTaskRequest(id=TASK_ID), context)
    assert task.status.state == pb.TASK_STATE_CANCELED


async def test_replay_uses_stable_key_and_trusted_caller(settings):
    service = service_for(settings)
    handler = MediaRequestHandler(service)
    request = send_request()
    request.message.metadata.update({"caller": "attacker"})
    context = ServerCallContext(state={"caller": "test"})
    first = await handler.on_message_send(request, context)
    second = await handler.on_message_send(request, context)
    assert first.id == second.id
    submissions = service.agent_tasks.submissions
    assert submissions[0][0] == submissions[1][0] == "test"
    assert submissions[0][1] == submissions[1][1]
    assert submissions[0][1].startswith("a2a:")
    with pytest.raises(TaskNotFoundError):
        await handler.on_get_task(pb.GetTaskRequest(id=TASK_ID),
                                  ServerCallContext(state={"caller": "another"}))
    with pytest.raises(InvalidParamsError):
        await handler.on_message_send(request, ServerCallContext())


@pytest.mark.parametrize("kind,gone", [("file", False), ("collection", False), ("file", True)])
async def test_partial_keeps_successful_artifacts_and_collection_urls(settings, kind, gone):
    service = service_for(settings, "partial", kind, gone)
    service.agent_tasks.result["job"] = {"outputs": {"result": {
        "status": "succeeded", "artifact_id": ARTIFACT_ID,
    }}}
    handler = MediaRequestHandler(service)
    task = await handler.on_get_task(pb.GetTaskRequest(id=TASK_ID),
                                     ServerCallContext(state={"caller": "test"}))
    body = MessageToDict(task)
    assert task.status.state == pb.TASK_STATE_FAILED and len(task.artifacts) == 1
    assert "private-location" not in json.dumps(body)
    if gone:
        assert body["artifacts"][0]["parts"][0]["data"]["available"] is False
        assert task.metadata["job"]["outputs"]["result"]["artifact_id"] == ARTIFACT_ID
    elif kind == "file":
        assert task.artifacts[0].parts[1].url.endswith("/content")
    else:
        assert len(task.artifacts[0].parts) == 1
        access = body["artifacts"][0]["parts"][0]["data"]["access"]["http"]
        assert "members" in access and "content" not in access


def test_context_only_copies_trusted_caller_and_protocol_version():
    from starlette.requests import Request

    request = Request({"type": "http", "state": {"caller": "test"},
                       "headers": [(b"authorization", b"Bearer private"),
                                   (b"a2a-version", b"1.0")]})
    context = MediaCallContextBuilder().build(request)
    assert context.state == {"caller": "test", "headers": {"a2a-version": "1.0"}}


async def test_sdk_errors_and_logs_never_echo_raw_inputs(settings, caplog):
    settings.a2a_enabled = True
    app = FastAPI()
    service = service_for(settings)

    @app.middleware("http")
    async def trusted_caller(request, call_next):
        request.state.caller = "test"
        return await call_next(request)

    register_a2a(app, service)
    secret = "private-signed-url-and-token-never-echo"
    request = {"jsonrpc": "2.0", "id": "safe-id", "method": "SendMessage", "params": {
        "message": {"messageId": "safe-message", "role": "ROLE_USER", "parts": [
            {"data": copy.deepcopy(BODY)},
        ]},
    }}
    malformed = copy.deepcopy(request)
    malformed["params"]["message"]["parts"] = secret
    invalid = copy.deepcopy(request)
    invalid["params"]["message"]["parts"][0]["data"]["intent"] = {secret: secret}
    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                      base_url="http://test") as client:
            for body in (malformed, invalid):
                response = await client.post("/a2a", json=body, headers={"A2A-Version": "1.0"})
                assert response.json()["error"]["code"] == -32602
                assert secret not in response.text
            response = await client.post("/a2a", content="{" + secret,
                                         headers={"A2A-Version": "1.0"})
            assert response.json()["error"]["code"] == -32700
            assert secret not in response.text
    assert secret not in caplog.text


def test_disabled_routes_and_schema_export_need_no_running_service(settings):
    app = FastAPI()
    register_a2a(app, SimpleNamespace(settings=settings))
    assert "/a2a" not in app.openapi()["paths"]
    settings.a2a_enabled = True
    app = FastAPI()
    register_a2a(app, SimpleNamespace(settings=settings))
    schema = app.openapi()
    assert "/a2a" in schema["paths"]
    assert "/.well-known/agent-card.json" in schema["paths"]


@pytest.mark.parametrize("field,value", [
    ("task_id", TASK_ID), ("context_id", "existing-context"),
    ("reference_task_ids", [TASK_ID]), ("extensions", ["https://example.invalid/extension"]),
])
def test_task_continuation_and_references_are_explicitly_unsupported(field, value):
    from a2a.utils.errors import UnsupportedOperationError

    request = send_request()
    if isinstance(value, list):
        getattr(request.message, field).extend(value)
    else:
        setattr(request.message, field, value)
    with pytest.raises(UnsupportedOperationError):
        _request(request)


async def test_sdk_versions_and_unimplemented_operations_are_explicit(settings):
    settings.a2a_enabled = True
    app = FastAPI()

    @app.middleware("http")
    async def trusted_caller(request, call_next):
        request.state.caller = "test"
        return await call_next(request)

    register_a2a(app, service_for(settings))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        for method, expected in (
            ("ListTasks", -32004), ("SendStreamingMessage", -32004),
            ("SubscribeToTask", -32004), ("CreateTaskPushNotificationConfig", -32003),
            ("GetTaskPushNotificationConfig", -32003),
            ("ListTaskPushNotificationConfigs", -32003),
            ("DeleteTaskPushNotificationConfig", -32003), ("GetExtendedAgentCard", -32004),
            ("message/send", -32601),
        ):
            response = await client.post("/a2a", headers={"A2A-Version": "1.0"}, json={
                "jsonrpc": "2.0", "id": "safe-id", "method": method, "params": {},
            })
            assert response.json()["error"]["code"] == expected, (method, response.text)
        for headers in ({}, {"A2A-Version": "0.3"}, {"A2A-Version": "2.0"}):
            response = await client.post("/a2a", headers=headers, json={
                "jsonrpc": "2.0", "id": "safe-id", "method": "GetTask",
                "params": {"id": TASK_ID},
            })
            assert response.json()["error"]["code"] == -32009


async def test_arbitrary_service_exception_does_not_cross_protocol_boundary(settings):
    from a2a.utils.errors import InternalError

    class BrokenTasks(FakeTasks):
        async def get(self, caller, task_id):
            raise RuntimeError("never-echo-raw-provider-or-path")

    service = service_for(settings)
    service.agent_tasks = BrokenTasks()
    handler = MediaRequestHandler(service)
    with pytest.raises(InternalError) as error:
        await handler.on_get_task(pb.GetTaskRequest(id=TASK_ID),
                                  ServerCallContext(state={"caller": "test"}))
    assert str(error.value) == "Internal error" and error.value.data is None
