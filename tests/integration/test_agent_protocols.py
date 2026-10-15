import asyncio
import hashlib
import json

import httpx
import pytest
from pydantic import SecretStr
from test_mcp_service import remote_session, serve

from media_service.agent_models import AgentRequest, MediaPlan, PlanningOutcome
from media_service.config import Caller
from media_service.service import Service

TERMINAL = {"succeeded", "partial", "failed", "canceled", "needs_input", "unsupported"}


class FakePlanner:
    def __init__(self, *, decision="execute", block=False):
        self.decision = decision
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_requested = asyncio.Event()
        self.stopped = asyncio.Event()
        if not block:
            self.release.set()
            self.stopped.set()

    async def plan(self, request, capabilities):
        assert isinstance(request, AgentRequest)
        assert "media.probe.v1" in capabilities["operations"]
        self.calls += 1
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancel_requested.set()
            await self.stopped.wait()
            raise
        if self.decision != "execute":
            return PlanningOutcome(decision=self.decision, message="Use a new complete request")
        return PlanningOutcome(decision="execute", plan=MediaPlan.model_validate({
            "steps": [{"id": "probe", "operation": "media.probe.v1",
                       "inputs": {"media": {"source": next(iter(request.inputs))}}}],
            "outputs": {"report": {"step": "probe", "output": "report"}},
        }))

    async def close(self):
        self.release.set()
        self.stopped.set()


def enable_agent(settings):
    settings.llm_enabled = True
    settings.llm_api_key = SecretStr("test-model-key-no-network-access")
    settings.llm_model = "test-offline-model"
    settings.llm_base_url = "https://must-not-connect.invalid/v1"


async def finished_agent(client, task_id):
    async with asyncio.timeout(15):
        while True:
            response = await client.get(f"/v1/agent/tasks/{task_id}")
            assert response.status_code == 200
            result = response.json()
            if result["status"] in TERMINAL:
                return result
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("authenticated", [False, True])
async def test_agent_real_http_mcp_upload_plan_job_download(settings, authenticated):
    enable_agent(settings)
    token = settings.callers["test"].tokens[0].get_secret_value()
    settings.service_token = SecretStr(token) if authenticated else None
    headers = {"Authorization": f"Bearer {token}"} if authenticated else {}
    planner = FakePlanner(block=True)
    service = Service(settings, planner=planner)
    async with serve(service) as base, httpx.AsyncClient(base_url=base, headers=headers) as client:
        if authenticated:
            for endpoint in ("/v1/agent/tasks", "/a2a", "/.well-known/agent-card.json"):
                denied = await client.post(endpoint, content=b"private-invalid-body",
                                          headers={"Authorization": "Bearer invalid"})
                assert denied.status_code == 401
        data = (settings.storages["input"].root / "audio_mp3_mono_22050.mp3").read_bytes()
        upload = await client.post("/v1/files", content=data, headers={
            "Content-Type": "application/octet-stream", "Idempotency-Key": "agent-upload",
            "X-File-Sha256": hashlib.sha256(data).hexdigest(),
        })
        assert upload.status_code == 201
        request = {"intent": "Inspect the audio and provide its properties",
                   "inputs": {"audio": {"kind": "uploaded_file", "file_id": upload.json()["file_id"]}}}
        submitted = await client.post("/v1/agent/tasks", json=request,
                                      headers={"Idempotency-Key": "shared-agent-key"})
        assert submitted.status_code == 202
        task_id = submitted.json()["agent_task_id"]
        assert submitted.json()["status"] in {"queued", "planning"}
        assert submitted.json()["job_id"] is None
        await asyncio.wait_for(planner.entered.wait(), 5)
        async with remote_session(base, headers) as session:
            listed = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert len(listed) == 22
            assert listed["generate_media"].annotations.open_world_hint is True
            assert listed["generate_media"].annotations.read_only_hint is False
            assert listed["get_agent_task"].annotations.read_only_hint is True
            assert listed["cancel_agent_task"].annotations.destructive_hint is True
            request_schema = listed["generate_media"].input_schema["$defs"]["AgentRequest"]
            assert set(request_schema["properties"]) == {"intent", "inputs", "output_policy"}
            assert request_schema["additionalProperties"] is False
            assert listed["generate_media"].output_schema["title"] == "AgentTaskResult"
            replayed = await session.call_tool("generate_media", {
                "request": request, "idempotency_key": "shared-agent-key",
            })
            assert not replayed.is_error
            assert replayed.structured_content["agent_task_id"] == task_id
            assert planner.calls == 1
            capabilities = await session.call_tool("list_capabilities", {})
            assert capabilities.structured_content["agent"]["enabled"] is True
            assert "must-not-connect" not in json.dumps(capabilities.structured_content)
            planner.release.set()
            final = await finished_agent(client, task_id)
            assert final["status"] == "succeeded" and final["decision"] == "execute"
            assert final["job"]["status"] == "succeeded"
            assert final["job"]["inputs_released"] is True
            queried = await session.call_tool("get_agent_task", {"agent_task_id": task_id})
            assert not queried.is_error and queried.structured_content == final
            artifact_id = final["job"]["outputs"]["report"]["artifact_id"]
            metadata = await session.call_tool("get_artifact", {"artifact_id": artifact_id})
            artifact = metadata.structured_content
            downloaded = await client.get(artifact["access"]["http"]["content"])
            assert downloaded.status_code == 200 and downloaded.json()["type"] == "audio"
            assert hashlib.sha256(downloaded.content).hexdigest() == artifact["sha256"]
            assert request["intent"] not in json.dumps(final)
            assert upload.json()["file_id"] not in json.dumps(final)
            assert planner.calls == 1


