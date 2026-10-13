import asyncio
import json
import time

import pytest

from media_service.errors import ServiceError
from media_service.models import JobCreate
from media_service.service import Service


def source(key):
    return {"kind": "shared_file", "storage_id": "input", "key": key}


def single(operation, port, output, input_source, params=None):
    return JobCreate.model_validate({
        "inputs": {"main": input_source},
        "steps": [{"id": "step", "operation": operation, "inputs": {port: {"source": "main"}}, "params": params or {}}],
        "outputs": {"result": {"step": "step", "output": output}},
    })


async def finished(service, job_id, wait_seconds=30):
    until = time.monotonic() + wait_seconds
    while time.monotonic() < until:
        job = await service.state.job("test", job_id)
        if job["inputs_released"]:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


@pytest.fixture
async def service(settings):
    app = Service(settings)
    await app.start()
    try:
        yield app
    finally:
        await app.close()


async def test_dag_independent_branches_skipped_dependency_partial_collection(service):
    request = JobCreate.model_validate({
        "inputs": {"main": source("ffmpeg_h264_640x360.mkv"), "missing": source("missing.mp4")},
        "steps": [
            {"id": "failed", "operation": "video.transcode.v1", "inputs": {"video": {"source": "missing"}}},
            {"id": "skipped", "operation": "media.probe.v1", "inputs": {"media": {"step": "failed", "output": "video"}}},
            {"id": "audio", "operation": "video.extract_audio.v1", "inputs": {"video": {"source": "main"}}, "params": {"preset": "wav_mono_16k_v1"}},
            {"id": "frames", "operation": "video.extract_frames.v1", "inputs": {"video": {"source": "main"}}, "params": {"timestamps_ms": [0, 0, 999999]}},
        ],
        "outputs": {"bad": {"step": "failed", "output": "video"}, "dependent": {"step": "skipped", "output": "report"}, "audio": {"step": "audio", "output": "audio"}, "frames": {"step": "frames", "output": "frames"}},
    })
    job_id, _ = await service.accept("test", "dag", request)
    job = await finished(service, job_id)
    assert job["status"] == "partial", job
    assert [s["status"] for s in job["steps"]] == ["failed", "skipped", "succeeded", "partial"]
    frames_id = job["outputs"]["frames"]["artifact_id"]
    first = await service.artifacts.members("test", frames_id, limit=2)
    assert first["next_cursor"] == 2 and [i["index"] for i in first["items"]] == [0, 1]
    assert first["items"][0]["actual_time_ms"] == first["items"][1]["actual_time_ms"] == 0
    assert first["items"][0]["member_key"] != first["items"][1]["member_key"]
    last = await service.artifacts.members("test", frames_id, cursor=2, limit=2)
    assert last["next_cursor"] is None and last["items"][0]["error"]["code"] == "TIMESTAMP_OUT_OF_RANGE"
    artifact = await service.db.one("SELECT manifest FROM artifacts WHERE id=?", (frames_id,))
    assert len(json.loads(artifact["manifest"])["files"]) == 2


async def test_all_failed_frame_items_are_explicit_without_fake_artifact(service):
    request = single("video.extract_frames.v1", "video", "frames", source("ffmpeg_h264_640x360.mkv"), {"timestamps_ms": [999999, 888888, 999999]})
    job_id, _ = await service.accept("test", "no-frames", request)
    job = await finished(service, job_id)
    assert job["status"] == "failed"
    assert job["outputs"]["result"]["artifact_id"] is None
    items = job["steps"][0]["error"]["details"]["items"]
    assert [i["index"] for i in items] == [0, 1, 2]
    assert not await service.db.all("SELECT * FROM artifacts WHERE job_id=?", (job_id,))


