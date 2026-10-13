import asyncio
import hashlib
import json
import time

import httpx
import pytest

from media_service.api import create_app
from media_service.models import JobCreate
from media_service.service import Service


def probe_request(key="222.mp3"):
    return {
        "inputs": {"main": {"kind": "shared_file", "storage_id": "input", "key": key}},
        "steps": [
            {"id": "probe", "operation": "media.probe.v1", "inputs": {"media": {"source": "main"}}}
        ],
        "outputs": {"report": {"step": "probe", "output": "report"}},
    }


async def wait_job(service, job_id):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        job = await service.state.job("test", job_id)
        if job["inputs_released"]:
            return job
        await asyncio.sleep(0.02)
    raise AssertionError("job did not stop")


@pytest.mark.asyncio
async def test_real_probe_immutable_publication_and_restart(settings):
    service = Service(settings)
    await service.start()
    try:
        job_id, created = await service.accept(
            "test", "probe-key", JobCreate.model_validate(probe_request())
        )
        assert created
        job = await wait_job(service, job_id)
        assert job["status"] == "succeeded", job
        artifact_id = job["outputs"]["report"]["artifact_id"]
        artifact = await service.db.one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
        path = settings.storages["output"].root / artifact["directory"] / "report.json"
        data = path.read_bytes()
        report = json.loads(data)
        assert report["type"] == "audio"
        assert report["streams"][0]["sample_rate"] == 22050
        assert artifact["sha256"] == hashlib.sha256(data).hexdigest()
        assert str(settings.storages["input"].root) not in data.decode()
    finally:
        await service.close()
    restarted = Service(settings)
    await restarted.start()
    try:
        replay, created = await restarted.accept(
            "test", "probe-key", JobCreate.model_validate(probe_request())
        )
        assert replay == job_id and not created
        final = await restarted.state.job("test", job_id)
        assert final["outputs"]["report"]["artifact_id"] == artifact_id
        assert path.read_bytes() == data
        assert settings.storages["input"].root.joinpath("222.mp3").exists()
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_real_api_probe_and_invalid_media(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/health/ready")).status_code == 200
            assert (await client.get("/v1/capabilities")).status_code == 401
            headers = {
                "Authorization": "Bearer " + settings.callers["test"].tokens[0].get_secret_value(),
                "Idempotency-Key": "http-probe",
            }
            result = await client.post("/v1/jobs", json=probe_request(), headers=headers)
            assert result.status_code == 202, result.text
            job = await wait_job(app.state.service, result.json()["job_id"])
            assert job["status"] == "succeeded"
            headers["Idempotency-Key"] = "missing"
            result = await client.post(
                "/v1/jobs", json=probe_request("missing.mp4"), headers=headers
            )
            assert result.status_code == 202
            job = await wait_job(app.state.service, result.json()["job_id"])
            assert job["status"] == "failed"
            assert job["steps"][0]["error"]["code"] == "SOURCE_UNREACHABLE"
            assert job["inputs_released"]
