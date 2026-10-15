import asyncio
import json
import shutil
import sqlite3
from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import SecretStr

from media_service.api import create_app
from media_service.artifacts import Artifacts
from media_service.config import Caller
from media_service.database import Database
from media_service.models import JobCreate
from media_service.products import Product
from media_service.publishing import Publisher
from media_service.scheduler import Scheduler
from media_service.state import State


def request_body() -> dict:
    return {
        "inputs": {"main": {"kind": "shared_file", "storage_id": "input", "key": "audio_mp3_mono_22050.mp3"}},
        "steps": [
            {
                "id": "probe",
                "operation": "media.probe.v1",
                "inputs": {"media": {"source": "main"}},
            }
        ],
        "outputs": {"report": {"step": "probe", "output": "report"}},
    }


class ControlService:
    def __init__(self, settings, *, execute: bool = False):
        self.settings = settings
        self.db = Database(settings.state_dir)
        self.state = State(self.db, settings)
        self.artifacts = Artifacts(self.state)
        self.engine = {"test_executor": "control-only"}
        self.ready = False
        self.execute = execute
        self.entered = asyncio.Event()
        self.stopping = asyncio.Event()
        self.release = asyncio.Event()
        self.scheduler = Scheduler(self.state, self.run)

    async def run(self, job, cancel):
        self.entered.set()
        await cancel.wait()
        self.stopping.set()
        await self.release.wait()
        await self.state.finish(job["id"], job["current_attempt"], "canceled")

    async def start(self):
        await self.db.open()
        if self.execute:
            self.scheduler.start()
        self.ready = True

    async def close(self):
        self.ready = False
        self.release.set()
        await self.scheduler.stop()
        await self.db.close()

    async def accept(self, caller: str, key: str, request: JobCreate):
        accepted = await self.state.accept(caller, key, request, self.engine)
        self.scheduler.wake.set()
        return accepted


@asynccontextmanager
async def running(settings, *, execute: bool = False):
    service = ControlService(settings, execute=execute)
    app = create_app(service=service)
    token = settings.callers["test"].tokens[0].get_secret_value()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            yield client, service, app


async def submit(client, key="key", body=None):
    return await client.post(
        "/v1/jobs",
        json=request_body() if body is None else body,
        headers={"Idempotency-Key": key},
    )


def assert_error(response, status, code):
    assert response.status_code == status
    payload = response.json()["error"]
    assert payload["code"] == code
    assert payload["request_id"] == response.headers["x-request-id"]
    assert len(payload["request_id"]) == 32
    assert set(payload) == {"code", "message", "retryable", "request_id", "details"}
    assert payload["details"] is None


async def test_health_auth_and_caller_isolation(settings):
    other_token = "second-isolated-test-token-without-business-access"
    settings.callers["other"] = Caller(
        tokens=[SecretStr(other_token)], default_output_storage="output"
    )
    for storage in settings.storages.values():
        storage.callers.append("other")
    async with running(settings) as (client, service, _):
        assert (await client.get("/health/live", headers={"Authorization": ""})).status_code == 200
        assert (await client.get("/health/ready")).status_code == 200
        service.db.healthy = False
        assert_error(await client.get("/health/ready"), 503, "STATE_UNAVAILABLE")
        service.db.healthy = True
        service.scheduler.failed = True
        assert_error(await client.get("/health/ready"), 503, "STATE_UNAVAILABLE")
        service.scheduler.failed = False
        unauthorized = await client.get("/v1/capabilities", headers={"Authorization": ""})
        assert_error(unauthorized, 401, "UNAUTHORIZED")
        assert unauthorized.headers["www-authenticate"] == "Bearer"
        capabilities = await client.get("/v1/capabilities")
        assert capabilities.status_code == 200
        operations = capabilities.json()["operations"]
        assert len(operations) == 6
        assert {name.rsplit(".", 1)[0] for name in operations} == {
            "media.probe", "video.transcode", "audio.transcode", "video.extract_audio",
            "video.extract_frames", "image.resize",
        }
        assert capabilities.json()["limits"]["api_processes"] == 1
        created = await submit(client)
        job_id = created.json()["job_id"]
        other_headers = {"Authorization": f"Bearer {other_token}"}
        assert_error(
            await client.get(f"/v1/jobs/{job_id}", headers=other_headers), 404, "NOT_FOUND"
        )
        assert_error(
            await client.post(f"/v1/jobs/{job_id}/cancel", headers=other_headers), 404, "NOT_FOUND"
        )
        separate = await client.post(
            "/v1/jobs",
            json=request_body(),
            headers=other_headers | {"Idempotency-Key": "key"},
        )
        assert separate.status_code == 202 and separate.json()["job_id"] != job_id
        service.ready = False
        assert_error(await client.get("/health/ready"), 503, "STATE_UNAVAILABLE")
        assert_error(await submit(client, "not-ready"), 503, "STATE_UNAVAILABLE")


