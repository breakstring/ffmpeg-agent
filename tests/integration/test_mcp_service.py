import asyncio
import hashlib
import json
import socket
from contextlib import asynccontextmanager

import httpx
import httpx2
import pytest
import uvicorn
from mcp import ClientSession, MCPError, types
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr

from media_service.api import create_app
from media_service.config import Caller
from media_service.service import Service


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
                    raise AssertionError("MCP service exited before readiness")
                await asyncio.sleep(0.01)
        yield base
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()


@asynccontextmanager
async def remote_session(base, headers):
    async with httpx2.AsyncClient(headers=headers) as client:
        async with streamable_http_client(f"{base}/mcp", http_client=client) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=10) as session:
                initialized = await session.initialize()
                assert initialized.server_info.name == "ffmpeg-agent"
                yield session


async def finished(session, job_id):
    async with asyncio.timeout(15):
        while True:
            result = await session.call_tool("get_job", {"job_id": job_id})
            assert not result.is_error
            job = result.structured_content
            if job["inputs_released"]:
                return job
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("authenticated", [False, True])
async def test_remote_mcp_upload_job_download_and_optional_token(settings, authenticated):
    token = settings.callers["test"].tokens[0].get_secret_value()
    settings.service_token = SecretStr(token) if authenticated else None
    service = Service(settings)
    headers = {"Authorization": f"Bearer {token}"} if authenticated else {}
    async with serve(service) as base:
        async with httpx.AsyncClient(base_url=base, headers=headers) as client:
            if authenticated:
                unauthorized = await client.post("/mcp", content=b"not-json", headers={
                    "Authorization": "Bearer invalid", "Content-Type": "application/json"
                })
                assert unauthorized.status_code == 401
                for method, endpoint in (("GET", "/v1/capabilities"),
                                         ("POST", "/v1/files"),
                                         ("GET", "/v1/artifacts/art_" + "0" * 32 + "/content")):
                    denied = await client.request(method, endpoint, headers={
                        "Authorization": "Bearer invalid"
                    })
                    assert denied.status_code == 401
            data = (settings.storages["input"].root / "222.mp3").read_bytes()
            upload = await client.post("/v1/files", content=data, headers={
                "Content-Type": "application/octet-stream", "Idempotency-Key": "mcp-upload",
                "X-File-Sha256": hashlib.sha256(data).hexdigest(),
                "X-File-Size": str(len(data)),
            })
            assert upload.status_code == 201, upload.text
            file_id = upload.json()["file_id"]
            async with remote_session(base, headers) as session:
                listed = await session.list_tools()
                tools = {tool.name: tool for tool in listed.tools}
                assert len(tools) == 19
                for tool in tools.values():
                    assert tool.input_schema["type"] == "object"
                    assert tool.input_schema["additionalProperties"] is False
                    assert tool.output_schema["type"] == "object"
                assert tools["create_lease"].annotations.idempotent_hint is False
                assert tools["cancel_job"].annotations.destructive_hint is True
                source_schema = tools["probe_media"].input_schema["properties"]["source"]
                assert "uploaded_file" in json.dumps(source_schema)
                for name in ("probe_media", "transcode_video", "transcode_audio", "extract_audio",
                             "extract_frames", "resize_image"):
                    schema = json.dumps(tools[name].input_schema)
                    assert "ffmpeg_command" not in schema and "local_path" not in schema
                    assert "base64" not in schema and '"caller"' not in schema
                capabilities = await session.call_tool("list_capabilities", {})
                assert capabilities.structured_content["file_transfer"]["upload"] == base + "/v1/files"
                uploaded = await session.call_tool("get_uploaded_file", {"file_id": file_id})
                assert uploaded.structured_content["sha256"] == hashlib.sha256(data).hexdigest()
                arguments = {"source": {"kind": "uploaded_file", "file_id": file_id},
                             "idempotency_key": "mcp-probe", "params": {}}
                accepted = await session.call_tool("probe_media", arguments)
                assert not accepted.is_error, accepted
                assert accepted.structured_content["created"] is True
                job_id = accepted.structured_content["job_id"]
                replay = await session.call_tool("probe_media", arguments)
                assert replay.structured_content["job_id"] == job_id
                assert replay.structured_content["created"] is False
                job = await finished(session, job_id)
                assert job["status"] == "succeeded", job
                artifact_id = job["outputs"]["result"]["artifact_id"]
                metadata = await session.call_tool("get_artifact", {"artifact_id": artifact_id})
                assert not metadata.is_error
                artifact = metadata.structured_content
                downloaded = await client.get(artifact["access"]["http"]["content"])
                assert downloaded.status_code == 200 and downloaded.json()["type"] == "audio"
                assert hashlib.sha256(downloaded.content).hexdigest() == artifact["sha256"]
                lease = await session.call_tool("create_lease", {"artifact_id": artifact_id})
                lease_id = lease.structured_content["lease_id"]
                renewed = await session.call_tool("renew_lease", {"lease_id": lease_id})
                assert renewed.structured_content["lease_id"] == lease_id
                released_lease = await session.call_tool("release_lease", {"lease_id": lease_id})
                assert released_lease.structured_content["released"] is True
                extended = await session.call_tool("extend_artifact", {
                    "artifact_id": artifact_id, "retention_seconds": 100000,
                })
                assert not extended.is_error
                released_file = await session.call_tool("release_uploaded_file", {"file_id": file_id})
                assert released_file.structured_content["state"] == "retiring"
                released = await session.call_tool("release_artifact", {"artifact_id": artifact_id})
                assert released.structured_content["state"] == "retiring"