async def test_agent_disabled_keeps_structured_mcp_available(settings):
    settings.service_token = None
    service = Service(settings)
    async with serve(service) as base, remote_session(base, {}) as session:
        listed = {tool.name for tool in (await session.list_tools()).tools}
        assert len(listed) == 19
        assert not {"generate_media", "get_agent_task", "cancel_agent_task"} & listed
        async with httpx.AsyncClient(base_url=base) as client:
            response = await client.post("/v1/agent/tasks", json={
                "intent": "Inspect audio",
                "inputs": {"audio": {"kind": "shared_file", "storage_id": "input",
                                     "key": "audio_mp3_mono_22050.mp3"}},
            }, headers={"Idempotency-Key": "disabled"})
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "AGENT_DISABLED"
        unsupported = await session.call_tool("generate_media", {})
        assert unsupported.is_error
        assert unsupported.structured_content["error"]["code"] == "UNSUPPORTED_OPERATION"


@pytest.mark.parametrize("decision", ["needs_input", "unsupported"])
async def test_agent_nonexecuting_outcome_and_legacy_caller_isolation(settings, decision):
    enable_agent(settings)
    token = settings.callers["test"].tokens[0].get_secret_value()
    other_token = "isolated-other-agent-test-token"
    settings.callers["other"] = Caller(tokens=[SecretStr(other_token)],
                                        default_output_storage="output")
    settings.storages["output"].callers.append("other")
    service = Service(settings, planner=FakePlanner(decision=decision))
    headers = {"Authorization": f"Bearer {token}"}
    async with serve(service) as base, remote_session(base, headers) as session:
        accepted = await session.call_tool("generate_media", {
            "request": {"intent": "An ambiguous or unsupported operation",
                        "inputs": {"main": {"kind": "shared_file", "storage_id": "input",
                                            "key": "audio_mp3_mono_22050.mp3"}}},
            "idempotency_key": "decision-request",
        })
        assert not accepted.is_error
        task_id = accepted.structured_content["agent_task_id"]
        async with httpx.AsyncClient(base_url=base, headers=headers) as client:
            final = await finished_agent(client, task_id)
            assert final["status"] == decision and final["decision"] == decision
            assert final["job"] is None and final["job_id"] is None and final["plan"] is None
            denied = await client.get(f"/v1/agent/tasks/{task_id}",
                                      headers={"Authorization": f"Bearer {other_token}"})
            assert denied.status_code == 404
        async with remote_session(base, {"Authorization": f"Bearer {other_token}"}) as other:
            denied = await other.call_tool("cancel_agent_task", {"agent_task_id": task_id})
            assert denied.is_error and denied.structured_content["error"]["code"] == "NOT_FOUND"


async def test_agent_mcp_cancel_waits_for_actual_planner_stop(settings):
    enable_agent(settings)
    settings.service_token = None
    planner = FakePlanner(block=True)
    service = Service(settings, planner=planner)
    async with serve(service) as base, remote_session(base, {}) as session:
        result = await session.call_tool("generate_media", {
            "request": {"intent": "Inspect audio",
                        "inputs": {"main": {"kind": "shared_file", "storage_id": "input",
                                            "key": "audio_mp3_mono_22050.mp3"}}},
            "idempotency_key": "cancel-planning",
        })
        assert not result.is_error
        task_id = result.structured_content["agent_task_id"]
        await asyncio.wait_for(planner.entered.wait(), 5)
        cancel = asyncio.create_task(session.call_tool("cancel_agent_task", {"agent_task_id": task_id}))
        try:
            await asyncio.wait_for(planner.cancel_requested.wait(), 5)
            async with httpx.AsyncClient(base_url=base) as client:
                pending = await client.get(f"/v1/agent/tasks/{task_id}")
                assert pending.json()["status"] == "canceling"
                assert pending.json()["job_id"] is None
                planner.stopped.set()
                canceled = await finished_agent(client, task_id)
                assert canceled["status"] == "canceled" and canceled["job_id"] is None
            assert not (await cancel).is_error
        finally:
            planner.stopped.set()
            await asyncio.gather(cancel, return_exceptions=True)
