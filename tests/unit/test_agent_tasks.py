import asyncio
import json
import sqlite3
import time
from contextlib import asynccontextmanager

import pytest
from pydantic import SecretStr

from media_service.agent_models import AgentRequest, MediaPlan, PlanningOutcome
from media_service.database import SCHEMA, UPLOAD_SCHEMA, Database
from media_service.errors import ServiceError
from media_service.service import Service
from media_service.state import encode


def request():
    return AgentRequest.model_validate({
        "intent": "检查音频信息",
        "inputs": {"main": {"kind": "shared_file", "storage_id": "input",
                            "key": "audio_mp3_mono_22050.mp3"}},
    })


def outcome():
    return PlanningOutcome(decision="execute", plan=MediaPlan.model_validate({
        "steps": [{"id": "inspect", "operation": "media.probe.v1",
                   "inputs": {"media": {"source": "main"}}}],
        "outputs": {"report": {"step": "inspect", "output": "report"}},
    }))


class Planner:
    def __init__(self, *, block=False, fail=False):
        self.calls = 0
        self.live = self.maximum = 0
        self.entered = asyncio.Event()
        self.two_entered = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = fail
        self.closed = False
        if not block:
            self.release.set()

    async def plan(self, request, capabilities):
        self.calls += 1
        self.live += 1
        self.maximum = max(self.live, self.maximum)
        self.entered.set()
        if self.calls == 2:
            self.two_entered.set()
        try:
            await self.release.wait()
            if self.fail:
                raise RuntimeError("private-provider-value-that-must-not-leak")
            return outcome()
        finally:
            self.live -= 1

    async def close(self):
        self.closed = True


@asynccontextmanager
async def control(settings, planner=None, *, workers=True):
    settings.llm_enabled = True
    settings.llm_api_key = SecretStr("isolated-offline-key")
    settings.llm_model = "offline"
    settings.llm_base_url = "https://never-connect.invalid/v1"
    settings.work_dir.mkdir(exist_ok=True)
    service = Service(settings, planner=planner or Planner())
    await service.db.open()
    service.ready = True
    await service.agent_tasks.recover()
    if workers:
        await service.agent_tasks.start()
    try:
        yield service
    finally:
        await service.agent_tasks.close()
        await service.db.close()


async def wait_state(service, identifier, *statuses):
    async with asyncio.timeout(5):
        while True:
            row = await service.db.one("SELECT * FROM agent_requests WHERE id=?", (identifier,))
            if row["state"] in statuses:
                return row
            await asyncio.sleep(0.005)


async def test_concurrent_replay_has_one_planning_call_and_atomic_job_link(settings):
    planner = Planner(block=True)
    async with control(settings, planner) as service:
        accepted = await asyncio.gather(*[
            service.agent_tasks.submit("test", "same", request()) for _ in range(20)
        ])
        identifier = accepted[0][0]
        assert {item[0] for item in accepted} == {identifier}
        assert sum(item[1] for item in accepted) == 1
        await planner.entered.wait()
        assert planner.calls == 1
        changed = request().model_copy(update={"intent": "不同描述"})
        with pytest.raises(ServiceError, match="IDEMPOTENCY_CONFLICT"):
            await service.agent_tasks.submit("test", "same", changed)
        planner.release.set()
        row = await wait_state(service, identifier, "submitted")
        assert row["request"] is None
        assert row["job_id"] is not None
        assert (await service.db.one("SELECT count(*) n FROM jobs"))["n"] == 1
        result = await service.agent_tasks.get("test", identifier)
        assert result["job_id"] == row["job_id"] and result["status"] == "queued"
        assert "检查音频信息" not in encode(result)
        assert "audio_mp3_mono_22050.mp3" not in encode(result)
        with pytest.raises(ServiceError, match="NOT_FOUND"):
            await service.agent_tasks.get("another", identifier)
        with pytest.raises(ServiceError, match="NOT_FOUND"):
            await service.agent_tasks.cancel("another", identifier)


async def test_queue_and_concurrency_limits(settings):
    settings.planning_concurrency = 2
    settings.max_agent_requests = 3
    planner = Planner(block=True)
    async with control(settings, planner) as service:
        for index in range(3):
            await service.agent_tasks.submit("test", str(index), request())
        await asyncio.wait_for(planner.two_entered.wait(), 3)
        with pytest.raises(ServiceError, match="QUOTA_EXCEEDED"):
            await service.agent_tasks.submit("test", "over-limit", request())
        assert planner.maximum == 2
        planner.release.set()


