import json
import os

import pytest

from media_service.database import Database
from media_service.errors import ServiceError
from media_service.models import JobCreate
from media_service.products import Product
from media_service.publishing import Publisher
from media_service.runtime import Runtime
from media_service.state import State


class PublicationInterrupted(Exception):
    pass


def definition():
    return JobCreate.model_validate(
        {
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
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["intent", "staged", "renamed", "committed"])
async def test_publication_windows_reconcile_after_state_reopen(settings, tmp_path, window):
    db = Database(settings.state_dir)
    await db.open()
    state = State(db, settings)
    job_id, _ = await state.accept("test", "checkpoint", definition(), {})
    job = await state.claim()
    assert job is not None
    await state.step_start(job_id, job["current_attempt"], "probe")
    source = tmp_path / "source"
    source.mkdir()
    (source / "report.json").write_text('{"tested":true}')
    publisher = Publisher(state)
    published_id = None

    async def hook(point, artifact_id):
        nonlocal published_id
        if point == window:
            published_id = artifact_id
            raise PublicationInterrupted

    publisher.checkpoint_hook = hook
    with pytest.raises(PublicationInterrupted):
        await publisher.publish(
            job, "probe", Product("file", {"type": "json"}, ["report.json"]), source, True
        )
    artifact = await db.one("SELECT * FROM artifacts WHERE id=?", (published_id,))
    assert artifact["state"] == ("ready" if window == "committed" else "publishing")
    before = await state.job("test", job_id)
    assert (before["outputs"]["report"]["artifact_id"] is not None) == (window == "committed")
    await db.close()
    # 此用例仅重开真实 SQLite，文件工作已同步结束；容器硬停止另有独立验收。
    await db.open()
    publisher = Publisher(state)
    await publisher.reconcile({})
    await Runtime(state, settings).recover_jobs({})
    artifact = await db.one("SELECT * FROM artifacts WHERE id=?", (published_id,))
    step = await db.one("SELECT * FROM steps WHERE job_id=?", (job_id,))
    if window == "intent":
        assert artifact["state"] == "deleting" and step["status"] == "pending"
        assert step["artifact_id"] is None
    else:
        assert artifact["state"] == "ready" and step["status"] == "succeeded"
        assert step["artifact_id"] == published_id
        assert (
            settings.storages["output"].root / artifact["directory"] / "report.json"
        ).read_text() == '{"tested":true}'
        await publisher.reconcile({})
        assert (await db.one("SELECT artifact_id FROM steps WHERE job_id=?", (job_id,)))[
            "artifact_id"
        ] == published_id
    await db.close()


@pytest.mark.asyncio
async def test_corrupted_staging_never_becomes_ready(settings, tmp_path):
    db = Database(settings.state_dir)
    await db.open()
    state = State(db, settings)
    job_id, _ = await state.accept("test", "corrupt-stage", definition(), {})
    job = await state.claim()
    await state.step_start(job_id, job["current_attempt"], "probe")
    source = tmp_path / "source"
    source.mkdir()
    (source / "report.json").write_text("expected")
    publisher = Publisher(state)

    async def hook(point, artifact_id):
        if point == "staged":
            artifact = await db.one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
            target = settings.storages["output"].root / artifact["staging"] / "report.json"
            os.chmod(target, 0o600)
            target.write_text("corrupt")
            raise PublicationInterrupted

    publisher.checkpoint_hook = hook
    with pytest.raises(PublicationInterrupted):
        await publisher.publish(job, "probe", Product("file", {}, ["report.json"]), source, True)
    await Publisher(state).reconcile({})
    artifact = await db.one("SELECT * FROM artifacts WHERE job_id=?", (job_id,))
    assert artifact["state"] == "deleting"
    assert (await state.job("test", job_id))["outputs"]["report"]["artifact_id"] is None
    await db.close()


@pytest.mark.asyncio
async def test_finished_job_cannot_publish_and_revoked_attempt_cannot_commit(settings, tmp_path):
    db = Database(settings.state_dir)
    await db.open()
    state = State(db, settings)
    job_id, _ = await state.accept("test", "stale-attempt", definition(), {})
    job = await state.claim()
    await state.step_start(job_id, job["current_attempt"], "probe")
    source = tmp_path / "source"
    source.mkdir()
    (source / "report.json").write_text("expected")
    publisher = Publisher(state)

    async def hook(point, artifact_id):
        if point == "renamed":
            await state.finish(job_id, job["current_attempt"], "failed")

    publisher.checkpoint_hook = hook
    with pytest.raises(ServiceError, match="EXECUTION_INTERRUPTED"):
        await publisher.publish(job, "probe", Product("file", {}, ["report.json"]), source, True)
    await publisher.reconcile({})
    artifact = await db.one("SELECT * FROM artifacts WHERE job_id=?", (job_id,))
    assert artifact["state"] == "deleting"
    assert not json.loads(artifact["manifest"])["items"]
    await db.close()