async def test_concurrent_idempotency_conflict_and_persistent_replay(settings):
    async with running(settings) as (client, _, _):
        results = await asyncio.gather(*(submit(client, "shared-key") for _ in range(16)))
        assert [result.status_code for result in results].count(202) == 1
        assert [result.status_code for result in results].count(200) == 15
        ids = {result.json()["job_id"] for result in results}
        assert len(ids) == 1
        job_id = ids.pop()
        assert all(result.headers["location"] == f"/v1/jobs/{job_id}" for result in results)
        different = request_body() | {"output_policy": {"retention_seconds": 100}}
        assert_error(await submit(client, "shared-key", different), 409, "IDEMPOTENCY_CONFLICT")
    async with running(settings) as (client, _, _):
        replay = await submit(client, "shared-key")
        assert replay.status_code == 200 and replay.json()["job_id"] == job_id
        assert (await client.get(f"/v1/jobs/{job_id}")).json()["status"] == "queued"


async def test_queue_limit_and_failed_persistence_never_return_accepted(settings):
    settings.max_queued_jobs = 1
    async with running(settings) as (client, service, _):
        assert (await submit(client, "one")).status_code == 202
        assert_error(await submit(client, "two"), 429, "QUOTA_EXCEEDED")
        assert (await submit(client, "one")).status_code == 200
        row = await service.db.one("SELECT count(*) n FROM jobs")
        assert row is not None and row["n"] == 1


