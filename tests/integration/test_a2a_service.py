import asyncio
import hashlib
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import a2a_pb2 as pb
from a2a.utils.errors import InvalidParamsError, UnsupportedOperationError
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from pydantic import SecretStr

from media_service.agent_models import MediaPlan, PlanningOutcome
from media_service.api import create_app
from media_service.service import Service

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/a2a-workflow.py"


class FixturePlanner:
    def __init__(self, outcome=None, *, hold=False, hold_cleanup=False):
        self.outcome = outcome or PlanningOutcome(decision="execute", plan=MediaPlan(
            steps=[{"id": "probe", "operation": "media.probe.v1",
                    "inputs": {"media": {"source": "source"}}}],
            outputs={"result": {"step": "probe", "output": "report"}},
        ))
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.hold = hold
        self.hold_cleanup = hold_cleanup
        self.calls = 0

    async def plan(self, request, capabilities):
        self.calls += 1
        self.started.set()
        try:
            if self.hold:
                await self.release.wait()
            return self.outcome
        finally:
            if self.hold_cleanup:
                self.cleanup_started.set()
                await self.cleanup_release.wait()

    async def close(self):
        self.release.set()
        self.cleanup_release.set()


def enable_agent(settings, authenticated=False):
    settings.llm_enabled = True
    settings.llm_api_key = SecretStr("isolated-fixture-no-provider-access")
    settings.llm_model = "fixture"
    settings.llm_base_url = "https://example.invalid/v1"
    settings.a2a_enabled = True
    token = settings.callers["test"].tokens[0].get_secret_value()
    settings.service_token = SecretStr(token) if authenticated else None
    return {"Authorization": f"Bearer {token}"} if authenticated else {}


@asynccontextmanager
async def serve(service):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    service.settings.public_base_url = base
    server = uvicorn.Server(uvicorn.Config(
        create_app(service=service), log_config=None, access_log=False,
        timeout_graceful_shutdown=2,
    ))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError("A2A service exited before readiness")
                await asyncio.sleep(0.01)
        yield base
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()


async def remote_client(http, base):
    card = await A2ACardResolver(http, base).get_agent_card()
    assert card.name == "ffmpeg-agent"
    assert card.supported_interfaces[0].protocol_version == "1.0"
    assert card.supported_interfaces[0].url == base + "/a2a"
    assert not card.capabilities.streaming and not card.capabilities.push_notifications
    client = ClientFactory(ClientConfig(
        httpx_client=http, streaming=False, polling=True,
        supported_protocol_bindings=["JSONRPC"],
    )).create(card)
    return card, client


async def upload(http, settings, name="audio_mp3_mono_22050.mp3"):
    data = (settings.storages["input"].root / name).read_bytes()
    response = await http.post("/v1/files", content=data, headers={
        "Content-Type": "application/octet-stream", "Idempotency-Key": "a2a-upload",
        "X-File-Size": str(len(data)), "X-File-Sha256": hashlib.sha256(data).hexdigest(),
    })
    assert response.status_code == 201, response.text
    return response.json()["file_id"]


def message(file_id, message_id="a2a-fixture-message", intent="检查媒体属性"):
    body = {"intent": intent, "inputs": {
        "source": {"kind": "uploaded_file", "file_id": file_id},
    }}
    return pb.SendMessageRequest(message=pb.Message(
        message_id=message_id, role=pb.ROLE_USER,
        parts=[pb.Part(data=ParseDict(body, Value()))],
    ))


async def send(client, request):
    responses = [response async for response in client.send_message(request)]
    assert len(responses) == 1 and responses[0].HasField("task")
    return responses[0].task


async def finished(client, task_id):
    async with asyncio.timeout(20):
        while True:
            task = await client.get_task(pb.GetTaskRequest(id=task_id))
            if task.status.state in {
                pb.TASK_STATE_COMPLETED, pb.TASK_STATE_FAILED, pb.TASK_STATE_CANCELED,
                pb.TASK_STATE_REJECTED,
            }:
                return task
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("authenticated", [False, True])
async def test_official_a2a_client_upload_replay_execute_download(settings, authenticated):
    headers = enable_agent(settings, authenticated)
    planner = FixturePlanner(hold=True)
    service = Service(settings, planner=planner)
    async with serve(service) as base, httpx.AsyncClient(base_url=base, headers=headers) as http:
        if authenticated:
            for method, path in (("GET", "/.well-known/agent-card.json"), ("POST", "/a2a")):
                denied = await http.request(method, path, headers={"Authorization": "Bearer invalid"})
                assert denied.status_code == 401
        card, client = await remote_client(http, base)
        assert bool(card.security_requirements) == authenticated
        file_id = await upload(http, settings)
        accepted = await send(client, message(file_id))
        assert accepted.status.state == pb.TASK_STATE_WORKING
        await asyncio.wait_for(planner.started.wait(), 5)
        replay = await send(client, message(file_id))
        assert replay.id == accepted.id and planner.calls == 1
        with pytest.raises(InvalidParamsError) as conflict:
            await send(client, message(file_id, intent="不同描述"))
        assert conflict.value.data["code"] == "IDEMPOTENCY_CONFLICT"
        with pytest.raises(UnsupportedOperationError):
            await client.list_tasks(pb.ListTasksRequest())
        planner.release.set()
        result = await finished(client, accepted.id)
        assert result.status.state == pb.TASK_STATE_COMPLETED
        assert result.metadata["status"] == "succeeded" and result.metadata["job_id"]
        assert "intent" not in MessageToDict(result.metadata)
        artifact = MessageToDict(result.artifacts[0].parts[0].data)
        response = await http.get(result.artifacts[0].parts[1].url)
        assert response.status_code == 200 and response.json()["type"] == "audio"
        assert hashlib.sha256(response.content).hexdigest() == artifact["sha256"]
        if authenticated:
            denied = await http.get(result.artifacts[0].parts[1].url,
                                    headers={"Authorization": "Bearer invalid"})
            assert denied.status_code == 401
        replay = await send(client, message(file_id))
        assert replay.id == result.id and planner.calls == 1


