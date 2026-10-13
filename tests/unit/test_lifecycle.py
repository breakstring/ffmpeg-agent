import asyncio
import hashlib
import json
import threading
import time
from datetime import datetime

import pytest

import media_service.artifacts as artifact_module
from media_service.artifacts import Artifacts
from media_service.config import Settings
from media_service.database import Database
from media_service.errors import ServiceError
from media_service.models import ArtifactResult, JobCreate
from media_service.state import State, encode, uid


@pytest.fixture
async def lifecycle(tmp_path):
    settings = Settings.model_validate({
        "state_dir": tmp_path / "state",
        "work_dir": tmp_path / "work",
        "storages": {
            "input": {"root": tmp_path / "input", "role": "input", "callers": ["test", "other"]},
            "output": {"root": tmp_path / "output", "role": "output", "callers": ["test", "other"]},
        },
        "callers": {
            "test": {"tokens": ["test-only-credential-with-no-business-access"],
                     "default_output_storage": "output"},
            "other": {"tokens": ["another-test-only-credential-no-business-access"],
                      "default_output_storage": "output"},
        },
        "public_base_url": "https://media.example.invalid",
    })
    settings.work_dir.mkdir()
    for storage in settings.storages.values():
        storage.root.mkdir()
    (settings.storages["input"].root / "source.mp4").write_bytes(b"caller-owned-source")
    db = Database(settings.state_dir)
    await db.open()
    state = State(db, settings)
    try:
        yield state, Artifacts(state)
    finally:
        await db.close()


def request(source=None):
    return JobCreate.model_validate({
        "inputs": {"main": source or {
            "kind": "shared_file", "storage_id": "input", "key": "source.mp4",
        }},
        "steps": [{"id": "encode", "operation": "video.transcode.v1",
                   "inputs": {"video": {"source": "main"}}}],
        "outputs": {"video": {"step": "encode", "output": "video"}},
    })


async def published(state, *, kind="file", public=True, terminal=True, publishing=False):
    job_id, _ = await state.accept("test", uid("test"), request(), {})
    job = await state.claim()
    assert job is not None and job["id"] == job_id
    artifact_id = uid("art")
    work = state.settings.work_dir / job["attempt_path"]
    work.mkdir(parents=True)
    (work / "scratch.bin").write_bytes(b"owned-working-copy")
    directory = artifact_id
    staging = f".staging_{artifact_id}"
    root = state.settings.storages["output"].root
    path = root / (staging if publishing else directory)
    path.mkdir()
    keys = ["video.mp4"] if kind == "file" else ["frame-0.png", "frame-2.png"]
    files = []
    for key in keys:
        contents = f"lifecycle-test:{key}".encode()
        (path / key).write_bytes(contents)
        files.append({"key": key, "size_bytes": len(contents),
                      "sha256": hashlib.sha256(contents).hexdigest()})
    items = []
    if kind == "collection":
        items = [
            {"index": 0, "requested_time_ms": 100, "actual_time_ms": 120,
             "member_key": keys[0], "size_bytes": files[0]["size_bytes"],
             "sha256": files[0]["sha256"]},
            {"index": 1, "requested_time_ms": 9999,
             "error": ServiceError("TIMESTAMP_OUT_OF_RANGE").public()},
            {"index": 2, "requested_time_ms": 100, "actual_time_ms": 120,
             "member_key": keys[1], "size_bytes": files[1]["size_bytes"],
             "sha256": files[1]["sha256"]},
        ]
    manifest = encode({"files": files, "items": items})
    (path / "manifest.json").write_text(manifest)
    now = time.time()

    async def insert(tx):
        await tx.execute(
            "INSERT INTO artifacts(id,caller,job_id,step_id,attempt_id,storage_id,directory,"
            "staging,state,public,kind,media,manifest,size_bytes,sha256,created,published,"
            "retain_until,result_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (artifact_id, "test", job_id, "encode", job["current_attempt"], "output", directory,
             staging, "publishing" if publishing else "ready", int(public), kind,
             encode({"type": "video" if kind == "file" else "collection"}), manifest,
             sum(f["size_bytes"] for f in files), hashlib.sha256(manifest.encode()).hexdigest(),
             now - 5, None if publishing else now - 5, None if publishing else now + 300,
             "succeeded" if kind == "file" else "partial"),
        )
        if not publishing:
            await tx.execute(
                "UPDATE steps SET status='succeeded',artifact_id=? WHERE job_id=?",
                (artifact_id, job_id),
            )
            await tx.execute(
                "INSERT INTO refs VALUES(?,?,?,'job',?)", (uid("ref"), artifact_id, job_id, now)
            )

    await state.db.run(insert)
    if terminal:
        await state.finish(job_id, job["current_attempt"], "failed" if publishing else "succeeded")
    return artifact_id, job, path