async def test_planning_timeout_and_error_do_not_expose_provider(settings):
    settings.planning_timeout_seconds = 0.05
    async with control(settings, Planner(block=True)) as service:
        identifier, _ = await service.agent_tasks.submit("test", "timeout", request())
        row = await wait_state(service, identifier, "failed")
        assert json.loads(row["error"])["code"] == "PLANNING_TIMEOUT"
        assert row["request"] is None and row["job_id"] is None
    async with control(settings, Planner(fail=True)) as service:
        identifier, _ = await service.agent_tasks.submit("test", "failure", request())
        row = await wait_state(service, identifier, "failed")
        assert json.loads(row["error"])["code"] == "PLANNING_FAILED"
        assert "private-provider" not in encode(await service.agent_tasks.get("test", identifier))


async def test_accepted_media_job_outlives_planning_deadline_during_get_and_recovery(settings):
    settings.planning_timeout_seconds = 180
    settings.task_timeout_seconds = 7200
    async with control(settings) as service:
        identifier, _ = await service.agent_tasks.submit("test", "independent-timeouts", request())
        row = await wait_state(service, identifier, "submitted")
        job = await service.db.one("SELECT * FROM jobs WHERE id=?", (row["job_id"],))
        assert job["deadline"] - job["created"] == 7200
        assert job["deadline"] > row["deadline"]
        before = await service.agent_tasks.get("test", identifier)
        await service.db.run(lambda tx: tx.execute(
            "UPDATE agent_requests SET deadline=? WHERE id=?", (time.time() - 1, identifier),
        ))
        for recovered in (False, True):
            if recovered:
                await service.agent_tasks.recover()
            result = await service.agent_tasks.get("test", identifier)
            assert result["status"] == "queued" and result["error"] is None
            assert result["job_id"] == row["job_id"]
            assert result["job"]["deadline_at"] == before["job"]["deadline_at"]
            persisted = await service.db.one("SELECT * FROM jobs WHERE id=?", (row["job_id"],))
            assert persisted["deadline"] == job["deadline"]
            assert persisted["status"] == "queued" and persisted["error"] is None


async def test_cancel_while_error_settlement_waits_clears_private_request(settings, monkeypatch):
    async with control(settings, Planner(fail=True), workers=False) as service:
        entered = asyncio.Event()
        fail = service.agent_tasks._fail

        async def waiting_fail(identifier, error):
            if error.code == "PLANNING_FAILED":
                entered.set()
                await asyncio.Event().wait()
            await fail(identifier, error)

        monkeypatch.setattr(service.agent_tasks, "_fail", waiting_fail)
        identifier, _ = await service.agent_tasks.submit("test", "settling", request())
        await service.agent_tasks.start()
        await asyncio.wait_for(entered.wait(), 3)
        await service.agent_tasks.cancel("test", identifier)
        await service.agent_tasks.cancel("test", identifier)
        row = await wait_state(service, identifier, "canceled")
        assert row["request"] is None and row["job_id"] is None


async def test_close_before_process_first_runs_does_not_leave_planning(settings, monkeypatch):
    async with control(settings, workers=False) as service:
        claim = service.agent_tasks._claim

        async def closing_claim():
            row = await claim()
            service.agent_tasks.stopping = True
            return row

        monkeypatch.setattr(service.agent_tasks, "_claim", closing_claim)
        identifier, _ = await service.agent_tasks.submit("test", "first-await", request())
        await service.agent_tasks.start()
        row = await wait_state(service, identifier, "failed")
        assert row["request"] is None
        assert json.loads(row["error"])["code"] == "PLANNING_INTERRUPTED"


async def test_close_after_plan_commit_preserves_recoverable_plan(settings, monkeypatch):
    async with control(settings, workers=False) as service:
        saved = asyncio.Event()
        save = service.agent_tasks._save

        async def committed_save(*args):
            await save(*args)
            saved.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(service.agent_tasks, "_save", committed_save)
        identifier, _ = await service.agent_tasks.submit("test", "committed", request())
        await service.agent_tasks.start()
        await asyncio.wait_for(saved.wait(), 3)
        await service.agent_tasks.close()
        row = await wait_state(service, identifier, "planned")
        assert row["outcome"] is not None and row["request"] is not None
        assert row["error"] is None


