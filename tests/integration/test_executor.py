import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from media_service.config import Caller, Settings, Storage
from media_service.errors import ServiceError
from media_service.executor import Executor
from media_service.files import file_job, safe_open

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.fixture
def executor(tmp_path: Path) -> Executor:
    for name in ["state", "work", "input", "output"]:
        (tmp_path / name).mkdir()
    settings = Settings(
        state_dir=tmp_path / "state",
        work_dir=tmp_path / "work",
        storages={"out": Storage(root=tmp_path / "output", role="output", callers=["test"])},
        callers={
            "test": Caller(tokens=["test-executor-credential-0000"], default_output_storage="out")
        },
        disk_reserve_bytes=0,
        stop_grace_seconds=0.15,
    )
    return Executor(settings)


async def execute(executor: Executor, code: str, **options):
    return await executor.run(
        [sys.executable, "-c", code],
        deadline=options.pop("deadline", time.time() + 10),
        cancel=options.pop("cancel", asyncio.Event()),
        **options,
    )


async def wait_file(path: Path):
    end = time.monotonic() + 4
    while time.monotonic() < end:
        if await file_job(lambda _: path.exists()):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("child did not report ready")


def assert_stopped(pid: int):
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_real_ffprobe_and_ffmpeg_read_verified_fd(executor: Executor, tmp_path: Path):
    fd = safe_open(FIXTURES, "ffmpeg_hevc_640x360.mp4", 100_000)
    try:
        inputs = [
            "-protocol_whitelist",
            "fd",
            "-format_whitelist",
            "mov",
            "-fd",
            str(fd),
            "-i",
            "fd:",
        ]
        result = await executor.run(
            ["ffprobe", "-v", "error", *inputs, "-show_streams", "-of", "json"],
            pass_fds=(fd,),
            deadline=time.time() + 10,
            cancel=asyncio.Event(),
        )
        assert result.returncode == 0
        assert json.loads(result.stdout)["streams"][0]["codec_name"] == "hevc"
        os.lseek(fd, 0, os.SEEK_SET)
        image = tmp_path / "output" / "frame.png"
        result = await executor.run(
            ["ffmpeg", "-v", "error", *inputs, "-frames:v", "1", str(image)],
            pass_fds=(fd,),
            deadline=time.time() + 10,
            cancel=asyncio.Event(),
            monitor_dir=image.parent,
        )
        assert result.returncode == 0 and image.read_bytes().startswith(b"\x89PNG")
        assert executor.active_pids == set()
    finally:
        os.close(fd)


async def test_large_stderr_drains_and_preserves_exit_code(executor: Executor):
    result = await execute(
        executor, "import sys; sys.stderr.write('x'*3000000+'THE_END'); sys.exit(13)"
    )
    assert result.returncode == 13 and len(result.stderr) == 65536
    assert result.stderr.endswith(b"THE_END")


async def test_stdout_is_bounded_and_process_stops(executor: Executor):
    with pytest.raises(ServiceError, match="OUTPUT_LIMIT"):
        await execute(
            executor,
            "import sys,time; sys.stdout.buffer.write(b'x'*10000000); sys.stdout.flush(); time.sleep(20)",
        )
    assert not executor.active_pids


async def test_ignore_term_is_killed_and_reaped(executor: Executor, tmp_path: Path):
    ready = tmp_path / "ready"
    cancel = asyncio.Event()
    code = f"import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path({str(ready)!r}).touch(); time.sleep(30)"
    task = asyncio.create_task(execute(executor, code, cancel=cancel))
    await wait_file(ready)
    pid = next(iter(executor.active_pids))
    cancel.set()
    with pytest.raises(ServiceError, match="CANCELED"):
        await asyncio.wait_for(task, 3)
    assert not executor.active_pids
    assert_stopped(pid)


async def test_repeated_coroutine_cancel_waits_until_stopped(executor: Executor, tmp_path: Path):
    ready = tmp_path / "ready"
    code = f"import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path({str(ready)!r}).touch(); time.sleep(30)"
    task = asyncio.create_task(execute(executor, code))
    await wait_file(ready)
    pid = next(iter(executor.active_pids))
    task.cancel()
    await asyncio.sleep(0.025)
    task.cancel()
    assert not task.done()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not executor.active_pids
    assert_stopped(pid)