async def expect_error(awaitable, code, status):
    with pytest.raises(ServiceError) as failure:
        await awaitable
    assert (failure.value.code, failure.value.status) == (code, status)


async def test_metadata_authorization_and_shared_access(lifecycle):
    state, artifacts = lifecycle
    artifact_id, _, _ = await published(state)
    result = await artifacts.metadata("test", artifact_id)
    ArtifactResult.model_validate(result)
    assert result["access"]["http"]["content"] == (
        f"https://media.example.invalid/v1/artifacts/{artifact_id}/content"
    )
    assert result["access"]["shared"] == {
        "storage_id": "output", "key": f"{artifact_id}/video.mp4",
    }
    assert str(state.settings.storages["output"].root) not in json.dumps(result)
    await expect_error(artifacts.metadata("other", artifact_id), "NOT_FOUND", 404)
    state.settings.storages["output"].callers.remove("test")
    assert "shared" not in (await artifacts.metadata("test", artifact_id))["access"]


async def test_collection_pagination_keeps_failed_and_repeated_requests(lifecycle):
    state, artifacts = lifecycle
    artifact_id, _, _ = await published(state, kind="collection")
    first = await artifacts.members("test", artifact_id, limit=2)
    second = await artifacts.members("test", artifact_id, cursor=first["next_cursor"], limit=2)
    assert [entry["index"] for entry in first["items"] + second["items"]] == [0, 1, 2]
    assert first["items"][1]["error"]["code"] == "TIMESTAMP_OUT_OF_RANGE"
    assert first["items"][0]["requested_time_ms"] == second["items"][0]["requested_time_ms"]
    assert second["next_cursor"] is None
    result = await artifacts.metadata("test", artifact_id)
    assert (result["member_count"], result["item_count"]) == (2, 3)
    assert result["access"]["shared"]["key"] == artifact_id
    _, ref_id, file = await artifacts.acquire_send("test", artifact_id, "frame-2.png")
    assert file["key"] == "frame-2.png"
    await artifacts.release_send(ref_id)
    await expect_error(
        artifacts.acquire_send("test", artifact_id, "manifest.json"), "NOT_FOUND", 404
    )
    assert await state.db.all("SELECT * FROM refs") == []


async def test_retiring_artifact_requires_matching_valid_lease_for_every_read(lifecycle):
    state, artifacts = lifecycle
    artifact_id, _, path = await published(state, kind="collection")
    lease = await artifacts.lease_create("test", artifact_id, 900)
    other_id, _, _ = await published(state)
    wrong_lease = await artifacts.lease_create("test", other_id, 900)
    await artifacts.release("test", artifact_id)
    await artifacts.release("test", artifact_id)
    await artifacts.gc()
    assert path.is_dir()
    await expect_error(artifacts.metadata("test", artifact_id), "ARTIFACT_GONE", 410)
    await expect_error(artifacts.members("test", artifact_id), "ARTIFACT_GONE", 410)
    await expect_error(
        artifacts.metadata("test", artifact_id, wrong_lease["lease_id"]), "ARTIFACT_GONE", 410
    )
    assert (await artifacts.metadata("test", artifact_id, lease["lease_id"]))["state"] == "retiring"
    assert len((await artifacts.members("test", artifact_id, lease["lease_id"]))["items"]) == 3
    _, ref, _ = await artifacts.acquire_send("test", artifact_id, "frame-0.png", lease["lease_id"])
    await artifacts.release_send(ref)
    await expect_error(artifacts.lease_create("test", artifact_id, 1), "ARTIFACT_GONE", 410)
    await expect_error(artifacts.lease_renew("test", lease["lease_id"], 1), "ARTIFACT_GONE", 410)