async def test_remote_mcp_errors_are_stable_and_do_not_expose_inputs(settings, caplog):
    settings.service_token = None
    service = Service(settings)
    secret = "never-emit-this-test-source-secret"
    async with serve(service) as base, remote_session(base, {}) as session:
        invalid = await session.call_tool("probe_media", {
            "source": {"kind": "uploaded_file", "file_id": secret},
            "idempotency_key": "invalid", "params": {},
        })
        assert invalid.is_error and invalid.structured_content["error"]["code"] == "INVALID_REQUEST"
        unknown = await session.call_tool(secret, {})
        assert unknown.is_error
        assert unknown.structured_content["error"]["code"] == "UNSUPPORTED_OPERATION"

        async def broken(*args):
            raise RuntimeError(secret)

        service.artifacts.metadata = broken
        unexpected = await session.call_tool("get_artifact", {"artifact_id": "art_" + "0" * 32})
        assert unexpected.is_error
        assert unexpected.structured_content["error"]["code"] == "STATE_UNAVAILABLE"
        async with httpx.AsyncClient(base_url=base) as client:
            malformed = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                "method": [secret]}, headers={"Content-Type": "application/json"})
            assert malformed.status_code == 400
            rejected_origin = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                "method": "ping"}, headers={"Origin": f"https://{secret}.invalid"})
            assert rejected_origin.status_code == 403
            rejected_host = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                "method": "ping"}, headers={"Host": f"{secret}.invalid"})
            assert rejected_host.status_code == 421
        for response in (invalid, unknown, unexpected):
            assert secret not in response.model_dump_json()
        assert secret not in malformed.text + rejected_origin.text + rejected_host.text
        assert secret not in caplog.text


async def test_remote_mcp_cancel_waits_for_input_release(settings):
    settings.service_token = None
    service = Service(settings)
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def checkpoint(point, artifact_id):
        if point == "intent":
            entered.set()
            await gate.wait()

    service.publisher.checkpoint_hook = checkpoint
    async with serve(service) as base, remote_session(base, {}) as session:
        accepted = await session.call_tool("submit_job", {
            "idempotency_key": "cancel-mcp",
            "definition": {
                "inputs": {"main": {"kind": "shared_file", "storage_id": "input",
                                    "key": "222.mp3"}},
                "steps": [{"id": "p", "operation": "media.probe.v1",
                           "inputs": {"media": {"source": "main"}}}],
                "outputs": {"report": {"step": "p", "output": "report"}},
            },
        })
        assert not accepted.is_error
        job_id = accepted.structured_content["job_id"]
        await asyncio.wait_for(entered.wait(), 5)
        canceled = await session.call_tool("cancel_job", {"job_id": job_id})
        assert not canceled.is_error
        final = await finished(session, job_id)
        assert final["status"] == "canceled" and final["inputs_released"]
        assert not service.executor.active_pids