async def test_cancel_after_committed_output_keeps_immutable_binding(service):
    committed = asyncio.Event()
    gate = asyncio.Event()

    async def hook(point, artifact_id):
        if point == "committed":
            committed.set()
            await gate.wait()

    service.publisher.checkpoint_hook = hook
    request = JobCreate.model_validate({
        "inputs": {"main": source("222.mp3")},
        "steps": [
            {"id": "report", "operation": "media.probe.v1", "inputs": {"media": {"source": "main"}}},
            {"id": "audio", "operation": "audio.transcode.v1", "inputs": {"audio": {"source": "main"}}},
        ],
        "outputs": {"report": {"step": "report", "output": "report"}, "audio": {"step": "audio", "output": "audio"}},
    })
    job_id, _ = await service.accept("test", "checkpoint-cancel", request)
    await asyncio.wait_for(committed.wait(), 5)
    before = await service.state.job("test", job_id)
    artifact_id = before["outputs"]["report"]["artifact_id"]
    assert artifact_id is not None and not before["inputs_released"]
    await service.state.cancel("test", job_id)
    service.scheduler.notify_cancel(job_id)
    final = await finished(service, job_id)
    assert final["status"] == "canceled" and final["inputs_released"]
    assert final["outputs"]["report"]["artifact_id"] == artifact_id
    assert final["outputs"]["audio"]["artifact_id"] is None
    assert (await service.artifacts.metadata("test", artifact_id))["state"] == "ready"
    assert not service.executor.active_pids
    assert not await service.state.cancel("test", job_id)


async def test_pending_internal_artifact_reference_survives_retirement(service):
    request = single("audio.transcode.v1", "audio", "audio", source("222.mp3"))
    first_id, _ = await service.accept("test", "first-audio", request)
    first = await finished(service, first_id)
    artifact_id = first["outputs"]["result"]["artifact_id"]
    metadata = await service.artifacts.metadata("test", artifact_id)
    path = service.settings.storages["output"].root / metadata["access"]["shared"]["key"]
    blocked = asyncio.Event()
    gate = asyncio.Event()

    async def hook(point, _):
        if point == "intent":
            blocked.set()
            await gate.wait()

    service.publisher.checkpoint_hook = hook
    blocker, _ = await service.accept("test", "blocker", single("media.probe.v1", "media", "report", source("222.mp3")))
    await asyncio.wait_for(blocked.wait(), 5)
    downstream, _ = await service.accept("test", "reuse", single("media.probe.v1", "media", "report", {"kind": "artifact", "artifact_id": artifact_id}))
    assert (await service.state.job("test", downstream))["status"] == "queued"
    await service.artifacts.release("test", artifact_id)
    await service.artifacts.gc()
    assert path.exists()
    with pytest.raises(ServiceError, match="ARTIFACT_GONE"):
        await service.accept("test", "new-consumer", single("media.probe.v1", "media", "report", {"kind": "artifact", "artifact_id": artifact_id}))
    service.publisher.checkpoint_hook = None
    await service.state.cancel("test", blocker)
    service.scheduler.notify_cancel(blocker)
    final = await finished(service, downstream)
    assert final["status"] == "succeeded", final
    await service.artifacts.gc()
    assert not path.exists()


async def test_two_real_media_jobs_cancel_one_does_not_stop_other(settings):
    settings.worker_concurrency = 2
    app = Service(settings)
    await app.start()
    try:
        request = single("video.transcode.v1", "video", "video", source("Feline_Apocalypse.mp4"))
        first, _ = await app.accept("test", "cancel-one", request)
        second, _ = await app.accept("test", "keep-one", request)
        until = time.monotonic() + 10
        while len(app.executor.active_pids) < 2:
            assert time.monotonic() < until
            await asyncio.sleep(0.005)
        await app.state.cancel("test", first)
        app.scheduler.notify_cancel(first)
        canceled, completed = await asyncio.gather(finished(app, first), finished(app, second))
        assert canceled["status"] == "canceled"
        assert completed["status"] == "succeeded", completed
        assert not app.executor.active_pids
        assert canceled["inputs_released"] and completed["inputs_released"]
    finally:
        await app.close()