async def test_send_reference_survives_expired_retention_and_released_lease(lifecycle):
    state, artifacts = lifecycle
    artifact_id, _, path = await published(state)
    lease = await artifacts.lease_create("test", artifact_id, 60)
    _, ref_id, file = await artifacts.acquire_send("test", artifact_id)
    with (path / file["key"]).open("rb") as handle:
        await artifacts.release("test", artifact_id)
        await artifacts.lease_release("test", lease["lease_id"])
        await state.db.run(lambda tx: tx.execute("UPDATE refs SET created=0 WHERE id=?", (ref_id,)))
        await artifacts.gc()
        assert path.is_dir()
        assert handle.read() == b"lifecycle-test:video.mp4"
        assert await state.db.one("SELECT id FROM refs WHERE id=?", (ref_id,))
    await artifacts.release_send(ref_id)
    await artifacts.release_send(ref_id)
    await artifacts.gc()
    assert not path.exists()
    await expect_error(artifacts.metadata("test", artifact_id), "ARTIFACT_GONE", 410)
    await expect_error(artifacts.metadata("other", artifact_id), "NOT_FOUND", 404)


async def test_lease_limits_no_shortening_and_cross_caller_release(lifecycle):
    state, artifacts = lifecycle
    state.settings.max_lease_seconds = 100
    state.settings.max_lease_total_seconds = 150
    artifact_id, _, _ = await published(state)
    lease = await artifacts.lease_create("test", artifact_id, 3600)
    row = await state.db.one("SELECT * FROM leases WHERE id=?", (lease["lease_id"],))
    assert row is not None and row["expires"] - row["created"] == 100
    shorter = await artifacts.lease_renew("test", lease["lease_id"], 1)
    assert shorter["expires_at"] == lease["expires_at"]
    await artifacts.lease_release("other", lease["lease_id"])
    assert (await state.db.one("SELECT released FROM leases WHERE id=?", (lease["lease_id"],))) == {"released": 0}
    await state.db.run(lambda tx: tx.execute(
        "UPDATE leases SET created=?,expires=? WHERE id=?",
        (time.time() - 100, time.time() + 1, lease["lease_id"]),
    ))
    await artifacts.lease_renew("test", lease["lease_id"], 3600)
    row = await state.db.one("SELECT * FROM leases WHERE id=?", (lease["lease_id"],))
    assert row is not None and row["expires"] - row["created"] == 150
    await artifacts.lease_release("test", lease["lease_id"])
    await artifacts.lease_release("test", lease["lease_id"])
    await expect_error(artifacts.lease_renew("test", lease["lease_id"], 1), "ARTIFACT_GONE", 410)


async def test_retention_extension_counts_from_publication_and_never_shortens(lifecycle):
    state, artifacts = lifecycle
    artifact_id, _, _ = await published(state)
    before = await state.db.one("SELECT published,retain_until FROM artifacts WHERE id=?", (artifact_id,))
    assert before is not None
    result = await artifacts.extend("test", artifact_id, 1)
    assert datetime.fromisoformat(result["retain_until"]).timestamp() == pytest.approx(before["retain_until"], abs=0.001)
    result = await artifacts.extend("test", artifact_id, 1000)
    assert datetime.fromisoformat(result["retain_until"]).timestamp() == pytest.approx(before["published"] + 1000, abs=0.001)
    await expect_error(artifacts.extend("test", artifact_id, 604801), "INVALID_REQUEST", 422)
    await artifacts.release("test", artifact_id)
    await expect_error(artifacts.extend("test", artifact_id, 1000), "ARTIFACT_GONE", 410)


