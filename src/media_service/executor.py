import asyncio
import os
import shutil
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from media_service.config import Settings
from media_service.errors import ServiceError
from media_service.files import check_cancel, directory_size, file_job

_STDOUT_LIMIT = 8 * 1024 * 1024
_STDERR_LIMIT = 64 * 1024


@dataclass(frozen=True)
class ProcessResult:
    stdout: bytes
    stderr: bytes
    returncode: int


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 探测权限错误不能证明进程组停止，保持引用并重试。
        return True
    if sys.platform != "linux":
        return True
    # 容器 init 可能尚未回收孤儿僵尸；僵尸不再持有文件，不能误报为活执行者。
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid and fields[0] not in {"Z", "X"}:
                return True
        except FileNotFoundError, ProcessLookupError:
            continue
    return False


class Executor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.active_pids: set[int] = set()
        # 子进程继承实例锁：API意外退出但媒体进程仍存活时，不能接管同一状态卷。
        self.guard_fd: int | None = None
        self._slots = asyncio.Semaphore(settings.worker_concurrency)
        self._stopping = asyncio.Event()
        self._runs: set[asyncio.Task[ProcessResult]] = set()

    async def run(
        self,
        args: list[str],
        *,
        pass_fds: tuple[int, ...] = (),
        deadline: float,
        cancel: asyncio.Event,
        monitor_dir: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> ProcessResult:
        aborted = asyncio.Event()
        owned = asyncio.create_task(
            self._run(args, pass_fds, deadline, cancel, aborted, monitor_dir, env)
        )
        self._runs.add(owned)
        owned.add_done_callback(self._runs.discard)
        try:
            return await asyncio.shield(owned)
        except asyncio.CancelledError:
            aborted.set()
            # 保留启动和收尾任务的所有权，防止 create_subprocess_exec 的取消窗口漏进程。
            while not owned.done():
                try:
                    await asyncio.shield(owned)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if owned.done() and not owned.cancelled():
                owned.exception()
            raise

    def _check_stop(self, deadline: float, cancel: asyncio.Event, aborted: asyncio.Event) -> None:
        if cancel.is_set() or aborted.is_set() or self._stopping.is_set():
            raise ServiceError("CANCELED")
        if time.time() >= deadline:
            raise ServiceError("TASK_TIMEOUT")

    async def _run(
        self,
        args: list[str],
        pass_fds: tuple[int, ...],
        deadline: float,
        cancel: asyncio.Event,
        aborted: asyncio.Event,
        monitor_dir: Path | None,
        env: dict[str, str] | None,
    ) -> ProcessResult:
        while True:
            self._check_stop(deadline, cancel, aborted)
            try:
                await asyncio.wait_for(self._slots.acquire(), timeout=0.05)
                break
            except TimeoutError:
                continue
        process: asyncio.subprocess.Process | None = None
        drains: list[asyncio.Task[None]] = []
        stdout, stderr = bytearray(), bytearray()
        output_exceeded = asyncio.Event()
        try:
            self._check_stop(deadline, cancel, aborted)
            if monitor_dir is not None:
                await self._monitor(monitor_dir)
            self._check_stop(deadline, cancel, aborted)
            try:
                process = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    pass_fds=tuple(dict.fromkeys((*pass_fds, self.guard_fd)))
                    if self.guard_fd is not None
                    else pass_fds,
                    start_new_session=True,
                    env=env,
                )
            except OSError as error:
                raise ServiceError("ENGINE_INCOMPATIBLE") from error
            self.active_pids.add(process.pid)
            assert process.stdout is not None and process.stderr is not None
            drains = [
                asyncio.create_task(self._drain(process.stdout, stdout, False, output_exceeded)),
                asyncio.create_task(self._drain(process.stderr, stderr, True, output_exceeded)),
            ]
            last_monitor = 0.0
            while process.returncode is None:
                self._check_stop(deadline, cancel, aborted)
                if output_exceeded.is_set():
                    raise ServiceError("OUTPUT_LIMIT")
                for drain in drains:
                    if drain.done():
                        drain.result()
                if monitor_dir is not None and time.monotonic() - last_monitor >= 0.1:
                    await self._monitor(monitor_dir)
                    last_monitor = time.monotonic()
                await asyncio.sleep(0.025)
            # 主进程退出不等于其派生进程停止，先清空进程组，再等管道EOF和reap。
            await self._stop_process(process)
            await asyncio.gather(*drains)
            self._check_stop(deadline, cancel, aborted)
            if output_exceeded.is_set():
                raise ServiceError("OUTPUT_LIMIT")
            if monitor_dir is not None:
                await self._monitor(monitor_dir)
            assert process.returncode is not None
            return ProcessResult(bytes(stdout), bytes(stderr), process.returncode)
        finally:
            if process is not None:
                await self._stop_process(process)
                if drains:
                    await asyncio.gather(*drains, return_exceptions=True)
                self.active_pids.discard(process.pid)
            self._slots.release()

    async def _drain(
        self,
        stream: asyncio.StreamReader,
        target: bytearray,
        rolling: bool,
        output_exceeded: asyncio.Event,
    ) -> None:
        while data := await stream.read(64 * 1024):
            if rolling:
                target.extend(data)
                if len(target) > _STDERR_LIMIT:
                    del target[:-_STDERR_LIMIT]
            elif not output_exceeded.is_set():
                remaining = _STDOUT_LIMIT - len(target)
                target.extend(data[:remaining])
                if len(data) > remaining:
                    output_exceeded.set()

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        pgid = process.pid
        if _group_alive(pgid):
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError, PermissionError:
                pass
            end = time.monotonic() + self.settings.stop_grace_seconds
            while _group_alive(pgid) and time.monotonic() < end:  # noqa: ASYNC110 - 进程组无可await的通知
                await asyncio.sleep(0.025)
            if _group_alive(pgid):
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError, PermissionError:
                    pass
                # KILL 也不等于退出；不可中断内核调用结束前保持执行引用。
                while _group_alive(pgid):  # noqa: ASYNC110 - 核对操作系统事实，不能用事件代替
                    await asyncio.sleep(0.025)
        await process.wait()

    async def _monitor(self, output_dir: Path) -> None:
        def inspect(event: threading.Event) -> None:
            if directory_size(output_dir, event) > self.settings.max_output_bytes:
                raise ServiceError("OUTPUT_LIMIT")
            if directory_size(self.settings.work_dir, event) > self.settings.max_work_bytes:
                raise ServiceError("OUTPUT_LIMIT")
            check_cancel(event)
            if shutil.disk_usage(output_dir).free < self.settings.disk_reserve_bytes:
                raise ServiceError("DISK_FULL", retryable=True)

        await file_job(inspect)

    async def stop_all(self) -> None:
        self._stopping.set()
        runs = asyncio.gather(*tuple(self._runs), return_exceptions=True)
        canceled = False
        while not runs.done():
            try:
                await asyncio.shield(runs)
            except asyncio.CancelledError:
                canceled = True
        if canceled:
            raise asyncio.CancelledError
