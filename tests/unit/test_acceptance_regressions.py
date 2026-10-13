import asyncio
import threading
import time

import pytest

import media_service.state as state_module
from media_service.artifacts import Artifacts
from media_service.database import Database
from media_service.errors import ServiceError
from media_service.models import JobCreate
from media_service.products import Product
from media_service.publishing import Publisher
from media_service.runtime import Runtime
from media_service.state import State, uid


@pytest.fixture
async def state(settings):
    settings.work_dir.mkdir()
    db = Database(settings.state_dir)
    await db.open()
    try:
        yield State(db, settings)
    finally:
        await db.close()


def request(source=None, *, frames=False):
    operation = "video.extract_frames.v1" if frames else "audio.transcode.v1"
    input_port, output_port = ("video", "frames") if frames else ("audio", "audio")
    return JobCreate.model_validate({
        "inputs": {"main": source or {
            "kind": "shared_file", "storage_id": "input", "key": "222.mp3",
        }},
        "steps": [{
            "id": "process", "operation": operation,
            "inputs": {input_port: {"source": "main"}},
            "params": {"timestamps_ms": [0, 999999]} if frames else {},
        }],
        "outputs": {"result": {"step": "process", "output": output_port}},
    })


async def running(state, *, frames=False):
    job_id, _ = await state.accept("test", uid("test"), request(frames=frames), {})
    job = await state.claim()
    assert job is not None and job["id"] == job_id
    return job


async def product(state, job, *, partial=False, publisher=None):
    source = state.settings.work_dir / job["attempt_path"] / "output"
    source.mkdir(parents=True)
    if partial:
        name = "frame-0.jpg"
        (source / name).write_bytes((state.settings.storages["input"].root / "haojiao.jpg").read_bytes())
        result = Product(
            kind="collection", media={"type": "collection"}, files=[name], status="partial",
            items=[
                {"index": 0, "member_key": name, "requested_time_ms": 0, "actual_time_ms": 0},
                {"index": 1, "requested_time_ms": 999999,
                 "error": ServiceError("TIMESTAMP_OUT_OF_RANGE").public()},
            ],
        )
    else:
        name = "audio.mp3"
        (source / name).write_bytes((state.settings.storages["input"].root / "222.mp3").read_bytes())
        result = Product(kind="file", media={"type": "audio"}, files=[name])
    await state.step_start(job["id"], job["current_attempt"], "process")
    return await (publisher or Publisher(state)).publish(job, "process", result, source, True)


@pytest.mark.parametrize("delay", ["preflight", "transaction_lock"])
async def test_acceptance_rechecks_artifact_expiry_inside_accept_transaction(state, monkeypatch, delay):
    producer = await running(state)
    artifact_id = await product(state, producer)
    await state.finish(producer["id"], producer["current_attempt"], "succeeded")
    clock = [time.time()]
    monkeypatch.setattr(state_module.time, "time", lambda: clock[0])
    await state.db.run(lambda tx: tx.execute(
        "UPDATE artifacts SET retain_until=? WHERE id=?", (clock[0] + 10, artifact_id)
    ))
    consumer = request({"kind": "artifact", "artifact_id": artifact_id})

    async def delayed_preflight():
        clock[0] += 20

    if delay == "preflight":
        with pytest.raises(ServiceError, match="ARTIFACT_GONE"):
            await state.accept("test", "late-reference", consumer, {}, delayed_preflight)
    else:
        await state.db.lock.acquire()
        task = asyncio.create_task(state.accept("test", "late-reference", consumer, {}))
        try:
            await asyncio.sleep(0)
            clock[0] += 20
        finally:
            state.db.lock.release()
        with pytest.raises(ServiceError, match="ARTIFACT_GONE"):
            await task
    assert await state.db.one("SELECT id FROM jobs WHERE idempotency_key='late-reference'") is None
    assert await state.db.all("SELECT * FROM refs WHERE artifact_id=?", (artifact_id,)) == []


@pytest.mark.parametrize("status", ["succeeded", "partial", "failed"])
async def test_complete_checkpoints_finish_without_spending_another_attempt(state, monkeypatch, status):
    state.settings.max_attempts = 1
    job = await running(state, frames=status == "partial")
    artifact_id = None
    if status == "failed":
        await state.step_error(job["id"], job["current_attempt"], "process", "failed",
                               ServiceError("INVALID_MEDIA").public())
    else:
        artifact_id = await product(state, job, partial=status == "partial")
    assert job["attempt_count"] == state.settings.max_attempts
    # 已提交检查点在截止前完成；恢复时仅剩终态收尾，不应重新执行媒体操作。
    monkeypatch.setattr(state_module.time, "time", lambda: job["deadline"] + 1)
    await Runtime(state, state.settings).recover_jobs({"engine": "changed-after-publication"})
    result = await state.job("test", job["id"])
    assert result["status"] == status
    assert result["inputs_released"]
    assert result["outputs"]["result"]["status"] == status
    assert result["outputs"]["result"]["artifact_id"] == artifact_id
    assert result["execution"]["attempts"] == 1
    assert result["error"] is None or result["error"]["code"] != "EXECUTION_INTERRUPTED"
    assert await state.db.all("SELECT * FROM refs WHERE job_id=?", (job["id"],)) == []


async def test_cancel_intent_wins_over_complete_checkpoints_at_attempt_limit(state):
    state.settings.max_attempts = 1
    job = await running(state)
    artifact_id = await product(state, job)
    await state.cancel("test", job["id"])
    await Runtime(state, state.settings).recover_jobs({})
    result = await state.job("test", job["id"])
    assert result["status"] == "canceled" and result["inputs_released"]
    assert result["error"]["code"] == "CANCELED"
    assert result["outputs"]["result"]["artifact_id"] == artifact_id
    assert result["outputs"]["result"]["status"] == "succeeded"
    assert await Artifacts(state).metadata("test", artifact_id)


async def test_deadline_expiring_during_actual_recovery_hash_does_not_abort_startup(state, monkeypatch):
    class CrashBeforeCommit(Exception):
        pass

    job = await running(state)
    publisher = Publisher(state)
    interrupted_artifact = None

    async def crash(name, artifact_id):
        nonlocal interrupted_artifact
        if name == "renamed":
            interrupted_artifact = artifact_id
            raise CrashBeforeCommit

    publisher.checkpoint_hook = crash
    with pytest.raises(CrashBeforeCommit):
        await product(state, job, publisher=publisher)
    assert interrupted_artifact is not None
    clock = [time.time()]
    monkeypatch.setattr(state_module.time, "time", lambda: clock[0])
    await state.db.run(lambda tx: tx.execute(
        "UPDATE jobs SET deadline=? WHERE id=?", (clock[0] + 10, job["id"])
    ))
    entered, release = threading.Event(), threading.Event()
    verify = publisher.verify_directory

    def paused_hash(artifact, key, event):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test hash gate timed out")
        return verify(artifact, key, event)

    monkeypatch.setattr(publisher, "verify_directory", paused_hash)
    task = asyncio.create_task(publisher.reconcile({}))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        clock[0] += 20
        release.set()
        await asyncio.wait_for(task, 2)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    await Runtime(state, state.settings).recover_jobs({})
    result = await state.job("test", job["id"])
    assert result["status"] == "failed" and result["error"]["code"] == "TASK_TIMEOUT"
    assert result["inputs_released"]
    await Artifacts(state).gc()
    assert await state.db.one(
        "SELECT state FROM artifacts WHERE id=?", (interrupted_artifact,)
    ) == {"state": "deleted"}