async def test_queued_downstream_reference_protects_expired_input(lifecycle):
    state, artifacts = lifecycle
    artifact_id, _, path = await published(state)
    consumer, _ = await state.accept(
        "test", uid("test"), request({"kind": "artifact", "artifact_id": artifact_id}), {}
    )
    await state.db.run(lambda tx: tx.execute(
        "UPDATE artifacts SET retain_until=? WHERE id=?", (time.time() - 1, artifact_id)
    ))
    await artifacts.gc()
    assert path.is_dir()
    assert (await artifacts.resolve_input(consumer, artifact_id)).read_bytes() == b"lifecycle-test:video.mp4"
    await expect_error(state.accept(
        "test", uid("test"), request({"kind": "artifact", "artifact_id": artifact_id}), {}
    ), "ARTIFACT_GONE", 410)
    await state.cancel("test", consumer)
    await artifacts.gc()
    assert not path.exists()


@pytest.mark.parametrize("lease_first", [True, False])
async def test_expiry_racing_lease_creation_and_gc_never_returns_deleted_lease(lifecycle, lease_first):
    state, artifacts = lifecycle
    artifact_id, _, path = await published(state)
    operations = [artifacts.release("test", artifact_id), artifacts.gc()]
    lease_index = 0 if lease_first else len(operations)
    operations.insert(lease_index, artifacts.lease_create("test", artifact_id, 100))
    results = await asyncio.gather(*operations, return_exceptions=True)
    lease = results.pop(lease_index)
    if isinstance(lease, ServiceError):
        assert lease.code == "ARTIFACT_GONE"
    else:
        assert isinstance(lease, dict)
        assert path.is_dir()
        assert await artifacts.metadata("test", artifact_id, lease["lease_id"])
        await artifacts.lease_release("test", lease["lease_id"])
    assert not any(isinstance(result, BaseException) for result in results)
    await artifacts.gc()
    assert not path.exists()


async def test_tombstones_preserve_idempotency_until_window_and_all_outputs_end(lifecycle):
    state, artifacts = lifecycle
    artifact_id, job, path = await published(state)
    await artifacts.release("test", artifact_id)
    await artifacts.gc()
    assert not path.exists()
    assert (await state.job("test", job["id"]))["inputs_released"]
    replay, created = await state.accept("test", job["idempotency_key"], request(), {})
    assert replay == job["id"] and not created
    await state.db.run(lambda tx: tx.execute(
        "UPDATE jobs SET idempotency_expires=? WHERE id=?", (time.time() - 1, job["id"])
    ))
    result = await artifacts.gc()
    assert result["jobs_pruned"] == 1
    await expect_error(state.job("test", job["id"]), "NOT_FOUND", 404)
    await expect_error(artifacts.metadata("test", artifact_id), "NOT_FOUND", 404)
    for table in ("artifacts", "steps", "attempts", "downloads", "refs", "leases"):
        assert await state.db.all(f"SELECT * FROM {table}") == []


async def test_gc_only_removes_owned_paths_and_keeps_active_publishing(lifecycle):
    state, artifacts = lifecycle
    artifact_id, job, staging = await published(state, terminal=False, publishing=True)
    input_path = state.settings.storages["input"].root / "source.mp4"
    foreign = state.settings.storages["output"].root / "unregistered"
    foreign.mkdir()
    (foreign / "keep.bin").write_bytes(b"unregistered")
    await artifacts.gc()
    assert staging.is_dir()
    assert (state.settings.work_dir / job["id"]).is_dir()
    await state.finish(job["id"], job["current_attempt"], "failed")
    await artifacts.gc()
    assert not staging.exists()
    assert not (state.settings.work_dir / job["id"]).exists()
    assert (await state.db.one("SELECT state FROM artifacts WHERE id=?", (artifact_id,))) == {"state": "deleted"}
    assert input_path.read_bytes() == b"caller-owned-source"
    assert (foreign / "keep.bin").read_bytes() == b"unregistered"


