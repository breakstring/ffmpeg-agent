import asyncio
import hashlib
import json
import sqlite3
from contextlib import asynccontextmanager

import httpx
import pytest

from media_service.api import create_app
from media_service.database import SCHEMA, UPLOAD_SCHEMA, Database
from media_service.service import Service


@asynccontextmanager
async def running(settings):
    service = Service(settings)
    app = create_app(service=service)
    token = settings.callers["test"].tokens[0].get_secret_value()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client, service


def definition():
    return {"inputs": {"main": {"kind": "upload", "part": "main"}},
            "steps": [{"id": "p", "operation": "media.probe.v1",
                       "inputs": {"media": {"source": "main"}}}],
            "outputs": {"report": {"step": "p", "output": "report"}}}


async def done(client, job_id):
    async with asyncio.timeout(10):
        while True:
            response = await client.get(f"/v1/jobs/{job_id}")
            assert response.status_code == 200
            job = response.json()
            if job["inputs_released"]:
                return job
            await asyncio.sleep(0.01)


async def test_raw_upload_larger_than_json_limit_replay_and_authenticated_metadata(settings):
    data = b"isolated-media-bytes" * (settings.max_request_bytes // 10)
    digest = hashlib.sha256(data).hexdigest()
    headers = {"Content-Type": "application/octet-stream", "Idempotency-Key": "large-upload",
               "X-File-Size": str(len(data)), "X-File-Sha256": digest}
    async with running(settings) as (client, service):
        first = await client.post("/v1/files", content=data, headers=headers)
        assert first.status_code == 201, first.text
        item = first.json()
        assert item["sha256"] == digest and item["size_bytes"] == len(data)
        replay = await client.post("/v1/files", content=data, headers=headers)
        assert replay.status_code == 200 and replay.json()["file_id"] == item["file_id"]
        conflict = await client.post("/v1/files", content=b"other", headers={
            "Content-Type": "application/octet-stream", "Idempotency-Key": "large-upload"})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert (await client.get(f"/v1/files/{item['file_id']}",
                                 headers={"Authorization": ""})).status_code == 401
        released = await client.post(f"/v1/files/{item['file_id']}/release")
        assert released.json()["state"] in {"retiring", "deleted"}
        await service.uploads.gc()
        assert not (settings.uploads_dir / item["file_id"]).exists()


async def test_multipart_job_real_probe_and_replay_after_uploaded_file_deleted(settings):
    data = (settings.storages["input"].root / "audio_mp3_mono_22050.mp3").read_bytes()
    files = [("job", (None, json.dumps(definition()), "application/json")),
             ("main", ("source.mp3", data, "audio/mpeg"))]
    async with running(settings) as (client, service):
        accepted = await client.post("/v1/jobs/upload", files=files,
                                     headers={"Idempotency-Key": "combined"})
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["job_id"]
        job = await done(client, job_id)
        assert job["status"] == "succeeded", job
        artifact = job["outputs"]["report"]["artifact_id"]
        content = await client.get(f"/v1/artifacts/{artifact}/content")
        assert content.status_code == 200 and content.json()["type"] == "audio"
        row = await service.db.one("SELECT id FROM uploaded_files")
        await service.uploads.release("test", row["id"])
        await service.uploads.gc()
        assert not (settings.uploads_dir / row["id"]).exists()
        replay = await client.post("/v1/jobs/upload", files=files,
                                   headers={"Idempotency-Key": "combined"})
        assert replay.status_code == 200 and replay.json()["job_id"] == job_id
        changed = [("job", (None, json.dumps(definition()), "application/json")),
                   ("main", ("source.mp3", b"different", "audio/mpeg"))]
        conflict = await client.post("/v1/jobs/upload", files=changed,
                                     headers={"Idempotency-Key": "combined"})
        assert conflict.status_code == 409


@pytest.mark.parametrize("case", ["missing", "extra", "duplicate", "file_first", "bad_part"])
async def test_multipart_invalid_parts_never_accept_job(settings, case):
    body = definition()
    file = ("main", ("a.mp3", b"fixture", "audio/mpeg"))
    if case == "bad_part":
        body["inputs"]["main"]["part"] = "elsewhere"
    parts = [("job", (None, json.dumps(body), "application/json"))]
    parts += [] if case == "missing" else [file]
    if case == "extra":
        parts.append(("extra", ("a.mp3", b"fixture", "audio/mpeg")))
    elif case == "duplicate":
        parts.append(file)
    elif case == "file_first":
        parts.reverse()
    async with running(settings) as (client, service):
        result = await client.post("/v1/jobs/upload", files=parts,
                                   headers={"Idempotency-Key": case})
        assert result.status_code in {400, 422}, result.text
        assert await service.db.all("SELECT id FROM jobs") == []


async def test_legacy_schema_migration_preserves_records_and_is_idempotent(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    db = Database(root)
    connection = sqlite3.connect(root / "state.sqlite")
    connection.executescript(SCHEMA.removesuffix(UPLOAD_SCHEMA) + "PRAGMA user_version=1;")
    connection.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("preserved", "value"))
    connection.commit()
    connection.close()
    await db.open()
    assert await db.one("PRAGMA user_version") == {"user_version": 3}
    assert await db.one("SELECT value FROM meta WHERE key='preserved'") == {"value": "value"}
    assert await db.all("SELECT * FROM uploaded_files") == []
    await db.close()
    await db.open()
    assert await db.one("SELECT value FROM meta WHERE key='preserved'") == {"value": "value"}
    await db.close()


async def test_upload_idle_timeout_closes_partial_file_before_gc(settings):
    settings.upload_idle_seconds = 0.03

    async def slow_stream():
        yield b"partial"
        await asyncio.sleep(0.2)
        yield b"never-accepted"

    async with running(settings) as (client, service):
        result = await client.post("/v1/files", content=slow_stream(), headers={
            "Content-Type": "application/octet-stream", "Idempotency-Key": "idle-upload",
        })
        assert result.status_code == 408, result.text
        assert result.json()["error"]["code"] == "UPLOAD_TIMEOUT"
        rows = await service.db.all("SELECT id,state FROM uploaded_files")
        assert len(rows) == 1 and rows[0]["state"] in {"retiring", "deleted"}
        await service.uploads.gc()
        assert not (settings.uploads_dir / rows[0]["id"]).exists()
