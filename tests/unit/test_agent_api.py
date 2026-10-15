from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from media_service.agent_models import AgentRequest
from media_service.api import create_app
from media_service.errors import ServiceError

TASK_ID = "agent_" + "1" * 32


def request_body():
    return {
        "intent": "Inspect the uploaded media",
        "inputs": {"main": {"kind": "uploaded_file", "file_id": "file_" + "2" * 32}},
    }


class AgentTasks:
    def __init__(self):
        self.failed = False
        self.requests = {}
        self.result = {
            "agent_task_id": TASK_ID, "status": "queued",
            "created_at": "2026-10-16T00:00:00Z",
            "planning_deadline_at": "2026-10-16T00:01:00Z",
        }
        self.cancel_status = "canceling"

    async def submit(self, caller, key, request):
        assert isinstance(request, AgentRequest)
        identity = (caller, key)
        previous = self.requests.get(identity)
        if previous is not None and previous != request:
            raise ServiceError("IDEMPOTENCY_CONFLICT", 409)
        self.requests[identity] = request
        return TASK_ID, previous is None

    async def get(self, caller, task_id):
        if task_id != TASK_ID or caller != "test":
            raise ServiceError("NOT_FOUND", 404)
        return self.result

    async def cancel(self, caller, task_id):
        await self.get(caller, task_id)
        self.result = self.result | {"status": self.cancel_status}
        return self.result


def app_service(settings, *, enabled=True):
    settings.llm_enabled = enabled
    service = SimpleNamespace(
        settings=settings, ready=True, agent_tasks=AgentTasks(),
        engine={"test_executor": "agent-adapter-only"},
        scheduler=SimpleNamespace(failed=False), db=SimpleNamespace(healthy=True),
    )
    return create_app(service=service), service


def client_for(app, settings):
    token = settings.callers["test"].tokens[0].get_secret_value()
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test", headers={"Authorization": f"Bearer {token}"},
    )


async def test_agent_http_submit_replay_conflict_and_cancel(settings):
    app, service = app_service(settings)
    async with client_for(app, settings) as client:
        created = await client.post("/v1/agent/tasks", json=request_body(),
                                    headers={"Idempotency-Key": "same-request"})
        assert created.status_code == 202
        assert created.headers["Location"] == f"/v1/agent/tasks/{TASK_ID}"
        assert created.json()["agent_task_id"] == TASK_ID
        assert created.json()["job_id"] is None
        assert "intent" not in created.json() and "inputs" not in created.json()
        replay = await client.post("/v1/agent/tasks", json=request_body(),
                                   headers={"Idempotency-Key": "same-request"})
        assert replay.status_code == 200 and replay.json() == created.json()
        conflict = await client.post("/v1/agent/tasks", json=request_body() | {"intent": "different"},
                                     headers={"Idempotency-Key": "same-request"})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        queried = await client.get(created.headers["Location"])
        assert queried.json() == created.json()
        canceling = await client.post(created.headers["Location"] + "/cancel")
        assert canceling.status_code == 202 and canceling.json()["status"] == "canceling"
        service.agent_tasks.cancel_status = "canceled"
        canceled = await client.post(created.headers["Location"] + "/cancel")
        assert canceled.status_code == 200 and canceled.json()["status"] == "canceled"


async def test_agent_http_disabled_retains_queries_and_capabilities_are_safe(settings):
    settings.llm_api_key = SecretStr("never-expose-model-api-key")
    settings.llm_base_url = "https://never-expose-provider.invalid/v1"
    app, _ = app_service(settings, enabled=False)
    async with client_for(app, settings) as client:
        disabled = await client.post("/v1/agent/tasks", json=request_body(),
                                     headers={"Idempotency-Key": "disabled"})
        assert disabled.status_code == 503 and disabled.json()["error"]["code"] == "AGENT_DISABLED"
        assert (await client.get(f"/v1/agent/tasks/{TASK_ID}")).status_code == 200
        assert (await client.post(f"/v1/agent/tasks/{TASK_ID}/cancel")).status_code == 202
        capabilities = await client.get("/v1/capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json()["agent"]["enabled"] is False
        assert capabilities.json()["agent"]["mcp_tools"] == []
        assert "never-expose" not in capabilities.text
        assert len(capabilities.json()["operations"]) == 6


@pytest.mark.parametrize("path,method", [
    ("/v1/agent/tasks", "POST"), (f"/v1/agent/tasks/{TASK_ID}", "GET"),
    (f"/v1/agent/tasks/{TASK_ID}/cancel", "POST"),
    ("/a2a", "POST"), ("/a2a/", "POST"), ("/.well-known/agent-card.json", "GET"),
])
async def test_agent_and_a2a_auth_precedes_body_parsing(settings, path, method):
    app, _ = app_service(settings)
    async with client_for(app, settings) as client:
        denied = await client.request(method, path, content=b"malformed-private-body",
                                      headers={"Authorization": "Bearer invalid"})
        assert denied.status_code == 401
        assert denied.headers["www-authenticate"] == "Bearer"
        assert "malformed-private-body" not in denied.text


async def test_agent_validation_idempotency_and_health(settings):
    app, service = app_service(settings)
    async with client_for(app, settings) as client:
        for headers in ({}, [("Idempotency-Key", "a"), ("Idempotency-Key", "b")]):
            response = await client.post("/v1/agent/tasks", json=request_body(), headers=headers)
            assert response.status_code == 400
        invalid = await client.post("/v1/agent/tasks", json=request_body() | {"intent": " "},
                                    headers={"Idempotency-Key": "invalid"})
        assert invalid.status_code == 422
        private = "not-for-public-response"
        invalid = await client.post("/v1/agent/tasks", json=request_body() | {private: private},
                                    headers={"Idempotency-Key": "invalid"})
        assert invalid.status_code == 422 and private not in invalid.text
        service.agent_tasks.failed = True
        assert (await client.get("/health/ready")).status_code == 503
        service.agent_tasks.failed = False
        service.ready = False
        response = await client.post("/v1/agent/tasks", json=request_body(),
                                     headers={"Idempotency-Key": "not-ready"})
        assert response.status_code == 503 and response.json()["error"]["code"] == "STATE_UNAVAILABLE"


def test_agent_schema_export_needs_no_runtime_service(settings):
    app = create_app(service=SimpleNamespace(settings=settings))
    document = app.openapi()
    assert "/v1/agent/tasks" in document["paths"]
    assert "/v1/agent/tasks/{agent_task_id}" in document["paths"]
    assert "/v1/agent/tasks/{agent_task_id}/cancel" in document["paths"]
    models = document["components"]["schemas"]
    assert {"AgentRequest", "AgentTaskResult", "MediaPlan"} <= set(models)
    assert models["AgentRequest"]["additionalProperties"] is False
    assert set(models["AgentRequest"]["properties"]) == {"intent", "inputs", "output_policy"}
    assert models["AgentRequest"]["properties"]["intent"]["maxLength"] == 4000