async def test_sqlite_busy_and_statement_failure_are_sanitized_503(settings):
    async with running(settings) as (client, service, _):
        await service.db.run(lambda tx: tx.execute("PRAGMA busy_timeout=30"))
        blocker = sqlite3.connect(settings.state_dir / "state.sqlite", isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            assert_error(await submit(client, "locked"), 503, "STATE_UNAVAILABLE")
        finally:
            blocker.rollback()
            blocker.close()
        await service.db.run(
            lambda tx: tx.execute(
                "CREATE TRIGGER reject_job BEFORE INSERT ON jobs "
                "BEGIN SELECT RAISE(ABORT, 'must-not-leak-database-failure'); END"
            )
        )
        failed = await submit(client, "failed")
        assert_error(failed, 503, "STATE_UNAVAILABLE")
        assert "must-not-leak" not in failed.text
        assert await service.db.all("SELECT id FROM jobs") == []
        await service.db.run(lambda tx: tx.execute("DROP TRIGGER reject_job"))
        assert (await submit(client, "recovered")).status_code == 202


@pytest.mark.parametrize("key", ["", "x" * 129, "invalid\tkey", "invalid\x7fkey"])
async def test_invalid_idempotency_key_rejected(settings, key):
    async with running(settings) as (client, service, _):
        assert_error(await submit(client, key), 400, "INVALID_REQUEST")
        assert await service.db.all("SELECT id FROM jobs") == []


async def test_missing_or_duplicate_idempotency_and_duplicate_auth_rejected(settings):
    async with running(settings) as (client, _, _):
        assert_error(await client.post("/v1/jobs", json=request_body()), 400, "INVALID_REQUEST")
        duplicate = await client.post(
            "/v1/jobs",
            json=request_body(),
            headers=[("Idempotency-Key", "one"), ("Idempotency-Key", "two")],
        )
        assert_error(duplicate, 400, "INVALID_REQUEST")
        token = settings.callers["test"].tokens[0].get_secret_value()
        duplicate_auth = await client.get(
            "/v1/capabilities",
            headers=[("Authorization", f"Bearer {token}"), ("Authorization", f"Bearer {token}")],
        )
        assert_error(duplicate_auth, 401, "UNAUTHORIZED")


async def test_malformed_json_unknown_fields_and_private_source_never_echo(settings):
    marker = "private-source-marker-do-not-echo"
    async with running(settings) as (client, _, _):
        malformed = await client.post(
            "/v1/jobs",
            content=b'{"private":',
            headers={"Content-Type": "application/json", "Idempotency-Key": "malformed"},
        )
        assert_error(malformed, 400, "INVALID_REQUEST")
        assert "private" not in malformed.text
        invalid = await submit(client, "invalid", request_body() | {"secret": marker})
        assert_error(invalid, 422, "INVALID_REQUEST")
        assert marker not in invalid.text
        request = request_body()
        request["inputs"]["main"] = {
            "kind": "http_file",
            "url": f"https://source.invalid/file?signature={marker}",
            "headers": {"Authorization": marker},
        }
        accepted = await submit(client, "private-source", request)
        assert accepted.status_code == 202
        assert marker not in accepted.text and "signature" not in accepted.text
        assert marker not in (await client.get(accepted.headers["location"])).text


async def test_actual_chunked_body_limit_and_request_ids(settings):
    settings.max_request_bytes = 1024
    async with running(settings) as (client, service, _):
        oversized = await client.post("/v1/jobs", content=b"x" * 1025)
        assert_error(oversized, 413, "REQUEST_TOO_LARGE")
        large_header = await client.post(
            "/v1/jobs", content=b"", headers={"Content-Length": "9" * 5000}
        )
        assert_error(large_header, 413, "REQUEST_TOO_LARGE")

        async def chunks():
            yield b" " * 600
            yield b" " * 600

        chunked = await client.post(
            "/v1/jobs", content=chunks(), headers={"Content-Type": "application/json"}
        )
        assert_error(chunked, 413, "REQUEST_TOO_LARGE")
        assert await service.db.all("SELECT id FROM jobs") == []
        healthy = await client.get("/health/live", headers={"X-Request-ID": "untrusted-id"})
        assert len(healthy.headers["x-request-id"]) == 32
        assert healthy.headers["x-request-id"] != oversized.headers["x-request-id"]


async def test_cancel_persists_before_test_executor_is_released(settings):
    async with running(settings, execute=True) as (client, service, _):
        created = await submit(client)
        job_id = created.json()["job_id"]
        await asyncio.wait_for(service.entered.wait(), 2)
        cancel = await client.post(f"/v1/jobs/{job_id}/cancel")
        assert cancel.status_code == 202
        assert cancel.json()["status"] == "canceling"
        assert cancel.json()["inputs_released"] is False
        await asyncio.wait_for(service.stopping.wait(), 2)
        row = await service.db.one("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,))
        assert row is not None and row["cancel_requested"] == 1
        service.release.set()
        finished = created
        for _ in range(100):
            finished = await client.get(f"/v1/jobs/{job_id}")
            if finished.json()["status"] == "canceled":
                break
            await asyncio.sleep(0.01)
        assert finished.json()["inputs_released"] is True
        assert (await client.post(f"/v1/jobs/{job_id}/cancel")).status_code == 200


async def test_disconnected_incomplete_request_is_not_accepted(settings):
    async with running(settings) as (_, service, app):
        token = settings.callers["test"].tokens[0].get_secret_value()
        messages = iter(
            [
                {
                    "type": "http.request",
                    "body": json.dumps(request_body()).encode()[:30],
                    "more_body": True,
                },
                {"type": "http.disconnect"},
            ]
        )
        sent = []

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        await app(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/jobs",
                "raw_path": b"/v1/jobs",
                "query_string": b"",
                "scheme": "http",
                "http_version": "1.1",
                "headers": [
                    (b"authorization", f"Bearer {token}".encode()),
                    (b"idempotency-key", b"disconnected"),
                ],
            },
            receive,
            send,
        )
        assert sent == []
        assert await service.db.all("SELECT id FROM jobs") == []


async def publish_result(service, *, collection=False):
    body = request_body()
    if collection:
        body["inputs"]["main"]["key"] = "video_h264_aac_square.mp4"
        body["steps"] = [
            {
                "id": "frames",
                "operation": "video.extract_frames.v1",
                "inputs": {"video": {"source": "main"}},
            "params": {"sampling": {"mode": "timestamps", "timestamps_ms": [0, 100]}},
            }
        ]
        body["outputs"] = {"frames": {"step": "frames", "output": "frames"}}
    job_id, _ = await service.accept("test", "published", JobCreate.model_validate(body))
    job = await service.state.claim()
    assert job is not None and job["id"] == job_id
    step_id = "frames" if collection else "probe"
    await service.state.step_start(job_id, job["current_attempt"], step_id)
    source = service.settings.work_dir / job["attempt_path"] / "result"
    source.mkdir(parents=True)
    if collection:
        files = ["frame-0.jpg", "frame-1.jpg"]
        items = []
        for index, key in enumerate(files):
            shutil.copyfile(service.settings.storages["input"].root / "image_jpeg_portrait_odd.jpg", source / key)
            items.append(
                {
                    "index": index,
                    "member_key": key,
                    "requested_time_ms": index * 100,
                    "actual_time_ms": index * 100,
                }
            )
        product = Product("collection", {"type": "image"}, files, items)
    else:
        (source / "report.json").write_text('{"api-test":true}')
        product = Product("file", {"type": "json"}, ["report.json"])
    artifact_id = await Publisher(service.state).publish(job, step_id, product, source, True)
    await service.state.finish(job_id, job["current_attempt"], "succeeded")
    return artifact_id


async def test_artifact_metadata_retention_and_lease_routes(settings):
    async with running(settings) as (client, service, _):
        artifact_id = await publish_result(service)
        path = f"/v1/artifacts/{artifact_id}"
        metadata = await client.get(path)
        assert metadata.status_code == 200
        assert metadata.json()["kind"] == "file"
        assert metadata.json()["access"]["shared"]["storage_id"] == "output"
        invalid = await client.post(f"{path}/leases", json={"unknown": "must-not-be-echoed"})
        assert_error(invalid, 422, "INVALID_REQUEST")
        assert "must-not-be-echoed" not in invalid.text
        extended = await client.patch(f"{path}/retention", json={"retention_seconds": 90000})
        assert extended.status_code == 200
        assert extended.json()["retain_until"] > metadata.json()["retain_until"]
        lease = await client.post(f"{path}/leases", json={})
        assert lease.status_code == 201
        lease_id = lease.json()["lease_id"]
        renewed = await client.patch(f"/v1/leases/{lease_id}", json={"duration_seconds": 1000})
        assert renewed.status_code == 200 and renewed.json()["lease_id"] == lease_id
        released = await client.post(f"{path}/release")
        assert released.status_code == 200 and released.json()["state"] == "retiring"
        assert_error(await client.get(path), 410, "ARTIFACT_GONE")
        authorized = await client.get(path, headers={"X-Artifact-Lease-Id": lease_id})
        assert authorized.status_code == 200 and authorized.json()["state"] == "retiring"
        assert_error(await client.post(f"{path}/leases", json={}), 410, "ARTIFACT_GONE")
        assert_error(
            await client.patch(f"/v1/leases/{lease_id}", json={"duration_seconds": 1000}),
            410,
            "ARTIFACT_GONE",
        )
        deleted = await client.delete(f"/v1/leases/{lease_id}")
        assert deleted.status_code == 204 and deleted.content == b""
        assert "x-request-id" in deleted.headers
        assert (await client.delete(f"/v1/leases/{lease_id}")).status_code == 204
        assert_error(
            await client.get(path, headers={"X-Artifact-Lease-Id": lease_id}),
            410,
            "ARTIFACT_GONE",
        )


async def test_collection_page_order_and_retiring_access_with_lease(settings):
    async with running(settings) as (client, service, _):
        artifact_id = await publish_result(service, collection=True)
        path = f"/v1/artifacts/{artifact_id}"
        first = await client.get(f"{path}/members?limit=1")
        assert first.status_code == 200 and first.json()["next_cursor"] == 1
        assert first.json()["items"][0]["index"] == 0
        second = await client.get(f"{path}/members?cursor=1&limit=1")
        assert second.json()["items"][0]["index"] == 1
        assert second.json()["next_cursor"] is None
        for query in ("limit=0", "limit=101", "cursor=-1", "cursor=not-an-integer"):
            assert_error(await client.get(f"{path}/members?{query}"), 422, "INVALID_REQUEST")
        lease = await client.post(f"{path}/leases", json={})
        lease_id = lease.json()["lease_id"]
        await client.post(f"{path}/release")
        assert_error(await client.get(f"{path}/members"), 410, "ARTIFACT_GONE")
        accessible = await client.get(f"{path}/members", headers={"X-Artifact-Lease-Id": lease_id})
        assert accessible.status_code == 200 and len(accessible.json()["items"]) == 2
        duplicate = await client.get(
            path,
            headers=[
                ("X-Artifact-Lease-Id", lease_id),
                ("X-Artifact-Lease-Id", lease_id),
            ],
        )
        assert_error(duplicate, 400, "INVALID_REQUEST")