async def test_failed_cleanup_keeps_deleting_record_and_retries(lifecycle, monkeypatch):
    state, artifacts = lifecycle
    artifact_id, _, path = await published(state)
    await artifacts.release("test", artifact_id)
    original = artifact_module.remove_owned_dir

    def fail(root, key, event):
        if key == artifact_id:
            raise PermissionError("test-only deletion failure")
        return original(root, key, event)

    monkeypatch.setattr(artifact_module, "remove_owned_dir", fail)
    await artifacts.gc()
    assert path.is_dir()
    assert (await state.db.one("SELECT state FROM artifacts WHERE id=?", (artifact_id,))) == {"state": "deleting"}
    monkeypatch.setattr(artifact_module, "remove_owned_dir", original)
    await artifacts.gc()
    assert not path.exists()
    assert (await state.db.one("SELECT state FROM artifacts WHERE id=?", (artifact_id,))) == {"state": "deleted"}


async def test_cancel_during_send_reference_commit_cannot_leak_reference(lifecycle, monkeypatch):
    state, artifacts = lifecycle
    artifact_id, _, _ = await published(state)
    committed = asyncio.Event()
    release = asyncio.Event()
    connection = state.db.connection
    assert connection is not None
    original_commit = connection.commit

    async def commit_gate():
        await original_commit()
        committed.set()
        await release.wait()

    monkeypatch.setattr(connection, "commit", commit_gate)
    task = asyncio.create_task(artifacts.acquire_send("test", artifact_id))
    try:
        await asyncio.wait_for(committed.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert await state.db.all("SELECT * FROM refs WHERE kind='send'") == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_repeated_cancel_during_send_release_still_removes_reference(lifecycle, monkeypatch):
    state, artifacts = lifecycle
    artifact_id, _, _ = await published(state)
    _, ref_id, _ = await artifacts.acquire_send("test", artifact_id)
    committing = asyncio.Event()
    released = asyncio.Event()
    connection = state.db.connection
    assert connection is not None
    original_commit = connection.commit

    async def commit_gate():
        committing.set()
        await released.wait()
        await original_commit()

    monkeypatch.setattr(connection, "commit", commit_gate)
    task = asyncio.create_task(artifacts.release_send(ref_id))
    try:
        await asyncio.wait_for(committing.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert await state.db.all("SELECT * FROM refs WHERE kind='send'") == []
    finally:
        released.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_private_intermediate_artifact_is_protected_only_by_internal_reference(lifecycle):
    state, artifacts = lifecycle
    artifact_id, job, path = await published(state, public=False, terminal=False)
    await state.db.run(lambda tx: tx.execute(
        "UPDATE artifacts SET retain_until=? WHERE id=?", (time.time() - 1, artifact_id)
    ))
    await artifacts.gc()
    assert (await artifacts.resolve_input(job["id"], artifact_id)).read_bytes() == b"lifecycle-test:video.mp4"
    await expect_error(artifacts.metadata("test", artifact_id), "NOT_FOUND", 404)
    await expect_error(artifacts.resolve_input(uid("job"), artifact_id), "NOT_FOUND", 404)
    await state.finish(job["id"], job["current_attempt"], "succeeded")
    await artifacts.gc()
    assert not path.exists()


async def test_cancelled_gc_waits_for_file_thread_and_leaves_retryable_registration(lifecycle, monkeypatch):
    state, artifacts = lifecycle
    artifact_id, _, path = await published(state)
    await artifacts.release("test", artifact_id)
    started = threading.Event()
    released = threading.Event()
    stopped = threading.Event()
    original = artifact_module.remove_owned_dir

    def gated(root, key, event):
        if key != artifact_id:
            return original(root, key, event)
        started.set()
        try:
            if not released.wait(5):
                raise RuntimeError("test cleanup gate timed out")
            return original(root, key, event)
        finally:
            stopped.set()

    monkeypatch.setattr(artifact_module, "remove_owned_dir", gated)
    task = asyncio.create_task(artifacts.gc())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not stopped.is_set()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert stopped.is_set()
        assert path.is_dir()
        assert (await state.db.one("SELECT state FROM artifacts WHERE id=?", (artifact_id,))) == {"state": "deleting"}
    finally:
        released.set()
        await asyncio.gather(task, return_exceptions=True)
    monkeypatch.setattr(artifact_module, "remove_owned_dir", original)
    await artifacts.gc()
    assert not path.exists()