@pytest.mark.parametrize("decision", ["execute", "needs_input", "unsupported"])
async def test_model_explanation_cannot_echo_private_description(settings, decision):
    class EchoPlanner(Planner):
        async def plan(self, request, capabilities):
            return PlanningOutcome(
                decision=decision, message=request.intent,
                plan=outcome().plan if decision == "execute" else None,
            )

    async with control(settings, EchoPlanner()) as service:
        body = request().model_copy(update={"intent": "私密描述-canary-不要持久化"})
        identifier, _ = await service.agent_tasks.submit("test", "echo", body)
        row = await wait_state(service, identifier, "submitted", "needs_input", "unsupported")
        assert row["request"] is None
        assert body.intent not in row["outcome"]
        assert body.intent not in encode(await service.agent_tasks.get("test", identifier))


async def test_failed_planner_initialization_leaves_no_background_workers(settings, monkeypatch):
    import media_service.planning

    settings.llm_enabled = True
    settings.llm_model = "offline"
    settings.llm_api_key = SecretStr("isolated-offline-key")
    settings.llm_base_url = "https://never-connect.invalid/v1"

    def invalid_client(settings):
        raise ValueError("isolated initialization failure")

    monkeypatch.setattr(media_service.planning, "PydanticPlanner", invalid_client)
    service = Service(settings)
    with pytest.raises(ValueError, match="isolated initialization failure"):
        await service.start()
    assert not service.ready
    assert service.db.connection is None
    assert service.scheduler.workers == []
    assert service.agent_tasks.workers == []
    assert service.maintenance is None


@pytest.mark.parametrize("stage", ["queued", "planning", "planned", "submitting", "submitted"])
async def test_recovery_resumes_saved_plan_without_replanning(settings, stage):
    planner = Planner()
    async with control(settings, planner, workers=False) as service:
        identifier, _ = await service.agent_tasks.submit("test", "recover", request())
        frozen = request().model_copy(deep=True)
        frozen.output_policy.storage_id = "output"
        plan = outcome()
        job = plan.plan.bind(frozen)
        await service.db.run(lambda tx: tx.execute(
            "UPDATE agent_requests SET state=?,outcome=?,job_hash=? WHERE id=?",
            (stage if stage != "submitted" else "submitting",
             encode(plan.model_dump()) if stage in {"planned", "submitting", "submitted"} else None,
             job.digest(), identifier),
        ))
        if stage == "submitted":
            original_id, _ = await service.accept(
                "test", f"agent:{identifier}", job, agent_request_id=identifier,
            )
        await service.agent_tasks.recover()
        await service.agent_tasks.start()
        row = await wait_state(service, identifier, "submitted", "failed")
        if stage == "planning":
            assert row["state"] == "failed"
            assert json.loads(row["error"])["code"] == "PLANNING_INTERRUPTED"
            assert row["request"] is None
        else:
            assert row["state"] == "submitted"
            assert (await service.db.one("SELECT count(*) n FROM jobs"))["n"] == 1
        if stage == "submitted":
            assert row["job_id"] == original_id
        assert planner.calls == (1 if stage == "queued" else 0)
        assert await service.agent_tasks.submit("test", "recover", request()) == (identifier, False)


@pytest.mark.parametrize("during_preflight", [False, True])
async def test_cancel_before_media_accept_cannot_leak_a_job(settings, during_preflight):
    async with control(settings, workers=False) as service:
        identifier, _ = await service.agent_tasks.submit("test", "cancel", request())
        frozen = request().model_copy(deep=True)
        frozen.output_policy.storage_id = "output"
        plan = outcome()
        job = plan.plan.bind(frozen)
        await service.db.run(lambda tx: tx.execute(
            "UPDATE agent_requests SET state='submitting',outcome=?,job_hash=? WHERE id=?",
            (encode(plan.model_dump()), job.digest(), identifier),
        ))
        entered = asyncio.Event()
        release = asyncio.Event()

        async def preflight():
            entered.set()
            await release.wait()

        if during_preflight:
            task = asyncio.create_task(service.state.accept(
                "test", f"agent:{identifier}", job, {}, preflight,
                agent_request_id=identifier,
            ))
            await entered.wait()
        assert (await service.agent_tasks.cancel("test", identifier))["status"] == "canceling"
        if during_preflight:
            release.set()
            with pytest.raises(ServiceError, match="CANCELED"):
                await task
        else:
            with pytest.raises(ServiceError, match="CANCELED"):
                await service.accept("test", f"agent:{identifier}", job,
                                     agent_request_id=identifier)
        assert await service.db.all("SELECT id FROM jobs") == []
        await service.agent_tasks.recover()
        assert (await service.agent_tasks.get("test", identifier))["status"] == "canceled"


