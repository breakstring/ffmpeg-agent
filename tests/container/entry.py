"""只用于本地容器故障验收的持久检查点，不进入服务镜像。"""

import asyncio
import json
import os
import sys
from contextlib import suppress
from pathlib import Path

import uvicorn

from media_service.api import create_app
from media_service.config import Settings
from media_service.service import Service


def marker(directory: Path, name: str, **values) -> bool:
    try:
        fd = os.open(directory / f"fault-{name}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as handle:
        json.dump({"pid": os.getpid(), **values}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def main():
    os.umask(0o077)
    mode = sys.argv[1]
    settings = Settings.from_file()
    service = Service(settings)

    async def checkpoint(name, artifact_id):
        if name == mode and marker(settings.state_dir, mode, artifact_id=artifact_id):
            await asyncio.Event().wait()

    if mode in {"intent", "staged", "renamed", "committed"}:
        service.publisher.checkpoint_hook = checkpoint
    if mode == "cancel_gate":
        stop = service.executor._stop_process

        async def held_stop(process):
            row = await service.db.one("SELECT id FROM jobs WHERE cancel_requested=1 LIMIT 1")
            if process.returncode is None and row and marker(
                settings.state_dir, mode, child_pid=process.pid, job_id=row["id"]
            ):
                await asyncio.Event().wait()
            await stop(process)

        service.executor._stop_process = held_stop
    app = create_app(settings, service)
    if mode == "hot_guard":

        @app.post("/_fault/release_lock")
        async def release_lock():
            service.ready = False
            await service.scheduler.stop()
            if service.maintenance:
                service.maintenance.cancel()
                with suppress(asyncio.CancelledError):
                    await service.maintenance
            # 故意不调用 runtime.clean_stop；测试同一 PID 1 代际的二次启动门禁。
            await service.db.close()
            return {"released": service.db.lock_fd is None, "clean_stop": False}

    uvicorn.run(app, host=settings.host, port=settings.port, access_log=False, log_level="error")


if __name__ == "__main__":
    main()
