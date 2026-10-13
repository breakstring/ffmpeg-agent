import asyncio
import time

import pytest

from media_service.database import Database
from media_service.errors import ServiceError
from media_service.models import JobCreate
from media_service.scheduler import Scheduler
from media_service.state import State


def probe_request():
    return JobCreate.model_validate(
        {
            "inputs": {"main": {"kind": "shared_file", "storage_id": "input", "key": "222.mp3"}},
            "steps": [
                {
                    "id": "probe",
                    "operation": "media.probe.v1",
                    "inputs": {"media": {"source": "main"}},
                }
            ],
            "outputs": {"report": {"step": "probe", "output": "report"}},
        }
    )


@pytest.mark.asyncio
async def test_replay_survives_disk_full_and_queued_timeout_never_claims(settings):
    db = Database(settings.state_dir)
    await db.open()
    state = State(db, settings)
    try:
        request = probe_request()
        job_id, _ = await state.accept("test", "replay", request, {})

        async def full():
            raise ServiceError("DISK_FULL", 503, True)

        assert await state.accept("test", "replay", request, {}, full) == (job_id, False)
        with pytest.raises(ServiceError, match="DISK_FULL"):
            await state.accept("test", "new", request, {}, full)
        async def expire(tx):
            await tx.execute("UPDATE jobs SET deadline=? WHERE id=?", (time.time() - 1, job_id))
        await db.run(expire)
        await state.expire_queued()
        job = await state.job("test", job_id)
        assert job["status"] == "failed" and job["error"]["code"] == "TASK_TIMEOUT"
        assert job["inputs_released"] and job["execution"]["attempts"] == 0
        assert await state.claim() is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_atomic_idempotency_and_persistence(settings):
    db = Database(settings.state_dir)
    await db.open()
    state = State(db, settings)
    request = probe_request()
    results = await asyncio.gather(*(state.accept("test", "same", request, {}) for _ in range(20)))
    assert len({r[0] for r in results}) == 1
    assert sum(r[1] for r in results) == 1
    changed = request.model_copy(deep=True)
    changed.output_policy.retention_seconds = 42
    with pytest.raises(ServiceError, match="IDEMPOTENCY_CONFLICT"):
        await state.accept("test", "same", changed, {})
    job_id = results[0][0]
    await db.close()
    await db.open()
    assert (await state.job("test", job_id))["status"] == "queued"
    assert (await state.accept("test", "same", request, {})) == (job_id, False)
    await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 2, 4])
async def test_worker_slots_and_cancel_finish_race(settings, concurrency):
    settings.worker_concurrency = concurrency
    db = Database(settings.state_dir)
    await db.open()
    state = State(db, settings)
    gate = asyncio.Event()
    reached = asyncio.Event()
    active = 0
    maximum = 0

    async def execute(job, cancel):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == concurrency:
            reached.set()
        await gate.wait()
        active -= 1
        await state.finish(job["id"], job["current_attempt"], "failed")

    ids = [(await state.accept("test", str(i), probe_request(), {}))[0] for i in range(8)]
    scheduler = Scheduler(state, execute)
    scheduler.start()
    await asyncio.wait_for(reached.wait(), 2)
    assert maximum == concurrency
    assert len(scheduler.active) == concurrency
    running = next(iter(scheduler.active))
    await state.cancel("test", running)
    scheduler.notify_cancel(running)
    job = await state.job("test", running)
    assert job["status"] == "canceling" and job["inputs_released"] is False
    gate.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if all([(await state.job("test", job_id))["inputs_released"] for job_id in ids]):
            break
        await asyncio.sleep(0.01)
    assert (await state.job("test", running))["status"] == "canceled"
    assert (await state.job("test", running))["inputs_released"]
    await scheduler.stop()
    assert maximum == concurrency and not scheduler.failed
    await db.close()


@pytest.mark.asyncio
async def test_queue_quota_does_not_accept_and_replay_still_works(settings):
    settings.max_queued_jobs = 1
    db = Database(settings.state_dir)
    await db.open()
    state = State(db, settings)
    job, _ = await state.accept("test", "one", probe_request(), {})
    with pytest.raises(ServiceError, match="QUOTA_EXCEEDED"):
        await state.accept("test", "two", probe_request(), {})
    assert await state.accept("test", "one", probe_request(), {}) == (job, False)
    assert await state.cancel("test", job)
    assert (await state.job("test", job))["inputs_released"]
    assert not await state.cancel("test", job)
    await db.close()