async def test_cancel_after_accept_cancels_exact_job_and_releases_inputs(settings):
    async with control(settings) as service:
        identifier, _ = await service.agent_tasks.submit("test", "cancel-job", request())
        row = await wait_state(service, identifier, "submitted")
        canceled = await service.agent_tasks.cancel("test", identifier)
        assert canceled["job_id"] == row["job_id"]
        assert canceled["status"] == "canceled" and canceled["job"]["inputs_released"]
        assert await service.agent_tasks.cancel("test", identifier) == canceled


@pytest.mark.parametrize("file_state", ["expired", "retiring"])
async def test_files_expiring_during_planning_are_not_revived(settings, file_state):
    planner = Planner(block=True)
    async with control(settings, planner) as service:
        file_id = "file_" + "c" * 32
        await service.db.run(lambda tx: tx.execute(
            "INSERT INTO uploaded_files(id,caller,upload_key,state,path,created,retain_until) "
            "VALUES(?,'test','temporary','ready','/private/isolated-input',?,?)",
            (file_id, time.time(), time.time() + 60),
        ))
        body = AgentRequest(intent="检查音频", inputs={"main": {"kind": "uploaded_file", "file_id": file_id}})
        identifier, _ = await service.agent_tasks.submit("test", "expiring", body)
        await planner.entered.wait()
        await service.db.run(lambda tx: tx.execute(
            "UPDATE uploaded_files SET state=?,retain_until=? WHERE id=?",
            ("ready" if file_state == "expired" else "retiring", time.time() - 1, file_id),
        ))
        planner.release.set()
        row = await wait_state(service, identifier, "failed")
        assert json.loads(row["error"])["code"] == "FILE_GONE"
        assert await service.db.all("SELECT * FROM file_refs") == []
        assert await service.db.all("SELECT id FROM jobs") == []


async def test_gc_protects_job_metadata_until_agent_record_expires(settings):
    async with control(settings) as service:
        identifier, _ = await service.agent_tasks.submit("test", "gc", request())
        row = await wait_state(service, identifier, "submitted")
        await service.agent_tasks.cancel("test", identifier)
        await service.db.run(lambda tx: tx.execute(
            "UPDATE jobs SET work_cleaned=1,idempotency_expires=? WHERE id=?",
            (time.time() - 1, row["job_id"]),
        ))
        assert await service.db.run(service.artifacts._prune_jobs) == 0
        await service.agent_tasks.gc()
        await service.db.run(lambda tx: tx.execute(
            "UPDATE agent_requests SET expires_at=? WHERE id=?", (time.time() - 1, identifier),
        ))
        await service.agent_tasks.gc()
        assert await service.db.run(service.artifacts._prune_jobs) == 1
        assert await service.db.all("PRAGMA foreign_key_check") == []


@pytest.mark.parametrize("version", [0, 1, 2])
async def test_agent_schema_migration_preserves_existing_state(tmp_path, version):
    root = tmp_path / "migration"
    root.mkdir()
    if version:
        connection = sqlite3.connect(root / "state.sqlite")
        connection.executescript(SCHEMA if version == 2 else SCHEMA.removesuffix(UPLOAD_SCHEMA))
        connection.execute("INSERT INTO meta VALUES('existing','preserved')")
        connection.execute(f"PRAGMA user_version={version}")
        connection.commit()
        connection.close()
    db = Database(root)
    await db.open()
    try:
        assert (await db.one("PRAGMA user_version"))["user_version"] == 3
        assert await db.all("SELECT * FROM agent_requests") == []
        assert await db.all("SELECT * FROM uploaded_files") == []
        if version:
            assert await db.one("SELECT value FROM meta WHERE key='existing'") == {"value": "preserved"}
    finally:
        await db.close()
