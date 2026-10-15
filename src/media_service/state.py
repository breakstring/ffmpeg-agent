import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

from media_service.config import Settings
from media_service.database import Database, Transaction
from media_service.errors import ServiceError
from media_service.models import OPERATIONS, ArtifactInput, JobCreate, SharedFile, UploadedFile

TERMINAL = {"succeeded", "partial", "failed", "canceled"}


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def encode(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def timestamp(value: float | None) -> str | None:
    return (
        datetime.fromtimestamp(value, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        if value is not None
        else None
    )


class State:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    async def accept(
        self, caller: str, key: str, request: JobCreate, engine: dict,
        preflight: Callable[[], Awaitable[None]] | None = None,
        *, agent_request_id: str | None = None,
    ) -> tuple[str, bool]:
        policy = request.output_policy
        storage_id = policy.storage_id or self.settings.callers[caller].default_output_storage
        storage = self.settings.storages.get(storage_id)
        if storage is None or storage.role != "output" or caller not in storage.callers:
            raise ServiceError("FORBIDDEN", 403)
        if policy.retention_seconds > self.settings.max_retention_seconds:
            raise ServiceError("INVALID_REQUEST")
        for source in request.inputs.values():
            if isinstance(source, SharedFile):
                config = self.settings.storages.get(source.storage_id)
                if config is None or config.role != "input" or caller not in config.callers:
                    raise ServiceError("FORBIDDEN", 403)
        # 默认输出区是调用方配置的一部分，接受时冻结进规范化请求。
        request = request.model_copy(deep=True)
        request.output_policy.storage_id = storage_id
        digest = request.digest()
        job_id = uid("job")

        async def create(tx: Transaction):
            now = time.time()
            if agent_request_id is not None:
                agent = await tx.one(
                    "SELECT * FROM agent_requests WHERE id=? AND caller=?",
                    (agent_request_id, caller),
                )
                if agent is None or agent["job_hash"] != digest:
                    raise ServiceError("STATE_CONFLICT", 409)
                if agent["job_id"]:
                    return agent["job_id"], False
                if agent["cancel_requested"]:
                    raise ServiceError("CANCELED", 409)
                if agent["state"] != "submitting":
                    raise ServiceError("STATE_CONFLICT", 409)
                if agent["deadline"] <= now:
                    raise ServiceError("PLANNING_TIMEOUT", 504)
            existing = await tx.one(
                "SELECT id,request_hash FROM jobs WHERE caller=? AND idempotency_key=?",
                (caller, key),
            )
            if existing:
                if existing["request_hash"] != digest:
                    raise ServiceError("IDEMPOTENCY_CONFLICT", 409)
                await link_agent(tx, existing["id"])
                return existing["id"], False
            count = await tx.one("SELECT count(*) AS n FROM jobs WHERE status='queued'")
            output_usage = await tx.one(
                "SELECT COALESCE(sum(size_bytes),0) n FROM artifacts WHERE storage_id=? AND state!='deleted'",
                (storage_id,),
            )
            if output_usage is not None and output_usage["n"] >= storage.quota_bytes:
                raise ServiceError("QUOTA_EXCEEDED", 429, True)
            own = await tx.one(
                "SELECT count(*) AS n FROM jobs WHERE status='queued' AND caller=?", (caller,)
            )
            assert count is not None and own is not None
            if (
                count["n"] >= self.settings.max_queued_jobs
                or own["n"] >= self.settings.callers[caller].max_queued_jobs
            ):
                raise ServiceError("QUOTA_EXCEEDED", 429, True)
            artifacts: set[str] = set()
            uploaded_files: set[str] = set()
            for name, source in request.inputs.items():
                if isinstance(source, UploadedFile):
                    item = await tx.one(
                        "SELECT * FROM uploaded_files WHERE id=? AND caller=?",
                        (source.file_id, caller),
                    )
                    if item is None:
                        raise ServiceError("NOT_FOUND", 404)
                    if item["state"] != "ready" or item["retain_until"] <= now:
                        raise ServiceError("FILE_GONE", 410)
                    uploaded_files.add(source.file_id)
                    continue
                if not isinstance(source, ArtifactInput):
                    continue
                artifact = await tx.one(
                    "SELECT * FROM artifacts WHERE id=? AND caller=? AND public=1",
                    (source.artifact_id, caller),
                )
                if artifact is None:
                    raise ServiceError("NOT_FOUND", 404)
                if artifact["state"] != "ready" or artifact["retain_until"] <= now:
                    raise ServiceError("ARTIFACT_GONE", 410)
                if artifact["kind"] == "collection":
                    items = json.loads(artifact["manifest"])["items"]
                    if source.member_key is None or not any(
                        i.get("member_key") == source.member_key and not i.get("error")
                        for i in items
                    ):
                        raise ServiceError("UNSUPPORTED_OPERATION")
                    media_type = "image"
                else:
                    if source.member_key is not None:
                        raise ServiceError("UNSUPPORTED_OPERATION")
                    media_type = json.loads(artifact["media"]).get("type")
                for step in request.steps:
                    if any(getattr(b, "source", None) == name for b in step.inputs.values()):
                        required = OPERATIONS[step.operation][0]
                        if media_type in {"json", "collection"} or required not in {
                            "media",
                            media_type,
                        }:
                            raise ServiceError("UNSUPPORTED_MEDIA")
                artifacts.add(source.artifact_id)
            public = {
                "steps": [{"id": s.id, "operation": s.operation,
                           "effective_params": s.params}
                          for s in request.steps],
                "outputs": {
                    name: binding.model_dump() for name, binding in request.outputs.items()
                },
            }
            await tx.execute(
                "INSERT INTO jobs(id,caller,idempotency_key,request_hash,definition,public_definition,status,created,deadline,engine,output_storage,retention_seconds) VALUES(?,?,?,?,?,?,'queued',?,?,?,?,?)",
                (
                    job_id,
                    caller,
                    key,
                    digest,
                    request.canonical(),
                    encode(public),
                    now,
                    now + self.settings.task_timeout_seconds,
                    encode(engine),
                    storage_id,
                    policy.retention_seconds,
                ),
            )
            for index, step in enumerate(request.steps):
                await tx.execute(
                    "INSERT INTO steps(job_id,id,ordinal,operation) VALUES(?,?,?,?)",
                    (job_id, step.id, index, step.operation),
                )
            for artifact_id in artifacts:
                await tx.execute(
                    "INSERT INTO refs(id,artifact_id,job_id,kind,created) VALUES(?,?,?,'job',?)",
                    (uid("ref"), artifact_id, job_id, now),
                )
            for file_id in uploaded_files:
                await tx.execute(
                    "INSERT INTO file_refs(file_id,job_id) VALUES(?,?)", (file_id, job_id)
                )
            await link_agent(tx, job_id)
            return job_id, True

        async def link_agent(tx: Transaction, accepted_id: str):
            if agent_request_id is not None:
                await tx.execute(
                    "UPDATE agent_requests SET state='submitted',job_id=?,request=NULL WHERE id=?",
                    (accepted_id, agent_request_id),
                )

        if preflight is not None:
            existing = await self.db.one(
                "SELECT id,request_hash FROM jobs WHERE caller=? AND idempotency_key=?",
                (caller, key),
            )
            if existing:
                if existing["request_hash"] != digest:
                    raise ServiceError("IDEMPOTENCY_CONFLICT", 409)
                if agent_request_id is not None:
                    return await self.db.run(create)
                return existing["id"], False
            # 文件检查不占数据库事务；create 内仍重新检查并发接受的同键请求。
            await preflight()
        return await self.db.run(create)

    async def expire_queued(self):
        async def expire(tx: Transaction):
            for job in await tx.all(
                "SELECT id FROM jobs WHERE status='queued' AND deadline<=?", (time.time(),)
            ):
                await self._finish(tx, job["id"], "failed", ServiceError("TASK_TIMEOUT").public())

        await self.db.run(expire)

    async def job(self, caller: str, job_id: str) -> dict:
        async def read(tx: Transaction):
            job = await tx.one("SELECT * FROM jobs WHERE id=? AND caller=?", (job_id, caller))
            if job is None:
                raise ServiceError("NOT_FOUND", 404)
            steps = await tx.all("SELECT * FROM steps WHERE job_id=? ORDER BY ordinal", (job_id,))
            return job, steps

        job, steps = await self.db.run(read)
        public = json.loads(job["public_definition"])
        effective = {step["id"]: step.get("effective_params") for step in public["steps"]}
        results = [
            {
                "id": s["id"],
                "status": s["status"],
                "artifact_id": s["artifact_id"],
                "error": json.loads(s["error"]) if s["error"] else None,
                "effective_params": effective.get(s["id"]),
            }
            for s in steps
        ]
        by_id = {s["id"]: s for s in results}
        outputs = {}
        for name, binding in public["outputs"].items():
            step = by_id[binding["step"]]
            outputs[name] = binding | {key: step[key] for key in ("status", "artifact_id", "error")}
        return {
            "job_id": job["id"],
            "status": job["status"],
            "phase": job["phase"],
            "progress": None,
            "inputs_released": bool(job["inputs_released"]),
            "created_at": timestamp(job["created"]),
            "deadline_at": timestamp(job["deadline"]),
            "idempotency_expires_at": timestamp(job["idempotency_expires"]),
            "steps": results,
            "outputs": outputs,
            "error": json.loads(job["error"]) if job["error"] else None,
            "execution": json.loads(job["engine"]) | {"attempts": job["attempt_count"]},
        }

    async def claim(self) -> dict | None:
        now = time.time()

        async def claim_one(tx: Transaction):
            jobs = await tx.all("SELECT * FROM jobs WHERE status='queued' ORDER BY created,id")
            counts = {
                r["caller"]: r["n"]
                for r in await tx.all(
                    "SELECT caller,count(*) n FROM jobs WHERE status IN ('running','canceling') GROUP BY caller"
                )
            }
            if sum(counts.values()) >= self.settings.worker_concurrency:
                return None
            for job in jobs:
                if job["deadline"] <= now:
                    await self._finish(tx, job["id"], "failed", ServiceError("TASK_TIMEOUT").public())
                    continue
                caller = self.settings.callers.get(job["caller"])
                if caller is not None and counts.get(job["caller"], 0) >= caller.max_running_jobs:
                    continue
                attempt_id = uid("attempt")
                number = job["attempt_count"] + 1
                path = f"{job['id']}/{attempt_id}"
                changed = await tx.execute(
                    "UPDATE jobs SET status='running',phase='acquiring_inputs',current_attempt=?,attempt_count=? WHERE id=? AND status='queued'",
                    (attempt_id, number, job["id"]),
                )
                if changed != 1:
                    continue
                await tx.execute(
                    "INSERT INTO attempts(id,job_id,number,status,lease_until,path,started) VALUES(?,?,?,'running',?,?,?)",
                    (
                        attempt_id,
                        job["id"],
                        number,
                        now + self.settings.execution_lease_seconds,
                        path,
                        now,
                    ),
                )
                return job | {
                    "current_attempt": attempt_id,
                    "attempt_count": number,
                    "attempt_path": path,
                }
            return None

        return await self.db.run(claim_one)

    async def cancel(self, caller: str, job_id: str) -> bool:
        return await self.db.run(lambda tx: self.cancel_in_transaction(tx, caller, job_id))

    async def cancel_in_transaction(self, tx: Transaction, caller: str, job_id: str) -> bool:
        job = await tx.one("SELECT * FROM jobs WHERE id=? AND caller=?", (job_id, caller))
        if job is None:
            raise ServiceError("NOT_FOUND", 404)
        if job["status"] in TERMINAL:
            return False
        await tx.execute(
            "UPDATE jobs SET cancel_requested=1,status='canceling' WHERE id=?", (job_id,)
        )
        if job["current_attempt"] is None:
            await self._finish(tx, job_id, "canceled", ServiceError("CANCELED").public())
        return True

    async def eligible(
        self, tx: Transaction, job_id: str, attempt_id: str, *, cancellation: bool = False
    ) -> dict:
        job = await tx.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if (
            job is None
            or job["current_attempt"] != attempt_id
            or job["status"] not in {"running", "canceling"}
        ):
            raise ServiceError("EXECUTION_INTERRUPTED", 409)
        attempt = await tx.one(
            "SELECT lease_until FROM attempts WHERE id=? AND status='running'", (attempt_id,)
        )
        if attempt is None or attempt["lease_until"] <= time.time():
            raise ServiceError("EXECUTION_INTERRUPTED", 409)
        if job["cancel_requested"] and not cancellation:
            raise ServiceError("CANCELED", 409)
        if job["deadline"] <= time.time() and not cancellation:
            raise ServiceError("TASK_TIMEOUT", 409)
        return job

    async def heartbeat(self, job_id: str, attempt_id: str):
        await self.db.run(
            lambda tx: tx.execute(
                "UPDATE attempts SET lease_until=? WHERE id=? AND job_id=? AND status='running'",
                (time.time() + self.settings.execution_lease_seconds, attempt_id, job_id),
            )
        )

    async def phase(self, job_id: str, attempt_id: str, phase: str):
        async def change(tx: Transaction):
            await self.eligible(tx, job_id, attempt_id)
            await tx.execute("UPDATE jobs SET phase=? WHERE id=?", (phase, job_id))

        await self.db.run(change)

    async def step_start(self, job_id: str, attempt_id: str, step_id: str):
        async def change(tx: Transaction):
            await self.eligible(tx, job_id, attempt_id)
            await tx.execute(
                "UPDATE steps SET status='running' WHERE job_id=? AND id=? AND status='pending'",
                (job_id, step_id),
            )

        await self.db.run(change)

    async def step_error(
        self, job_id: str, attempt_id: str, step_id: str, status: str, error: dict
    ):
        async def change(tx: Transaction):
            await self.eligible(tx, job_id, attempt_id)
            await tx.execute(
                "UPDATE steps SET status=?,error=? WHERE job_id=? AND id=? AND status IN ('pending','running')",
                (status, encode(error), job_id, step_id),
            )

        await self.db.run(change)

    async def _finish(self, tx: Transaction, job_id: str, status: str, error: dict | None):
        now = time.time()
        await tx.execute(
            "UPDATE jobs SET status=?,phase=NULL,inputs_released=1,terminal_at=?,idempotency_expires=?,definition=NULL,error=? WHERE id=?",
            (
                status,
                now,
                now + self.settings.idempotency_seconds,
                encode(error) if error else None,
                job_id,
            ),
        )
        await tx.execute(
            "UPDATE steps SET status=?,error=? WHERE job_id=? AND status IN ('pending','running')",
            (
                "canceled" if status == "canceled" else "skipped",
                encode(error or ServiceError("DEPENDENCY_FAILED").public()),
                job_id,
            ),
        )
        await tx.execute(
            "UPDATE attempts SET status='stopped',stopped=? WHERE job_id=? AND status='running'",
            (now, job_id),
        )
        await tx.execute("DELETE FROM refs WHERE job_id=?", (job_id,))
        await tx.execute("DELETE FROM file_refs WHERE job_id=?", (job_id,))

    async def finish(self, job_id: str, attempt_id: str, status: str, error: dict | None = None):
        async def complete(tx: Transaction):
            job = await tx.one("SELECT * FROM jobs WHERE id=?", (job_id,))
            if job is None or job["status"] in TERMINAL:
                return
            if job["current_attempt"] != attempt_id:
                raise ServiceError("EXECUTION_INTERRUPTED", 409)
            if job["cancel_requested"]:
                final_status, final_error = "canceled", ServiceError("CANCELED").public()
            else:
                final_status, final_error = status, error
            await self._finish(tx, job_id, final_status, final_error)

        await self.db.run(complete)
