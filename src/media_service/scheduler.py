import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

from media_service.state import State

logger = logging.getLogger("media_service")


class Scheduler:
    def __init__(self, state: State, execute: Callable[[dict, asyncio.Event], Awaitable[None]]):
        self.state = state
        self.execute = execute
        self.stopping = False
        self.failed = False
        self.wake = asyncio.Event()
        self.workers: list[asyncio.Task] = []
        self.active: dict[str, asyncio.Event] = {}

    def start(self):
        self.workers = [
            asyncio.create_task(self.worker(), name=f"media-worker-{n}")
            for n in range(self.state.settings.worker_concurrency)
        ]

    async def worker(self):
        try:
            while not self.stopping:
                job = await self.state.claim()
                if job is None:
                    try:
                        await asyncio.wait_for(
                            self.wake.wait(), self.state.settings.scheduler_interval_seconds
                        )
                    except TimeoutError:
                        pass
                    self.wake.clear()
                    continue
                cancel = asyncio.Event()
                self.active[job["id"]] = cancel
                heartbeat = asyncio.create_task(self.heartbeat(job))
                try:
                    # DB 是取消意图的真相，不能依赖内存事件与领取的先后顺序。
                    latest = await self.state.db.one(
                        "SELECT cancel_requested FROM jobs WHERE id=?", (job["id"],)
                    )
                    if latest is None or latest["cancel_requested"] or self.stopping:
                        cancel.set()
                    await self.execute(job, cancel)
                finally:
                    heartbeat.cancel()
                    with suppress(asyncio.CancelledError):
                        await heartbeat
                    self.active.pop(job["id"], None)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failed = True
            self.stopping = True
            for cancel in self.active.values():
                cancel.set()
            # 不输出含来源/路径的异常字符串。不可恢复失败由服务触发整容器退出。
            logger.error('{"event":"scheduler_failed","code":"STATE_UNAVAILABLE"}')

    async def heartbeat(self, job: dict):
        while True:
            await asyncio.sleep(self.state.settings.execution_lease_seconds / 3)
            await self.state.heartbeat(job["id"], job["current_attempt"])

    def notify_cancel(self, job_id: str):
        if job_id in self.active:
            self.active[job_id].set()
        self.wake.set()

    async def stop(self):
        self.stopping = True
        for event in self.active.values():
            event.set()
        self.wake.set()
        await asyncio.gather(*self.workers)
