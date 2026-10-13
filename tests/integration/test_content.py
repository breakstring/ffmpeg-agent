import asyncio
import time

import httpx
import pytest
from starlette.requests import ClientDisconnect

from media_service.api import create_app
from media_service.content import ArtifactResponse, byte_range
from media_service.errors import ServiceError
from media_service.models import JobCreate
from media_service.service import Service


@pytest.fixture
async def published(settings):
    service = Service(settings)
    await service.start()
    request = JobCreate.model_validate({
        "inputs": {"main": {"kind": "shared_file", "storage_id": "input", "key": "222.mp3"}},
        "steps": [{"id": "p", "operation": "media.probe.v1", "inputs": {"media": {"source": "main"}}}],
        "outputs": {"report": {"step": "p", "output": "report"}},
    })
    job_id, _ = await service.accept("test", "content-test", request)
    try:
        deadline = time.monotonic() + 10
        while True:
            job = await service.state.job("test", job_id)
            if job["inputs_released"]:
                break
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        assert job["status"] == "succeeded", job
        artifact_id = job["outputs"]["report"]["artifact_id"]
        metadata = await service.artifacts.metadata("test", artifact_id)
        path = settings.storages["output"].root / metadata["access"]["shared"]["key"]
        yield service, artifact_id, path
    finally:
        await service.close()


@pytest.mark.parametrize("header,expected", [
    (None, None), ("bytes=0-4", (0, 4)), ("bytes=3-", (3, 9)),
    ("bytes=-3", (7, 9)), ("bytes=-99", (0, 9)), ("bytes=0-99", (0, 9)),
    ("bytes=0-1,4-5", None), ("bytes=90-99,200-300", None),
])
def test_range_contract(header, expected):
    assert byte_range(header, 10) == expected


@pytest.mark.parametrize("header", ["bytes=", "bytes=-", "bytes=1-0", "bytes=10-", "bytes=-0", "words=0-1", "bytes=0-1,x-y", "bytes=0-1,", "bytes=" + "9" * 300 + "-"])
def test_invalid_ranges(header):
    with pytest.raises(ServiceError, match="INVALID_RANGE"):
        byte_range(header, 10)


@pytest.mark.asyncio
async def test_content_http_range_head_if_range_and_retiring_lease(published):
    service, artifact_id, path = published
    app = create_app(service=service)
    headers = {"Authorization": "Bearer " + service.settings.callers["test"].tokens[0].get_secret_value()}
    url = f"/v1/artifacts/{artifact_id}/content"
    data = path.read_bytes()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers) as client:
        full = await client.get(url)
        assert full.status_code == 200 and full.content == data
        head = await client.head(url)
        assert head.status_code == 200 and not head.content
        assert int(head.headers["content-length"]) == len(data)
        for value, expected in [("bytes=0-5", data[:6]), ("bytes=-7", data[-7:]), ("bytes=2-", data[2:])]:
            result = await client.get(url, headers={"Range": value})
            assert result.status_code == 206 and result.content == expected
        multi = await client.get(url, headers={"Range": "bytes=0-1,4-5"})
        assert multi.status_code == 200 and multi.content == data
        invalid = await client.get(url, headers={"Range": "bytes=999999-"})
        assert invalid.status_code == 416 and invalid.headers["content-range"] == f"bytes */{len(data)}"
        unchanged = await client.get(url, headers={"Range": "bytes=0-5", "If-Range": full.headers["etag"]})
        assert unchanged.status_code == 206
        changed = await client.get(url, headers={"Range": "bytes=0-5", "If-Range": '"different"'})
        assert changed.status_code == 200 and changed.content == data
        dated = await client.get(url, headers={"Range": "bytes=0-5", "If-Range": full.headers["last-modified"]})
        assert dated.status_code == 206
        lease = await service.artifacts.lease_create("test", artifact_id, 30)
        await service.artifacts.release("test", artifact_id)
        assert (await client.get(url)).status_code == 410
        assert (await client.get(url, headers={"X-Artifact-Lease-Id": lease["lease_id"]})).content == data
        assert not await service.db.all("SELECT * FROM refs WHERE kind='send'")


def scope():
    return {"type": "http", "method": "GET", "headers": [], "state": {"request_id": "test-request"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["disconnect", "cancel", "send_error", "timeout"])
async def test_actual_asgi_send_owns_reference_until_stopped(published, termination):
    service, artifact_id, path = published
    service.settings.http_send_seconds = 0.05 if termination == "timeout" else 5
    blocked = asyncio.Event()
    allow_send = asyncio.Event()
    disconnected = asyncio.Event()
    sender_stopped = asyncio.Event()

    async def send(message):
        if message["type"] == "http.response.body":
            blocked.set()
            try:
                await allow_send.wait()
                if termination == "send_error":
                    raise OSError("test transport stopped")
            finally:
                sender_stopped.set()

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    response = ArtifactResponse(service.artifacts, "test", artifact_id)
    task = asyncio.create_task(response(scope(), receive, send))
    await asyncio.wait_for(blocked.wait(), 2)
    await service.artifacts.release("test", artifact_id)
    await service.artifacts.gc()
    assert path.is_file()
    assert len(await service.db.all("SELECT * FROM refs WHERE kind='send'")) == 1
    if termination == "disconnect":
        disconnected.set()
    elif termination == "cancel":
        task.cancel()
    elif termination == "send_error":
        allow_send.set()
    try:
        await asyncio.wait_for(task, 2)
    except (asyncio.CancelledError, ClientDisconnect, OSError):
        pass
    assert sender_stopped.is_set()
    assert not await service.db.all("SELECT * FROM refs WHERE kind='send'")
    await service.artifacts.gc()
    assert not path.exists()