async def test_official_a2a_cancel_waits_for_planner_cleanup(settings):
    headers = enable_agent(settings)
    planner = FixturePlanner(hold=True, hold_cleanup=True)
    service = Service(settings, planner=planner)
    async with serve(service) as base, httpx.AsyncClient(base_url=base, headers=headers) as http:
        _, client = await remote_client(http, base)
        file_id = await upload(http, settings)
        accepted = await send(client, message(file_id))
        await asyncio.wait_for(planner.started.wait(), 5)
        try:
            canceling = await client.cancel_task(pb.CancelTaskRequest(id=accepted.id))
            await asyncio.wait_for(planner.cleanup_started.wait(), 5)
            assert canceling.status.state == pb.TASK_STATE_WORKING
            current = await client.get_task(pb.GetTaskRequest(id=accepted.id))
            assert current.metadata["status"] == "canceling"
            assert current.status.state == pb.TASK_STATE_WORKING
        finally:
            planner.cleanup_release.set()
        canceled = await finished(client, accepted.id)
        assert canceled.status.state == pb.TASK_STATE_CANCELED
        assert canceled.metadata["job_id"] is None


async def test_official_a2a_partial_collection_retains_member_and_failure(settings):
    headers = enable_agent(settings)
    planner = FixturePlanner(PlanningOutcome(decision="execute", plan=MediaPlan(
        steps=[{"id": "frames", "operation": "video.extract_frames.v1", "inputs": {
            "video": {"source": "source"}}, "params": {
                "sampling": {"mode": "timestamps", "timestamps_ms": [0, 5000]},
            }}], outputs={"result": {"step": "frames", "output": "frames"}},
    )))
    service = Service(settings, planner=planner)
    async with serve(service) as base, httpx.AsyncClient(base_url=base, headers=headers) as http:
        _, client = await remote_client(http, base)
        file_id = await upload(http, settings, "video_h264_640x360.mkv")
        accepted = await send(client, message(file_id, intent="抽取两个时间点"))
        result = await finished(client, accepted.id)
        assert result.status.state == pb.TASK_STATE_FAILED
        assert result.metadata["status"] == "partial"
        assert "部分" in result.status.message.parts[0].text
        assert len(result.artifacts) == 1 and len(result.artifacts[0].parts) == 1
        artifact = MessageToDict(result.artifacts[0].parts[0].data)
        assert artifact["kind"] == "collection"
        http_access = artifact["access"]["http"]
        assert "content" not in http_access
        response = await http.get(http_access["members"])
        assert response.status_code == 200
        successful = [item for item in response.json()["items"] if not item.get("error")]
        failed = [item for item in response.json()["items"] if item.get("error")]
        assert len(successful) == len(failed) == 1
        assert failed[0]["error"]["code"] == "TIMESTAMP_OUT_OF_RANGE"
        member = successful[0]
        response = await http.get(http_access["files_base"] + member["member_key"])
        assert response.status_code == 200
        assert hashlib.sha256(response.content).hexdigest() == member["sha256"]


async def test_official_a2a_workflow_example_downloads_verified_result(settings, tmp_path):
    import json
    import sys

    enable_agent(settings, authenticated=True)
    service = Service(settings, planner=FixturePlanner())
    async with serve(service) as base:
        config = tmp_path / "client-config.json"
        config.write_text(json.dumps({
            "public_base_url": base,
            "service_token": settings.service_token.get_secret_value(),
        }))
        intent = tmp_path / "intent.txt"
        intent.write_text("检查媒体属性", encoding="utf-8")
        destination = tmp_path / "downloaded"
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(EXAMPLE),
            str(settings.storages["input"].root / "audio_mp3_mono_22050.mp3"), str(destination),
            "--config", str(config), "--intent-file", str(intent), "--wait-seconds", "15",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
        assert process.returncode == 0, (stdout.decode(), stderr.decode())
        assert b"TASK_STATE_COMPLETED" in stdout
        assert json.loads((destination / "result").read_text())["type"] == "audio"
        assert settings.service_token.get_secret_value().encode() not in stdout + stderr
