import asyncio
import hashlib
import json
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING

from pydantic import ValidationError

from media_service.agent_models import AgentRequest, AgentTaskResult, PlanningOutcome
from media_service.database import Transaction, wait_settled
from media_service.errors import ServiceError
from media_service.media import capability_document
from media_service.state import encode, timestamp, uid

if TYPE_CHECKING:
    from media_service.planning import Planner
    from media_service.service import Service

logger = logging.getLogger("media_service")
PLANNING_TERMINAL = {"needs_input", "unsupported", "failed", "canceled"}


class AgentTasks:
    def __init__(self, service: Service, planner: Planner | None = None):
        self.service = service
        self.settings = service.settings
        self.db = service.db
        self.planner = planner
        self.failed = False
        self.stopping = False
        self.wake = asyncio.Event()
        self.workers: list[asyncio.Task] = []
        self.active: dict[str, asyncio.Task] = {}

    async def start(self):
        if not self.settings.llm_enabled:
            return
        if self.planner is None:
            from media_service.planning import PydanticPlanner

            self.planner = PydanticPlanner(self.settings)
        self.workers = [
            asyncio.create_task(self.worker(), name=f"planning-worker-{index}")
            for index in range(self.settings.planning_concurrency)
        ]

    async def submit(self, caller: str, key: str, request: AgentRequest) -> tuple[str, bool]:
        if not self.settings.llm_enabled:
            raise ServiceError("AGENT_DISABLED", 503)
        if self.stopping:
            raise ServiceError("PLANNING_CLOSED", 503)
        if not self.service.ready or self.failed or not self.db.healthy:
            raise ServiceError("STATE_UNAVAILABLE", 503)
        if caller not in self.settings.callers:
            raise ServiceError("FORBIDDEN", 403)
        if not key or len(key) > 200 or any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise ServiceError("INVALID_REQUEST", 400)
        # 请求摘要保留调用方原始省略语义，输出存储另外冻结，重放不受配置变更影响。
        digest = hashlib.sha256(encode(request.model_dump()).encode()).hexdigest()
        frozen = request.model_copy(deep=True)
        frozen.output_policy.storage_id = (
            frozen.output_policy.storage_id
            or self.settings.callers[caller].default_output_storage
        )
        storage_id = frozen.output_policy.storage_id
        assert storage_id is not None
        async def create(tx: Transaction):
            existing = await tx.one(
                "SELECT id,request_hash FROM agent_requests WHERE caller=? AND message_key=?",
                (caller, key),
            )
            if existing:
                if existing["request_hash"] != digest:
                    raise ServiceError("IDEMPOTENCY_CONFLICT", 409)
                return existing["id"], False
            storage = self.settings.storages.get(storage_id)
            if storage is None or storage.role != "output" or caller not in storage.callers:
                raise ServiceError("FORBIDDEN", 403)
            if frozen.output_policy.retention_seconds > self.settings.max_retention_seconds:
                raise ServiceError("INVALID_REQUEST", 422)
            pending = await tx.one(
                "SELECT count(*) n,COALESCE(sum(caller=?),0) own FROM agent_requests "
                "WHERE state IN ('queued','planning','planned','submitting','canceling')", (caller,),
            )
            assert pending is not None
            if (pending["n"] >= self.settings.max_agent_requests
                    or pending["own"] >= self.settings.callers[caller].max_queued_jobs):
                raise ServiceError("QUOTA_EXCEEDED", 429, True)
            now = time.time()
            identifier = uid("agent")
            await tx.execute(
                "INSERT INTO agent_requests(id,caller,message_key,request_hash,request,"
                "state,created,deadline) VALUES(?,?,?,?,?,'queued',?,?)",
                (identifier, caller, key, digest, encode(frozen.model_dump()), now,
                 now + self.settings.planning_timeout_seconds),
            )
            return identifier, True

        result = await self.db.run(create)
        self.wake.set()
        return result

    async def get(self, caller: str, identifier: str) -> dict:
        row = await self.db.one(
            "SELECT * FROM agent_requests WHERE id=? AND caller=?", (identifier, caller)
        )
        if row is None:
            raise ServiceError("NOT_FOUND", 404)
        outcome = json.loads(row["outcome"]) if row["outcome"] else {}
        job = await self.service.state.job(caller, row["job_id"]) if row["job_id"] else None
        status = job["status"] if job else row["state"]
        if status == "submitting":
            status = "planned"
        return AgentTaskResult.model_validate({
            "agent_task_id": identifier,
            "status": status,
            "created_at": timestamp(row["created"]),
            "planning_deadline_at": timestamp(row["deadline"]),
            "decision": outcome.get("decision"),
            "message": outcome.get("message"),
            "plan": outcome.get("plan"),
            "job_id": row["job_id"],
            "job": job,
            "error": job["error"] if job else json.loads(row["error"]) if row["error"] else None,
        }).model_dump()

    async def cancel(self, caller: str, identifier: str) -> dict:
        async def request(tx: Transaction):
            row = await tx.one(
                "SELECT * FROM agent_requests WHERE id=? AND caller=?", (identifier, caller)
            )
            if row is None:
                raise ServiceError("NOT_FOUND", 404)
            if row["state"] in PLANNING_TERMINAL:
                return None, False
            if row["cancel_requested"] and not row["job_id"]:
                return None, False
            if row["job_id"]:
                changed = await self.service.state.cancel_in_transaction(tx, caller, row["job_id"])
                if changed:
                    await tx.execute(
                        "UPDATE agent_requests SET cancel_requested=1 WHERE id=?", (identifier,)
                    )
                return row["job_id"], False
            await tx.execute(
                "UPDATE agent_requests SET cancel_requested=1,state='canceling' WHERE id=?",
                (identifier,),
            )
            if row["state"] in {"queued", "planned"}:
                await self._terminal(tx, identifier, "canceled", ServiceError("CANCELED"))
                return None, False
            return None, True

        job_id, stop_planner = await self.db.run(request)
        if job_id:
            self.service.scheduler.notify_cancel(job_id)
        if stop_planner and (active := self.active.get(identifier)) is not None:
            active.cancel()
        self.wake.set()
        return await self.get(caller, identifier)

    async def _terminal(
        self, tx: Transaction, identifier: str, status: str, error: ServiceError | None = None,
    ):
        now = time.time()
        await tx.execute(
            "UPDATE agent_requests SET state=?,request=NULL,terminal_at=?,expires_at=?,error=? "
            "WHERE id=? AND job_id IS NULL",
            (status, now, now + self.settings.idempotency_seconds,
             encode(error.public()) if error else None, identifier),
        )

    async def recover(self):
        async def restore(tx: Transaction):
            rows = await tx.all(
                "SELECT * FROM agent_requests WHERE state IN "
                "('queued','planning','planned','submitting','canceling')"
            )
            now = time.time()
            for row in rows:
                identifier = row["id"]
                if row["cancel_requested"]:
                    await self._terminal(tx, identifier, "canceled", ServiceError("CANCELED"))
                elif row["state"] == "planning":
                    await self._terminal(
                        tx, identifier, "failed", ServiceError("PLANNING_INTERRUPTED", 503)
                    )
                elif row["deadline"] <= now:
                    await self._terminal(
                        tx, identifier, "failed", ServiceError("PLANNING_TIMEOUT", 504)
                    )
                elif not self.settings.llm_enabled:
                    await self._terminal(tx, identifier, "failed", ServiceError("AGENT_DISABLED", 503))
                elif row["state"] == "submitting":
                    await tx.execute(
                        "UPDATE agent_requests SET state='planned' WHERE id=?", (identifier,)
                    )

        await self.db.run(restore)
        await self.gc()

    async def _claim(self) -> dict | None:
        async def claim(tx: Transaction):
            now = time.time()
            for row in await tx.all(
                "SELECT * FROM agent_requests WHERE state IN ('queued','planned') ORDER BY created,id"
            ):
                if row["deadline"] <= now:
                    await self._terminal(
                        tx, row["id"], "failed", ServiceError("PLANNING_TIMEOUT", 504)
                    )
                    continue
                state = "planning" if row["state"] == "queued" else "submitting"
                await tx.execute(
                    "UPDATE agent_requests SET state=? WHERE id=?", (state, row["id"])
                )
                return row | {"state": state}
            return None

        return await self.db.run(claim)

    async def _save(self, identifier: str, outcome: PlanningOutcome, job_hash: str | None):
        # 模型自由文本可能复述描述中的凭据或个人信息；公开与持久化说明只取受控模板。
        outcome = outcome.model_copy(update={"message": {
            "execute": "已生成受支持的媒体处理计划。",
            "needs_input": "请补充期望的媒体操作和输出要求，再使用新幂等键提交。",
            "unsupported": "当前请求超出已支持的媒体操作范围，请查看能力清单后重新提交。",
        }[outcome.decision]})
        async def save(tx: Transaction):
            row = await tx.one("SELECT * FROM agent_requests WHERE id=?", (identifier,))
            if row is None:
                raise ServiceError("NOT_FOUND", 404)
            if row["cancel_requested"]:
                raise ServiceError("CANCELED", 409)
            if row["deadline"] <= time.time():
                raise ServiceError("PLANNING_TIMEOUT", 504)
            if row["state"] != "planning":
                raise ServiceError("STATE_CONFLICT", 409)
            await tx.execute(
                "UPDATE agent_requests SET outcome=?,job_hash=? WHERE id=?",
                (encode(outcome.model_dump()), job_hash, identifier),
            )
            if outcome.decision == "execute":
                await tx.execute(
                    "UPDATE agent_requests SET state='planned' WHERE id=?", (identifier,)
                )
            else:
                await self._terminal(tx, identifier, outcome.decision)

        await self.db.run(save)
        self.wake.set()

    async def _fail(self, identifier: str, error: ServiceError):
        async def fail(tx: Transaction):
            row = await tx.one("SELECT * FROM agent_requests WHERE id=?", (identifier,))
            if row is None or row["job_id"] or row["state"] in PLANNING_TERMINAL:
                return
            if row["cancel_requested"]:
                await self._terminal(tx, identifier, "canceled", ServiceError("CANCELED"))
            elif (error.code == "PLANNING_INTERRUPTED"
                  and row["state"] in {"planned", "submitting"}):
                # 计划已保存，关闭只撤销本地提交等待；重启可安全提交同一计划。
                await tx.execute("UPDATE agent_requests SET state='planned' WHERE id=?", (identifier,))
            else:
                await self._terminal(tx, identifier, "failed", error)

        await self.db.run(fail)

    async def _process(self, row: dict):
        identifier = row["id"]
        try:
            current = await self.db.one("SELECT * FROM agent_requests WHERE id=?", (identifier,))
            if current is None:
                return
            if current["cancel_requested"]:
                raise ServiceError("CANCELED", 409)
            async with asyncio.timeout(max(0, row["deadline"] - time.time())):
                request = AgentRequest.model_validate_json(row["request"])
                if row["state"] == "planning":
                    assert self.planner is not None
                    outcome = await self.planner.plan(request, capability_document(self.service))
                    outcome = PlanningOutcome.model_validate(outcome)
                    job = outcome.plan.bind(request) if outcome.plan else None
                    if job:
                        self.service.media.validate_request(job, self.service.engine)
                    await self._save(identifier, outcome, job.digest() if job else None)
                else:
                    outcome = PlanningOutcome.model_validate_json(row["outcome"])
                    if outcome.plan is None:
                        raise ServiceError("STATE_CONFLICT", 409)
                    await self.service.accept(
                        row["caller"], f"agent:{identifier}", outcome.plan.bind(request),
                        agent_request_id=identifier,
                    )
        except TimeoutError:
            await self._fail(identifier, ServiceError("PLANNING_TIMEOUT", 504))
        except ValidationError:
            await self._fail(identifier, ServiceError("PLANNING_INVALID_OUTPUT", 422))
        except ServiceError as error:
            await self._fail(identifier, error)
        except Exception:
            await self._fail(identifier, ServiceError("PLANNING_FAILED", 502))

    async def worker(self):
        try:
            while not self.stopping:
                row = await self._claim()
                if row is None:
                    try:
                        await asyncio.wait_for(
                            self.wake.wait(), self.settings.scheduler_interval_seconds
                        )
                    except TimeoutError:
                        pass
                    self.wake.clear()
                    continue
                task = asyncio.create_task(self._process(row))
                self.active[row["id"]] = task
                try:
                    if self.stopping:
                        task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                finally:
                    # 取消可能发生在协程首次运行前，或错误收尾正等待事务时。
                    # 由领取者统一收尾，不能只依赖 _process 内部的 except。
                    if task.cancelled():
                        await wait_settled(asyncio.create_task(
                            self._fail(row["id"], ServiceError("PLANNING_INTERRUPTED", 503))
                        ))
                    self.active.pop(row["id"], None)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failed = True
            self.stopping = True
            self.service.ready = False
            for task in self.active.values():
                task.cancel()
            logger.error('{"event":"planning_worker_failed","code":"STATE_UNAVAILABLE"}')

    async def gc(self):
        async def collect(tx: Transaction):
            await tx.execute(
                "UPDATE agent_requests SET terminal_at=(SELECT terminal_at FROM jobs WHERE id=job_id),"
                "expires_at=(SELECT terminal_at+? FROM jobs WHERE id=job_id) "
                "WHERE state='submitted' AND terminal_at IS NULL AND job_id IN "
                "(SELECT id FROM jobs WHERE status IN ('succeeded','partial','failed','canceled'))",
                (self.settings.idempotency_seconds,),
            )
            await tx.execute(
                "DELETE FROM agent_requests WHERE terminal_at IS NOT NULL AND expires_at<=?",
                (time.time(),),
            )

        await self.db.run(collect)

    async def close(self):
        self.stopping = True
        self.wake.set()
        for task in self.active.values():
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*self.workers)
        if self.planner is not None:
            await self.planner.close()
