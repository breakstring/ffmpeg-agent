import asyncio
import hashlib
import os
import threading
from pathlib import Path

import pytest

from media_service.errors import ServiceError
from media_service.files import copy_verified, file_job, remove_owned_dir, safe_open, sha256_fd


def test_safe_open_keeps_inode_and_hash_offset(tmp_path: Path):
    source = tmp_path / "input.bin"
    source.write_bytes(b"old-data")
    descriptor = safe_open(tmp_path, source.name, 100)
    try:
        source.rename(tmp_path / "original.bin")
        source.write_bytes(b"replaced")
        os.lseek(descriptor, 2, os.SEEK_SET)
        assert sha256_fd(descriptor, threading.Event()) == (
            8,
            hashlib.sha256(b"old-data").hexdigest(),
        )
        assert os.lseek(descriptor, 0, os.SEEK_CUR) == 2
        assert os.read(descriptor, 6) == b"d-data"
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("key", ["", ".", "..", "../outside", "/etc/passwd", "a//b", "a/../b"])
def test_invalid_paths(tmp_path: Path, key: str):
    with pytest.raises(ServiceError, match="SOURCE_FORBIDDEN"):
        safe_open(tmp_path, key, 100)


def test_nonregular_symlink_and_size_rejection(tmp_path: Path):
    (tmp_path / "file").write_bytes(b"content")
    (tmp_path / "directory").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "file")
    (tmp_path / "dirlink").symlink_to(tmp_path / "directory", target_is_directory=True)
    os.mkfifo(tmp_path / "fifo")
    for key in ["directory", "link", "dirlink/file", "fifo"]:
        with pytest.raises(ServiceError, match="SOURCE_FORBIDDEN"):
            safe_open(tmp_path, key, 100)
    with pytest.raises(ServiceError, match="INPUT_TOO_LARGE"):
        safe_open(tmp_path, "file", 3)
    with pytest.raises(ServiceError, match="SOURCE_UNREACHABLE"):
        safe_open(tmp_path, "absent", 3)


def test_copy_validates_does_not_overwrite_and_cleans_failed_copy(tmp_path: Path):
    source = tmp_path / "input"
    source.write_bytes(b"content")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    target = tmp_path / "output"
    assert copy_verified(source, target, threading.Event(), expected_sha256=expected) == (
        7,
        expected,
    )
    with pytest.raises(FileExistsError):
        copy_verified(source, target, threading.Event())
    assert target.read_bytes() == b"content"
    failed = tmp_path / "failed"
    with pytest.raises(ServiceError, match="INPUT_CHANGED"):
        copy_verified(source, failed, threading.Event(), expected_size=8)
    assert not failed.exists()
    canceled = threading.Event()
    canceled.set()
    with pytest.raises(ServiceError, match="CANCELED"):
        copy_verified(source, failed, canceled)
    assert not failed.exists()


def test_owned_removal_is_scoped_and_does_not_follow_links(tmp_path: Path):
    owned = tmp_path / "owned"
    owned.mkdir()
    outside = tmp_path / "source"
    outside.mkdir()
    (outside / "keep").write_text("borrowed")
    (owned / "link").symlink_to(outside, target_is_directory=True)
    (owned / "nested").mkdir()
    (owned / "nested" / "delete").write_text("owned")
    assert remove_owned_dir(tmp_path, "owned", threading.Event())
    assert (outside / "keep").read_text() == "borrowed"
    assert not remove_owned_dir(tmp_path, "owned", threading.Event())
    for key in ["", ".", "..", "/"]:
        with pytest.raises(ServiceError, match="SOURCE_FORBIDDEN"):
            remove_owned_dir(tmp_path, key, threading.Event())


async def test_file_job_repeated_cancellation_waits_for_real_thread():
    started, release, stopped, saw_cancel = (threading.Event() for _ in range(4))

    def work(cancel: threading.Event):
        started.set()
        while not release.wait(0.005):
            if cancel.is_set():
                saw_cancel.set()
        stopped.set()

    task = asyncio.create_task(file_job(work))
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done() and not stopped.is_set() and saw_cancel.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


async def test_cancel_inflight_copy_waits_and_removes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source"
    source.write_bytes(b"source" * 200_000)
    target = tmp_path / "partial"
    inside_write, release = threading.Event(), threading.Event()
    original = os.write

    def blocked_write(fd: int, data):
        inside_write.set()
        assert release.wait(3)
        return original(fd, data)

    monkeypatch.setattr(os, "write", blocked_write)
    task = asyncio.create_task(file_job(lambda cancel: copy_verified(source, target, cancel)))
    assert await asyncio.to_thread(inside_write.wait, 3)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not target.exists()
    assert source.stat().st_size == 1_200_000


async def test_file_job_maximum_four_active_threads():
    guard = threading.Lock()
    release = threading.Event()
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()
    active = peak = 0

    def work(cancel: threading.Event):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
            if peak == 4:
                loop.call_soon_threadsafe(ready.set)
        try:
            while not release.wait(0.005):
                assert not cancel.is_set()
        finally:
            with guard:
                active -= 1

    tasks = [asyncio.create_task(file_job(work)) for _ in range(9)]
    await asyncio.wait_for(ready.wait(), 3)
    assert peak == 4
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 4 and active == 0