async def test_remote_mcp_modern_discovery_and_legacy_caller_isolation(settings):
    first_token = settings.callers["test"].tokens[0].get_secret_value()
    second_token = "isolated-second-mcp-testing-token"
    settings.callers["second"] = Caller(tokens=[SecretStr(second_token)],
        default_output_storage="output")
    settings.storages["output"].callers.append("second")
    service = Service(settings)
    async with serve(service) as base:
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {first_token}"}) as client:
            async with streamable_http_client(f"{base}/mcp", http_client=client) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=10) as session:
                    discovery = await session.discover()
                    assert "2026-07-28" in discovery.supported_versions
                    listed = await session.list_tools()
                    assert len(listed.tools) == 19
                    accepted = await session.call_tool("probe_media", {
                        "source": {"kind": "shared_file", "storage_id": "input", "key": "222.mp3"},
                        "idempotency_key": "modern-caller", "params": {},
                    })
                    assert not accepted.is_error
                    job_id = accepted.structured_content["job_id"]
                    assert (await finished(session, job_id))["status"] == "succeeded"
                    client.headers["Authorization"] = f"Bearer {second_token}"
                    forbidden = await session.call_tool("get_job", {"job_id": job_id})
                    assert forbidden.is_error
                    assert forbidden.structured_content["error"]["code"] == "NOT_FOUND"
                    client.headers["Authorization"] = f"Bearer {first_token}"
                    allowed = await session.call_tool("get_job", {"job_id": job_id})
                    assert not allowed.is_error and allowed.structured_content["job_id"] == job_id


async def test_remote_mcp_protocol_errors_do_not_reflect_secret_parameters(settings, caplog):
    settings.service_token = None
    service = Service(settings)
    secret = "never-reflect-this-protocol-test-secret"
    bodies = [
        {"jsonrpc": "2.0", "id": 1, "method": secret},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": [secret]}},
        {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {"clientInfo": secret}},
    ]
    async with serve(service) as base, httpx.AsyncClient(base_url=base) as client:
        for body, code in zip(bodies, (-32601, -32602, -32602), strict=True):
            response = await client.post("/mcp", json=body, headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25",
            })
            assert response.status_code == 200
            assert response.json()["error"]["code"] == code
            assert "data" not in response.json()["error"]
            assert secret not in response.text
        duplicate_origin = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 4,
            "method": "ping"}, headers=[("Origin", base), ("Origin", f"https://{secret}.invalid"),
                ("Accept", "application/json, text/event-stream")])
        assert duplicate_origin.status_code == 400
        assert secret not in duplicate_origin.text
        # HTTP/1.1 客户端会先拒绝重复 Host；用 ASGI 入站验证服务自身的头部边界。
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service=service)), base_url=base,
        ) as raw_client:
            duplicate_host = await raw_client.post("/mcp", json={"jsonrpc": "2.0", "id": 5,
                "method": "ping"}, headers=[("Host", "localhost"), ("Host", secret)])
            assert duplicate_host.status_code == 400
            assert secret not in duplicate_host.text
        async with remote_session(base, {}) as session:
            with pytest.raises(MCPError) as unknown:
                await session.send_request(types.Request(method=secret, params=None), types.EmptyResult)
            assert unknown.value.error.code == -32601
            assert secret not in str(unknown.value)
            with pytest.raises(MCPError) as invalid_params:
                await session.send_request(types.Request[dict, str](method="tools/call", params={
                    "name": [secret]
                }), types.EmptyResult)
            assert invalid_params.value.error.code == -32602
            assert secret not in str(invalid_params.value)
    assert secret not in caplog.text