async def test_deadline_stops_process(executor: Executor):
    with pytest.raises(ServiceError, match="TASK_TIMEOUT"):
        await execute(executor, "import time; time.sleep(20)", deadline=time.time() + 0.1)
    assert not executor.active_pids


async def test_normal_exit_stops_living_descendant(executor: Executor, tmp_path: Path):
    marker = tmp_path / "child"
    code = (
        "import os,time,pathlib,signal; pid=os.fork(); "
        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid())) if pid == 0 else None; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN) if pid == 0 else None; "
        "time.sleep(20) if pid == 0 else time.sleep(0.1)"
    )
    result = await asyncio.wait_for(execute(executor, code), 4)
    assert result.returncode == 0 and not executor.active_pids
    child = int(marker.read_text())
    try:
        os.kill(child, 0)
    except ProcessLookupError:
        return
    if sys.platform == "linux":
        snapshot = await file_job(lambda _: Path(f"/proc/{child}/stat").read_text())
        assert snapshot.rsplit(")", 1)[1].split()[0] == "Z"
    else:
        pytest.fail("descendant remains alive")


async def test_monitored_output_and_disk_limits(executor: Executor, tmp_path: Path):
    output = tmp_path / "output"
    executor.settings.max_output_bytes = 5
    code = f"import pathlib,time; pathlib.Path({str(output / 'large')!r}).write_bytes(b'x'*100); time.sleep(10)"
    with pytest.raises(ServiceError, match="OUTPUT_LIMIT"):
        await execute(executor, code, monitor_dir=output)
    assert not executor.active_pids
    executor.settings.max_output_bytes = 1000
    executor.settings.disk_reserve_bytes = 2**63
    with pytest.raises(ServiceError, match="DISK_FULL"):
        await execute(executor, "pass", monitor_dir=output)
    assert not executor.active_pids


async def test_waiting_slot_respects_cancel(executor: Executor, tmp_path: Path):
    ready = tmp_path / "ready"
    first_cancel = asyncio.Event()
    first = asyncio.create_task(
        execute(
            executor,
            f"import pathlib,time; pathlib.Path({str(ready)!r}).touch(); time.sleep(20)",
            cancel=first_cancel,
        )
    )
    await wait_file(ready)
    queued_cancel = asyncio.Event()
    second = asyncio.create_task(execute(executor, "raise SystemExit(99)", cancel=queued_cancel))
    queued_cancel.set()
    with pytest.raises(ServiceError, match="CANCELED"):
        await second
    assert len(executor.active_pids) == 1
    first_cancel.set()
    with pytest.raises(ServiceError, match="CANCELED"):
        await first


@pytest.mark.parametrize("count", [1, 2, 4])
async def test_executor_slots_bound_live_processes(executor: Executor, count: int):
    bounded = Executor(executor.settings.model_copy(update={"worker_concurrency": count}))
    tasks = [
        asyncio.create_task(execute(bounded, "import time; time.sleep(0.2)"))
        for _ in range(count + 2)
    ]
    peak = 0
    while not all(task.done() for task in tasks):
        peak = max(peak, len(bounded.active_pids))
        assert len(bounded.active_pids) <= count
        await asyncio.sleep(0.005)
    results = await asyncio.gather(*tasks)
    assert peak == count and all(result.returncode == 0 for result in results)


async def test_shutdown_waits_for_active_processes(executor: Executor, tmp_path: Path):
    marker = tmp_path / "started"
    task = asyncio.create_task(
        execute(
            executor,
            f"import pathlib,time; pathlib.Path({str(marker)!r}).touch(); time.sleep(20)",
        )
    )
    await wait_file(marker)
    pid = next(iter(executor.active_pids))
    await executor.stop_all()
    with pytest.raises(ServiceError, match="CANCELED"):
        await task
    assert not executor.active_pids
    assert_stopped(pid)
