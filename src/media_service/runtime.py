import json
import os
import sys
import time
from pathlib import Path

from media_service.config import Settings
from media_service.database import Transaction
from media_service.errors import ServiceError
from media_service.state import State, encode


def container_generation() -> str:
    if sys.platform != "linux" or os.getpid() == 1:
        raise RuntimeError("container mode requires Linux and a container init")
    stat = Path("/proc/1/stat").read_text()
    start_ticks = stat[stat.rfind(")") + 2 :].split()[19]
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    namespace = os.stat("/proc/1/ns/pid").st_ino
    return f"{boot}:{namespace}:{start_ticks}"


class Runtime:
    def __init__(self, state: State, settings: Settings):
        self.state = state
        self.settings = settings

    async def begin(self):
        generation = (
            container_generation() if self.settings.runtime_mode == "container" else "local"
        )

        async def guard(tx: Transaction):
            prior = await tx.one("SELECT value FROM meta WHERE key='runtime'")
            if prior:
                previous = json.loads(prior["value"])
                if not previous["clean"] and (
                    generation == "local" or previous["generation"] == generation
                ):
                    raise RuntimeError(
                        "previous execution stop is unconfirmed; restart the whole container"
                    )
            await tx.execute(
                "INSERT INTO meta(key,value) VALUES('runtime',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (encode({"generation": generation, "clean": False}),),
            )
            # 此时已由正常关闭或新的容器init代际确认旧发送/执行者停止。
            await tx.execute("DELETE FROM refs WHERE kind='send'")
            await tx.execute(
                "UPDATE attempts SET status='stopped',stopped=? WHERE status='running'",
                (time.time(),),
            )

        await self.state.db.run(guard)

    async def recover_jobs(self, engine: dict):
        async def recover(tx: Transaction):
            jobs = await tx.all(
                "SELECT * FROM jobs WHERE status NOT IN ('succeeded','partial','failed','canceled')"
            )
            for job in jobs:
                error = None
                if job["cancel_requested"]:
                    await self.state._finish(
                        tx, job["id"], "canceled", ServiceError("CANCELED").public()
                    )
                    continue
                steps = await tx.all("SELECT id,status FROM steps WHERE job_id=?", (job["id"],))
                if steps and all(s["status"] not in {"pending", "running"} for s in steps):
                    # 已提交的步骤只需原子收尾，不消耗新的执行尝试或重新读取输入。
                    by_id = {s["id"]: s["status"] for s in steps}
                    statuses = [by_id[b["step"]] for b in json.loads(job["public_definition"])["outputs"].values()]
                    status = "succeeded" if all(s == "succeeded" for s in statuses) else (
                        "partial" if any(s in {"succeeded", "partial"} for s in statuses) else "failed"
                    )
                    await self.state._finish(tx, job["id"], status, None)
                    continue
                if job["deadline"] <= time.time():
                    error = "TASK_TIMEOUT"
                elif json.loads(job["engine"]) != engine:
                    error = "ENGINE_INCOMPATIBLE"
                elif job["attempt_count"] >= self.settings.max_attempts:
                    error = "EXECUTION_INTERRUPTED"
                elif job["caller"] not in self.settings.callers:
                    error = "FORBIDDEN"
                if error:
                    await self.state._finish(tx, job["id"], "failed", ServiceError(error).public())
                    continue
                await tx.execute(
                    "UPDATE jobs SET status='queued',phase=NULL,current_attempt=NULL WHERE id=?",
                    (job["id"],),
                )
                await tx.execute(
                    "UPDATE steps SET status='pending' WHERE job_id=? AND status='running'",
                    (job["id"],),
                )

        await self.state.db.run(recover)

    async def clean_stop(self):
        async def mark(tx: Transaction):
            row = await tx.one("SELECT value FROM meta WHERE key='runtime'")
            if row:
                value = json.loads(row["value"])
                value["clean"] = True
                await tx.execute("UPDATE meta SET value=? WHERE key='runtime'", (encode(value),))

        await self.state.db.run(mark)
