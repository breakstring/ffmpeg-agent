import asyncio
import json
import logging
import os
import shutil
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from media_service.agent_tasks import AgentTasks
from media_service.artifacts import Artifacts
from media_service.config import Settings
from media_service.database import Database, Transaction, wait_settled
from media_service.downloader import Downloader
from media_service.errors import ServiceError
from media_service.executor import Executor
from media_service.files import file_job, safe_open, sha256_fd
from media_service.media import MediaEngine
from media_service.models import ArtifactInput, JobCreate, SharedFile, SourceBinding, UploadedFile
from media_service.publishing import Publisher
from media_service.runtime import Runtime
from media_service.scheduler import Scheduler
from media_service.state import State
from media_service.telemetry import event
from media_service.uploads import Uploads

logger = logging.getLogger("media_service")

if TYPE_CHECKING:
    from media_service.planning import Planner


class Service:
    def __init__(self, settings: Settings, *, planner: Planner | None = None):
        self.settings = settings
        self.db = Database(settings.state_dir)
        self.state = State(self.db, settings)
        self.runtime = Runtime(self.state, settings)
        self.executor = Executor(settings)
        self.media = MediaEngine(settings, self.executor)
        self.publisher = Publisher(self.state)
        self.artifacts = Artifacts(self.state)
        self.downloader = Downloader(self.state)
        self.uploads = Uploads(self.state)
        self.scheduler = Scheduler(self.state, self.execute)
        self.engine: dict = {}
        self.ready = False
        self.closing = False
        self.maintenance: asyncio.Task | None = None
        self.agent_tasks = AgentTasks(self, planner)

    async def start(self):
        self.settings.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.settings.work_dir, 0o700)
        for storage in self.settings.storages.values():
            if not storage.root.is_dir() or storage.root.is_symlink():
                raise RuntimeError("configured storage root is unavailable")
            if storage.role == "output" and not os.access(storage.root, os.W_OK):
                raise RuntimeError("output storage is not writable")
        await self.disk_preflight()
        await self.db.open()
        self.executor.guard_fd = self.db.lock_fd
        try:
            self.engine = await self.media.inspect()
            await self.runtime.begin()
            await self.uploads.initialize()
            await self.uploads.recover()
            await self.publisher.reconcile(self.engine)
            await self.runtime.recover_jobs(self.engine)
            # 规划恢复先于回收，未结束的计划/关联保护既有任务元数据。
            await self.agent_tasks.recover()
            await self.artifacts.gc()
            await self.uploads.gc()
            await self.agent_tasks.start()
            self.scheduler.start()
            self.maintenance = asyncio.create_task(self.maintain())
            self.ready = True
        except BaseException:
            self.ready = False
            self.closing = True
            await self.agent_tasks.close()
            await self.scheduler.stop()
            if self.maintenance is not None:
                self.maintenance.cancel()
                with suppress(asyncio.CancelledError):
                    await self.maintenance
            await self.db.close()
            raise

    async def disk_preflight(self):
        roots = [self.settings.work_dir] + [
            s.root for s in self.settings.storages.values() if s.role == "output"
        ]

        def inspect(event):
            for root in roots:
                if shutil.disk_usage(root).free < self.settings.disk_reserve_bytes:
                    raise ServiceError("DISK_FULL", 503, True)

        await file_job(inspect)

    async def maintain(self):
        while not self.closing:
            await asyncio.sleep(self.settings.gc_interval_seconds)
            if self.scheduler.failed or self.agent_tasks.failed or not self.db.healthy:
                self.ready = False
                # 正式入口由容器init托管，致命错误终止整个应用生命周期。
                if self.settings.runtime_mode == "container":
                    import signal

                    os.kill(os.getpid(), signal.SIGTERM)
                return
            try:
                await self.state.expire_queued()
                await self.agent_tasks.gc()
                await self.artifacts.gc()
                await self.uploads.gc()
            except Exception:
                logger.error('{"event":"gc_retry","code":"STATE_UNAVAILABLE"}')

    async def accept(
        self, caller: str, key: str, request: JobCreate, *, agent_request_id: str | None = None,
    ) -> tuple[str, bool]:
        if not self.ready or self.scheduler.failed or not self.db.healthy:
            raise ServiceError("STATE_UNAVAILABLE", 503, True)
        async def preflight():
            self.media.validate_request(request, self.engine)
            await self.disk_preflight()

        result = await self.state.accept(
            caller, key, request, self.engine, preflight, agent_request_id=agent_request_id,
        )
        self.scheduler.wake.set()
        return result

    async def execute(self, job: dict, cancel: asyncio.Event):
        started = time.monotonic()
        event("job_started", job_id=job["id"], attempt_id=job["current_attempt"],
              queue_ms=round((time.time() - job["created"]) * 1000, 2),
              active_jobs=len(self.scheduler.active))
        task = asyncio.create_task(self.pipeline(job, cancel))
        stop = asyncio.create_task(cancel.wait())
        error: ServiceError | None = None
        status = "failed"
        try:
            done, _ = await asyncio.wait(
                {task, stop},
                timeout=max(0, job["deadline"] - time.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                status = task.result()
            else:
                error = ServiceError("CANCELED" if cancel.is_set() else "TASK_TIMEOUT")
                task.cancel()
                with suppress(asyncio.CancelledError, ServiceError):
                    await wait_settled(task)
        except ServiceError as exc:
            error = exc
        except asyncio.CancelledError:
            task.cancel()
            with suppress(asyncio.CancelledError, ServiceError):
                await wait_settled(task)
            error = ServiceError("EXECUTION_INTERRUPTED")
            raise
        except OSError as exc:
            error = ServiceError(
                "DISK_FULL" if exc.errno == 28 else "EXECUTION_INTERRUPTED", retryable=True
            )
        except Exception:
            error = ServiceError("EXECUTION_INTERRUPTED")
        finally:
            stop.cancel()
            with suppress(asyncio.CancelledError):
                await stop
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await wait_settled(task)
            if self.closing and error and error.code in {"CANCELED", "EXECUTION_INTERRUPTED"}:
                await self.suspend(job)
            else:
                await self.state.finish(
                    job["id"], job["current_attempt"], status, error.public() if error else None
                )
            current = await self.state.job(job["caller"], job["id"])
            event("job_stopped", job_id=job["id"], attempt_id=job["current_attempt"],
                  status=current["status"], code=(current["error"] or {}).get("code"),
                  duration_ms=round((time.monotonic() - started) * 1000, 2),
                  active_processes=len(self.executor.active_pids))

    async def suspend(self, job: dict):
        async def suspend(tx: Transaction):
            current = await tx.one("SELECT * FROM jobs WHERE id=?", (job["id"],))
            if current and current["cancel_requested"]:
                await self.state._finish(
                    tx, job["id"], "canceled", ServiceError("CANCELED").public()
                )
            elif current and current["current_attempt"] == job["current_attempt"]:
                await tx.execute(
                    "UPDATE jobs SET status='queued',phase=NULL,current_attempt=NULL WHERE id=?",
                    (job["id"],),
                )
                await tx.execute(
                    "UPDATE attempts SET status='stopped',stopped=? WHERE id=?",
                    (time.time(), job["current_attempt"]),
                )
                await tx.execute(
                    "UPDATE steps SET status='pending' WHERE job_id=? AND status='running'",
                    (job["id"],),
                )

        await self.db.run(suspend)

    async def pipeline(self, job: dict, cancel: asyncio.Event) -> str:
        if cancel.is_set():
            raise ServiceError("CANCELED")
        if json.loads(job["engine"]) != self.engine:
            raise ServiceError("ENGINE_INCOMPATIBLE")
        request = JobCreate.model_validate_json(job["definition"])
        work = self.settings.work_dir / job["attempt_path"]
        work.mkdir(parents=True, mode=0o700)
        completed = {
            s["id"]: s
            for s in await self.db.all("SELECT * FROM steps WHERE job_id=?", (job["id"],))
        }
        needed = {
            b.source
            for s in request.steps
            if completed[s.id]["status"] == "pending"
            for b in s.inputs.values()
            if isinstance(b, SourceBinding)
        }
        descriptors: dict[str, int] = {}
        errors: dict[str, ServiceError] = {}
        try:
            for name in needed:
                source = request.inputs[name]
                try:
                    if isinstance(source, SharedFile):
                        storage = self.settings.storages.get(source.storage_id)
                        if storage is None or job["caller"] not in storage.callers:
                            raise ServiceError("FORBIDDEN")
                        fd = safe_open(storage.root, source.key, self.settings.max_input_bytes)
                    elif isinstance(source, UploadedFile):
                        item = await self.db.one(
                            "SELECT f.path FROM uploaded_files f WHERE f.id=? "
                            "AND f.state IN ('ready','retiring') AND EXISTS "
                            "(SELECT 1 FROM file_refs r WHERE r.file_id=f.id AND r.job_id=?)",
                            (source.file_id, job["id"]),
                        )
                        if item is None:
                            raise ServiceError("FILE_GONE", 410)
                        fd = safe_open(self.settings.uploads_dir, item["path"], self.settings.max_input_bytes)
                    elif isinstance(source, ArtifactInput):
                        path = await self.resolve_artifact(
                            job["id"], source.artifact_id, source.member_key
                        )
                        fd = safe_open(path.parent, path.name, self.settings.max_input_bytes)
                    else:
                        path = await self.downloader.acquire(job, name, source, cancel)
                        fd = safe_open(path.parent, path.name, self.settings.max_input_bytes)
                    descriptors[name] = fd
                    expected_size = getattr(source, "expected_size_bytes", None)
                    expected_hash = getattr(source, "expected_sha256", None)
                    if expected_size is not None and os.fstat(fd).st_size != expected_size:
                        raise ServiceError("INPUT_CHANGED")
                    if expected_hash is not None:
                        _, actual = await file_job(lambda event, fd=fd: sha256_fd(fd, event))
                        if actual != expected_hash:
                            raise ServiceError("INPUT_CHANGED")
                except ServiceError as exc:
                    errors[name] = exc
            await self.state.phase(job["id"], job["current_attempt"], "processing")
            for step in request.ordered_steps():
                previous = completed[step.id]
                if previous["status"] != "pending":
                    continue
                if cancel.is_set():
                    raise ServiceError("CANCELED")
                binding = next(iter(step.inputs.values()))
                own_fd = False
                fd = -1
                step_started = time.monotonic()
                step_code = None
                try:
                    if isinstance(binding, SourceBinding):
                        if binding.source in errors:
                            raise errors[binding.source]
                        fd = descriptors[binding.source]
                    else:
                        parent = completed[binding.step]
                        if parent["status"] != "succeeded":
                            await self.state.step_error(
                                job["id"],
                                job["current_attempt"],
                                step.id,
                                "skipped",
                                ServiceError("DEPENDENCY_FAILED").public(),
                            )
                            completed[step.id] = {"status": "skipped"}
                            continue
                        path = await self.resolve_artifact(job["id"], parent["artifact_id"])
                        fd = safe_open(path.parent, path.name, self.settings.max_input_bytes)
                        own_fd = True
                    await self.state.phase(job["id"], job["current_attempt"], "processing")
                    await self.state.step_start(job["id"], job["current_attempt"], step.id)
                    output = work / step.id
                    output.mkdir(mode=0o700)
                    product = await self.media.run(
                        step.operation, step.params, fd, output, job["deadline"], cancel
                    )
                    await self.state.phase(job["id"], job["current_attempt"], "publishing")
                    public = any(b.step == step.id for b in request.outputs.values())
                    artifact_id = await self.publisher.publish(
                        job, step.id, product, output, public
                    )
                    completed[step.id] = {"status": product.status, "artifact_id": artifact_id}
                except ServiceError as exc:
                    step_code = exc.code
                    if exc.code in {
                        "CANCELED",
                        "TASK_TIMEOUT",
                        "EXECUTION_INTERRUPTED",
                        "DISK_FULL",
                        "OUTPUT_LIMIT",
                    }:
                        raise
                    await self.state.step_error(
                        job["id"], job["current_attempt"], step.id, "failed", exc.public()
                    )
                    completed[step.id] = {"status": "failed"}
                finally:
                    if own_fd and fd >= 0:
                        os.close(fd)
                    event("step_stopped", job_id=job["id"], attempt_id=job["current_attempt"],
                          step_id=step.id, operation=step.operation, code=step_code,
                          status=completed.get(step.id, {}).get("status"),
                          duration_ms=round((time.monotonic() - step_started) * 1000, 2))
            statuses = [completed[b.step]["status"] for b in request.outputs.values()]
            if all(s == "succeeded" for s in statuses):
                return "succeeded"
            return "partial" if any(s in {"succeeded", "partial"} for s in statuses) else "failed"
        finally:
            for fd in descriptors.values():
                os.close(fd)

    async def resolve_artifact(
        self, job_id: str, artifact_id: str, member_key: str | None = None
    ) -> Path:
        artifact = await self.db.one(
            "SELECT a.* FROM artifacts a WHERE a.id=? AND a.state IN ('ready','retiring') AND EXISTS (SELECT 1 FROM refs r WHERE r.artifact_id=a.id AND r.job_id=?)",
            (artifact_id, job_id),
        )
        if artifact is None:
            raise ServiceError("ARTIFACT_GONE")
        files = json.loads(artifact["manifest"])["files"]
        key = member_key or (files[0]["key"] if artifact["kind"] == "file" else None)
        if not key or not any(f["key"] == key for f in files):
            raise ServiceError("UNSUPPORTED_MEDIA")
        return self.settings.storages[artifact["storage_id"]].root / artifact["directory"] / key

    async def close(self):
        self.ready = False
        self.closing = True
        await self.agent_tasks.close()
        await self.uploads.close()
        await self.scheduler.stop()
        await self.executor.stop_all()
        if self.maintenance:
            self.maintenance.cancel()
            with suppress(asyncio.CancelledError):
                await self.maintenance
        if self.db.connection:
            await self.runtime.clean_stop()
            await self.db.close()
