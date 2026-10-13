import asyncio
import hashlib
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from aiohttp import web

from media_service.api import create_app
from media_service.config import SourceRule
from media_service.models import JobCreate
from media_service.products import Product
from media_service.service import Service


@asynccontextmanager
async def serve(service):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
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
                    raise AssertionError("API exited before readiness")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}", port
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()


async def finished(client, job_id):
    async with asyncio.timeout(10):
        while True:
            result = await client.get(f"/v1/jobs/{job_id}")
            assert result.status_code == 200
            job = result.json()
            if job["inputs_released"]:
                return job
            await asyncio.sleep(0.01)


def probe(source):
    return {"inputs": {"main": source}, "steps": [{"id": "p", "operation": "media.probe.v1",
            "inputs": {"media": {"source": "main"}}}],
            "outputs": {"report": {"step": "p", "output": "report"}}}


@pytest.mark.asyncio
async def test_actual_tcp_http_source_job_range_and_secret_removal(settings):
    data = (settings.storages["input"].root / "222.mp3").read_bytes()
    seen = []

    async def source(request):
        seen.append(request.method)
        return web.Response(body=data)

    app = web.Application()
    app.router.add_route("*", "/audio", source)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    await web.SockSite(runner, listener).start()
    settings.callers["test"].source_rules = [SourceRule(host="127.0.0.1", ports=[port],
        schemes=["http"], networks=["127.0.0.1/32"])]
    service = Service(settings)
    headers = {"Authorization": "Bearer " + settings.callers["test"].tokens[0].get_secret_value()}
    try:
        async with serve(service) as (base, _), httpx.AsyncClient(base_url=base, headers=headers) as client:
            body = probe({"kind": "http_file", "url": f"http://127.0.0.1:{port}/audio?signature=test-sensitive-value",
                          "expected_size_bytes": len(data), "expected_sha256": hashlib.sha256(data).hexdigest()})
            accepted = await client.post("/v1/jobs", json=body, headers={"Idempotency-Key": "tcp-http"})
            assert accepted.status_code == 202, accepted.text
            job = await finished(client, accepted.json()["job_id"])
            assert job["status"] == "succeeded", job
            replay = await client.post("/v1/jobs", json=body, headers={"Idempotency-Key": "tcp-http"})
            assert replay.status_code == 200 and replay.json()["job_id"] == job["job_id"]
            assert seen == ["GET"]
            assert "signature" not in replay.text and "127.0.0.1" not in replay.text
            row = await service.db.one("SELECT definition FROM jobs WHERE id=?", (job["job_id"],))
            assert row["definition"] is None
            artifact_id = job["outputs"]["report"]["artifact_id"]
            uri = f"/v1/artifacts/{artifact_id}/content"
            full = await client.get(uri)
            assert full.status_code == 200 and full.json()["type"] == "audio"
            head = await client.head(uri)
            assert head.status_code == 200 and head.content == b""
            part = await client.get(uri, headers={"Range": "bytes=0-9", "If-Range": full.headers["etag"]})
            assert part.status_code == 206 and part.content == full.content[:10]
            async with asyncio.timeout(2):
                while await service.db.all("SELECT * FROM refs WHERE kind='send'"):  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_real_tcp_backpressure_disconnect_releases_only_after_send_stops(settings):
    # 64MiB 固定文件用于填满真实TCP窗口，不依赖媒体编码速度或假ASGI send。
    settings.gc_interval_seconds = 60
    service = Service(settings)
    async with serve(service) as (_, port):
        await service.scheduler.stop()
        request = JobCreate.model_validate(probe({"kind": "shared_file", "storage_id": "input", "key": "222.mp3"}))
        job_id, _ = await service.state.accept("test", "tcp-backpressure", request, service.engine)
        job = await service.state.claim()
        assert job is not None
        work = settings.work_dir / job["attempt_path"]
        work.mkdir(parents=True)
        with (work / "large.bin").open("wb") as file:
            file.truncate(64 * 1024**2)
        await service.state.step_start(job_id, job["current_attempt"], "p")
        artifact_id = await service.publisher.publish(job, "p", Product("file", {"type": "video"}, ["large.bin"]), work, True)
        await service.state.finish(job_id, job["current_attempt"], "succeeded")
        path = settings.storages["output"].root / artifact_id / "large.bin"
        client = socket.socket()
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        client.setblocking(False)
        loop = asyncio.get_running_loop()
        await loop.sock_connect(client, ("127.0.0.1", port))
        token = settings.callers["test"].tokens[0].get_secret_value()
        wire = f"GET /v1/artifacts/{artifact_id}/content HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\n\r\n"
        await loop.sock_sendall(client, wire.encode())
        try:
            head = await asyncio.wait_for(loop.sock_recv(client, 4096), 3)
            assert b"200 OK" in head
            await service.artifacts.release("test", artifact_id)
            await service.artifacts.gc()
            assert path.is_file()
            assert len(await service.db.all("SELECT * FROM refs WHERE kind='send'")) == 1
        finally:
            client.close()
        async with asyncio.timeout(3):
            while await service.db.all("SELECT * FROM refs WHERE kind='send'"):  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        await service.artifacts.gc()
        assert not path.exists()
