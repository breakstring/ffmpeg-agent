"""容器内只读证据及明确命名的测试进程控制；不输出请求和配置内容。"""

import asyncio
import hashlib
import json
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path

STATE = Path("/var/lib/ffmpeg-agent/state")


def process(pid: int):
    root = Path(f"/proc/{pid}")
    try:
        stat = (root / "stat").read_text()
        arguments = (root / "cmdline").read_bytes().split(b"\0")
        return {
            "pid": pid,
            "state": stat[stat.rfind(")") + 2 :].split()[0],
            "media": Path(os.fsdecode(arguments[0])).name == "ffmpeg" and b"-i" in arguments,
            "instance_lock_inherited": any(
                os.readlink(fd).endswith("/instance.lock") for fd in (root / "fd").iterdir()
            ),
        }
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def snapshot(job_id: str):
    with sqlite3.connect(f"file:{STATE / 'state.sqlite'}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        job = dict(connection.execute(
            "SELECT id,status,cancel_requested,attempt_count,inputs_released FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone())
        attempts = [dict(row) for row in connection.execute(
            "SELECT number,status FROM attempts WHERE job_id=? ORDER BY number", (job_id,)
        )]
        artifacts = [dict(row) for row in connection.execute(
            "SELECT id,state,directory,staging FROM artifacts WHERE job_id=? ORDER BY created",
            (job_id,),
        )]
        runtime = json.loads(connection.execute("SELECT value FROM meta WHERE key='runtime'").fetchone()[0])
    for artifact in artifacts:
        artifact["directory_exists"] = (Path("/media/output") / artifact.pop("directory")).exists()
        artifact["staging_exists"] = (Path("/media/output") / artifact.pop("staging")).exists()
    return {"job": job, "attempts": attempts, "artifacts": artifacts, "runtime": runtime}


async def try_start():
    from media_service.config import Settings
    from media_service.service import Service

    service = Service(Settings.from_file())
    try:
        await service.start()
    except RuntimeError as error:
        reason = {
            "previous execution stop is unconfirmed; restart the whole container": "runtime_guard",
            "state directory already has an application instance": "instance_lock",
        }.get(str(error), "unexpected_runtime_error")
        print(json.dumps({"rejected": True, "reason": reason}), flush=True)
        return 78
    await service.close()
    print(json.dumps({"rejected": False}), flush=True)
    return 0


def main():
    command = sys.argv[1]
    if command == "freeze_media":
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            for path in Path("/proc").iterdir():
                if path.name.isdigit():
                    info = process(int(path.name))
                    if info and info["media"]:
                        os.kill(info["pid"], signal.SIGSTOP)
                        info = process(info["pid"])
                        if info and info["state"] in {"T", "t"}:
                            print(json.dumps(info))
                            return
            time.sleep(0.01)
        raise SystemExit("real_ffmpeg_not_observed")
    if command == "marker":
        path = STATE / f"fault-{sys.argv[2]}.json"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                print(json.dumps(json.loads(path.read_text())))
                return
            except (FileNotFoundError, ValueError):
                time.sleep(0.05)
        raise SystemExit("fault_marker_not_observed")
    elif command == "snapshot":
        print(json.dumps(snapshot(sys.argv[2])))
    elif command == "process":
        print(json.dumps(process(int(sys.argv[2]))))
    elif command == "kill_api":
        pid = json.loads((STATE / "fault-api.json").read_text())["pid"]
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        assert b"/fault-tests/entry.py" in args
        os.kill(pid, signal.SIGKILL)
        print(json.dumps({"api_pid": pid, "signal": "SIGKILL"}))
    elif command == "try_start":
        raise SystemExit(asyncio.run(try_start()))
    elif command == "digest":
        root = Path("/media/output")
        path = root / sys.argv[2]
        assert path.resolve().is_relative_to(root)
        print(json.dumps({"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}))
    else:
        raise SystemExit("unknown_test_command")


if __name__ == "__main__":
    main()
