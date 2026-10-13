import asyncio
import hashlib
import os
import stat
import threading
import weakref
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from media_service.errors import ServiceError

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="media-file")
_SLOTS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)
_CHUNK = 1024 * 1024


def check_cancel(event: threading.Event) -> None:
    if event.is_set():
        raise ServiceError("CANCELED")


async def file_job[T](fn: Callable[[threading.Event], T]) -> T:
    loop = asyncio.get_running_loop()
    slots = _SLOTS.setdefault(loop, asyncio.Semaphore(4))
    async with slots:
        event = threading.Event()
        future = loop.run_in_executor(_POOL, fn, event)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            event.set()
            # 取消等待者不能释放文件引用；包括第二次取消，都要等真实线程退出。
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if future.done() and not future.cancelled():
                future.exception()
            raise


def _parts(key: str) -> list[str]:
    parts = key.split("/")
    if not key or "\\" in key or "\0" in key or any(part in {"", ".", ".."} for part in parts):
        raise ServiceError("SOURCE_FORBIDDEN")
    return parts


def _open_parent(root: Path, key: str) -> tuple[int, str]:
    parts = _parts(key)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return directory, parts[-1]
    except BaseException:
        os.close(directory)
        raise


def safe_open(root: Path, key: str, max_bytes: int) -> int:
    directory = source = -1
    try:
        directory, leaf = _open_parent(root, key)
        source = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        metadata = os.fstat(source)
        if not stat.S_ISREG(metadata.st_mode):
            raise ServiceError("SOURCE_FORBIDDEN")
        if metadata.st_size > max_bytes:
            raise ServiceError("INPUT_TOO_LARGE")
        result, source = source, -1
        return result
    except FileNotFoundError as error:
        raise ServiceError("SOURCE_UNREACHABLE", retryable=True) from error
    except OSError as error:
        raise ServiceError("SOURCE_FORBIDDEN") from error
    finally:
        if directory >= 0:
            os.close(directory)
        if source >= 0:
            os.close(source)


def sha256_fd(fd: int, event: threading.Event) -> tuple[int, str]:
    before = os.fstat(fd)
    digest = hashlib.sha256()
    offset = 0
    while offset < before.st_size:
        check_cancel(event)
        data = os.pread(fd, min(_CHUNK, before.st_size - offset), offset)
        if not data:
            raise ServiceError("INPUT_CHANGED")
        digest.update(data)
        offset += len(data)
    check_cancel(event)
    after = os.fstat(fd)
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ServiceError("INPUT_CHANGED")
    return offset, digest.hexdigest()


def fsync_directory(path: Path, event: threading.Event | None = None) -> None:
    if event is not None:
        check_cancel(event)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def copy_verified(
    source: Path,
    destination: Path,
    event: threading.Event,
    *,
    max_bytes: int = 2 * 1024**3,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> tuple[int, str]:
    source_fd = safe_open(source.parent, source.name, max_bytes)
    target_fd = -1
    created = False
    try:
        check_cancel(event)
        before = os.fstat(source_fd)
        target_fd = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        created = True
        digest = hashlib.sha256()
        size = 0
        while True:
            check_cancel(event)
            data = os.read(source_fd, _CHUNK)
            if not data:
                break
            size += len(data)
            if size > max_bytes:
                raise ServiceError("INPUT_TOO_LARGE")
            digest.update(data)
            view = memoryview(data)
            while view:
                check_cancel(event)
                count = os.write(target_fd, view)
                view = view[count:]
        after = os.fstat(source_fd)
        value = digest.hexdigest()
        if (
            (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
            or (expected_size is not None and size != expected_size)
            or (expected_sha256 is not None and value != expected_sha256)
        ):
            raise ServiceError("INPUT_CHANGED")
        check_cancel(event)
        os.fsync(target_fd)
        check_cancel(event)
        fsync_directory(destination.parent, event)
        return size, value
    except BaseException:
        if target_fd >= 0:
            os.close(target_fd)
            target_fd = -1
        if created:
            destination.unlink(missing_ok=True)
        raise
    finally:
        os.close(source_fd)
        if target_fd >= 0:
            os.close(target_fd)


def _clear_directory(directory: int, event: threading.Event) -> None:
    with os.scandir(directory) as entries:
        for entry in entries:
            check_cancel(event)
            if entry.is_dir(follow_symlinks=False):
                child = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory,
                )
                try:
                    _clear_directory(child, event)
                    identity = os.fstat(child)
                    current = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
                    if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
                        raise ServiceError("STATE_CONFLICT")
                    os.rmdir(entry.name, dir_fd=directory)
                finally:
                    os.close(child)
            else:
                os.unlink(entry.name, dir_fd=directory)


def remove_owned_dir(root: Path, key: str, event: threading.Event) -> bool:
    """调用方必须先确认此单目录已登记为服务所有，且不存在有效引用。"""
    parent = directory = -1
    try:
        check_cancel(event)
        try:
            parent, leaf = _open_parent(root, key)
            directory = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        except FileNotFoundError:
            return False
        _clear_directory(directory, event)
        check_cancel(event)
        identity = os.fstat(directory)
        current = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
            raise ServiceError("STATE_CONFLICT")
        os.rmdir(leaf, dir_fd=parent)
        os.fsync(parent)
        return True
    finally:
        if directory >= 0:
            os.close(directory)
        if parent >= 0:
            os.close(parent)


def directory_size(path: Path, event: threading.Event) -> int:
    total = 0
    pending = [path]
    while pending:
        check_cancel(event)
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    check_cancel(event)
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except FileNotFoundError:
                        continue
        except FileNotFoundError:
            continue
    return total
