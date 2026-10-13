import asyncio
import hashlib
import logging
import os
import re
import shutil
import threading
import time
import weakref
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass

from python_multipart import MultipartParser
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header

from media_service.database import Transaction, wait_settled
from media_service.downloader import _WRITE_SPACE_LOCK
from media_service.errors import ServiceError
from media_service.files import (
    check_cancel,
    directory_size,
    file_job,
    fsync_directory,
    remove_owned_dir,
)
from media_service.state import State, timestamp, uid

logger = logging.getLogger("media_service")
_CHUNK = 64 * 1024
_FILE_ID = re.compile(r"file_[0-9a-f]{32}")


class _Target:
    def __init__(self, state: State, file_id: str, reservation: int):
        self.settings = state.settings
        self.file_id = file_id
        self.reservation = reservation
        self.directory_fd = -1
        self.fd = -1
        self.size = 0
        self.digest = hashlib.sha256()

    def open(self, event: threading.Event) -> None:
        check_cancel(event)
        root = os.open(
            self.settings.uploads_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.mkdir(self.file_id, mode=0o700, dir_fd=root)
            self.directory_fd = os.open(
                self.file_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root
            )
            self.fd = os.open(
                "content.part", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=self.directory_fd,
            )
            os.fsync(root)
            check_cancel(event)
        finally:
            os.close(root)

    def write(self, data: bytes, event: threading.Event) -> None:
        check_cancel(event)
        if self.size + len(data) > self.settings.max_input_bytes:
            raise ServiceError("INPUT_TOO_LARGE", 413)
        if self.size + len(data) > self.reservation:
            raise ServiceError("INPUT_CHANGED")
        while not _WRITE_SPACE_LOCK.acquire(timeout=0.05):
            check_cancel(event)
        try:
            if directory_size(self.settings.work_dir, event) + len(data) > self.settings.max_work_bytes:
                raise ServiceError("OUTPUT_LIMIT")
            if shutil.disk_usage(self.settings.work_dir).free - len(data) < self.settings.disk_reserve_bytes:
                raise ServiceError("DISK_FULL", 503, True)
            view = memoryview(data)
            while view:
                check_cancel(event)
                count = os.write(self.fd, view)
                view = view[count:]
            self.digest.update(data)
            self.size += len(data)
        finally:
            _WRITE_SPACE_LOCK.release()

    def finalize(self, event: threading.Event) -> None:
        check_cancel(event)
        os.fchmod(self.fd, 0o400)
        os.fsync(self.fd)
        os.close(self.fd)
        self.fd = -1
        check_cancel(event)
        os.rename(
            "content.part", "content", src_dir_fd=self.directory_fd,
            dst_dir_fd=self.directory_fd,
        )
        os.fsync(self.directory_fd)
        check_cancel(event)

    def close(self, _: threading.Event) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        if self.directory_fd >= 0:
            os.close(self.directory_fd)
            self.directory_fd = -1


class Uploads:
    def __init__(self, state: State):
        self.state = state
        self.settings = state.settings
        self._slots = asyncio.Semaphore(self.settings.max_uploads)
        self._locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._gc_lock = asyncio.Lock()
        self._closing = False
        self._receivers: set[asyncio.Task] = set()

    async def close(self) -> None:
        self._closing = True
        current = asyncio.current_task()
        receivers = [task for task in self._receivers if task is not current]
        for task in receivers:
            task.cancel()

        async def settle() -> None:
            await asyncio.gather(*receivers, return_exceptions=True)

        await wait_settled(asyncio.create_task(settle()))

    async def initialize(self) -> None:
        def initialize(event: threading.Event) -> None:
            check_cancel(event)
            self.settings.uploads_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(
                self.settings.uploads_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            fsync_directory(self.settings.work_dir, event)

        await file_job(initialize)

    async def recover(self) -> dict:
        # 仅启动阶段调用；正常回收不能把仍在读取请求体的上传视为中断。
        await self.state.db.run(lambda tx: tx.execute(
            "UPDATE uploaded_files SET state='retiring' WHERE state='uploading'"
        ))
        return await self.gc()

    @staticmethod
    def _public(row: dict) -> dict:
        return {
            "file_id": row["id"],
            "state": row["state"],
            "size_bytes": row["size_bytes"],
            "sha256": row["sha256"],
            "created_at": timestamp(row["created"]),
            "retain_until": timestamp(row["retain_until"]),
        }

    async def metadata(self, caller: str, file_id: str) -> dict:
        async def read(tx: Transaction) -> dict:
            now = time.time()
            await tx.execute(
                "UPDATE uploaded_files SET state='retiring' WHERE id=? AND caller=? "
                "AND state='ready' AND retain_until<=?", (file_id, caller, now),
            )
            row = await tx.one(
                "SELECT * FROM uploaded_files WHERE id=? AND caller=?", (file_id, caller)
            )
            if row is None:
                raise ServiceError("NOT_FOUND", 404)
            return self._public(row)

        return await self.state.db.run(read)

    async def release(self, caller: str, file_id: str) -> dict:
        async def release(tx: Transaction) -> dict:
            row = await tx.one(
                "SELECT * FROM uploaded_files WHERE id=? AND caller=?", (file_id, caller)
            )
            if row is None:
                raise ServiceError("NOT_FOUND", 404)
            if row["state"] == "uploading":
                raise ServiceError("STATE_CONFLICT", 409)
            await tx.execute(
                "UPDATE uploaded_files SET state='retiring' WHERE id=? AND state='ready'",
                (file_id,),
            )
            if row["state"] == "ready":
                row["state"] = "retiring"
            return self._public(row)

        return await self.state.db.run(release)

    async def receive_stream(
        self, caller: str, key: str, stream: AsyncIterator[bytes], *,
        expected_size: int | None = None, expected_sha256: str | None = None,
        allow_retired_replay: bool = False,
    ) -> tuple[dict, bool]:
        if self._closing:
            raise ServiceError("STATE_UNAVAILABLE", 503, True)
        if (
            not 1 <= len(key) <= 128
            or any(ord(character) < 32 or ord(character) > 126 for character in key)
            or (expected_size is not None and expected_size < 0)
            or (expected_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256))
        ):
            raise ServiceError("INVALID_REQUEST", 400)
        if expected_size is not None and expected_size > self.settings.max_input_bytes:
            raise ServiceError("INPUT_TOO_LARGE", 413)
        lock = self._locks.setdefault((caller, key), asyncio.Lock())
        receiver = asyncio.current_task()
        assert receiver is not None
        self._receivers.add(receiver)
        try:
            async with asyncio.timeout(self.settings.upload_timeout_seconds), lock, self._slots:
                return await self._receive(
                    caller, key, stream, expected_size, expected_sha256, allow_retired_replay
                )
        except TimeoutError:
            raise ServiceError("UPLOAD_TIMEOUT", 408, True) from None
        except OSError as error:
            raise ServiceError(
                "DISK_FULL" if error.errno == 28 else "STATE_UNAVAILABLE", 503, True
            ) from None
        finally:
            self._receivers.discard(receiver)

    async def _receive(
        self, caller: str, key: str, stream: AsyncIterator[bytes],
        expected_size: int | None, expected_sha256: str | None, allow_retired_replay: bool,
    ) -> tuple[dict, bool]:
        file_id = uid("file")
        reservation = self.settings.max_input_bytes if expected_size is None else expected_size
        target: _Target | None = None

        async def reserve(tx: Transaction) -> dict | None:
            now = time.time()
            previous = await tx.one(
                "SELECT * FROM uploaded_files WHERE caller=? AND upload_key=?", (caller, key)
            )
            if previous is not None:
                replayable = (
                    allow_retired_replay and previous["state"] != "uploading"
                    and previous["sha256"] is not None
                )
                if not replayable and (
                    previous["state"] != "ready" or previous["retain_until"] <= now
                ):
                    raise ServiceError("FILE_GONE", 410)
                return previous
            usage = await tx.one(
                "SELECT COALESCE(sum(size_bytes),0) n FROM uploaded_files WHERE state!='deleted'"
            )
            assert usage is not None
            if usage["n"] + reservation > self.settings.max_upload_bytes:
                raise ServiceError("QUOTA_EXCEEDED", 429, True)
            await tx.execute(
                "INSERT INTO uploaded_files(id,caller,upload_key,state,path,size_bytes,"
                "created,retain_until,request_hash) VALUES(?,?,?,'uploading',?,?,?,NULL,'')",
                (file_id, caller, key, f"{file_id}/content", reservation, now),
            )
            return None

        try:
            previous = await self.state.db.run(reserve)
            if previous is not None:
                size, digest = await self._hash_stream(stream, expected_size, expected_sha256)
                if size != previous["size_bytes"] or digest != previous["sha256"]:
                    raise ServiceError("IDEMPOTENCY_CONFLICT", 409)
                metadata = await self.metadata(caller, previous["id"])
                if metadata["state"] != "ready" and not allow_retired_replay:
                    raise ServiceError("FILE_GONE", 410)
                return metadata, False
            target = _Target(self.state, file_id, reservation)
            await file_job(target.open)
            async for chunk in stream:
                for offset in range(0, len(chunk), _CHUNK):
                    data = chunk[offset:offset + _CHUNK]
                    await file_job(lambda event, data=data: target.write(data, event))
            digest = target.digest.hexdigest()
            if (
                (expected_size is not None and target.size != expected_size)
                or (expected_sha256 is not None and digest != expected_sha256)
            ):
                raise ServiceError("INPUT_CHANGED")
            await file_job(target.finalize)
            await file_job(target.close)

            async def publish(tx: Transaction) -> dict:
                now = time.time()
                assert target is not None
                changed = await tx.execute(
                    "UPDATE uploaded_files SET state='ready',size_bytes=?,sha256=?,"
                    "retain_until=?,request_hash=? WHERE id=? AND state='uploading'",
                    (target.size, digest, now + self.settings.upload_retention_seconds,
                     hashlib.sha256(f"{target.size}:{digest}".encode()).hexdigest(), file_id),
                )
                if changed != 1:
                    raise ServiceError("STATE_CONFLICT", 409)
                row = await tx.one("SELECT * FROM uploaded_files WHERE id=?", (file_id,))
                assert row is not None
                return self._public(row)

            return await self.state.db.run(publish), True
        finally:
            async def settle() -> None:
                if target is not None:
                    await file_job(target.close)
                # 已提交 ready 的文件保持不变；响应丢失仍可按同键重放查回。
                await self.state.db.run(lambda tx: tx.execute(
                    "UPDATE uploaded_files SET state='retiring' WHERE id=? AND state='uploading'",
                    (file_id,),
                ))

            settling = asyncio.create_task(settle())
            try:
                await asyncio.shield(settling)
            except asyncio.CancelledError:
                await wait_settled(settling)
                raise

    async def _hash_stream(
        self, stream: AsyncIterator[bytes], expected_size: int | None,
        expected_sha256: str | None,
    ) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        async for data in stream:
            size += len(data)
            if size > self.settings.max_input_bytes:
                raise ServiceError("INPUT_TOO_LARGE", 413)
            if expected_size is not None and size > expected_size:
                raise ServiceError("INPUT_CHANGED")
            digest.update(data)
        value = digest.hexdigest()
        if (
            (expected_size is not None and size != expected_size)
            or (expected_sha256 is not None and value != expected_sha256)
        ):
            raise ServiceError("INPUT_CHANGED")
        return size, value

    async def gc(self) -> dict:
        async with self._gc_lock:
            async def candidates(tx: Transaction) -> list[dict]:
                now = time.time()
                await tx.execute(
                    "UPDATE uploaded_files SET state='retiring' WHERE state='ready' "
                    "AND retain_until<=?", (now,),
                )
                await tx.execute(
                    "UPDATE uploaded_files SET state='deleting' WHERE state='retiring' "
                    "AND NOT EXISTS (SELECT 1 FROM file_refs WHERE file_id=uploaded_files.id)"
                )
                return await tx.all(
                    "SELECT * FROM uploaded_files WHERE state='deleting' "
                    "AND NOT EXISTS (SELECT 1 FROM file_refs WHERE file_id=uploaded_files.id)"
                )

            deleted = 0
            for row in await self.state.db.run(candidates):
                if not _FILE_ID.fullmatch(row["id"]) or row["path"] != f"{row['id']}/content":
                    logger.warning("上传文件登记路径无效", extra={"file_id": row["id"]})
                    continue
                try:
                    await file_job(lambda event, row=row: remove_owned_dir(
                        self.settings.uploads_dir, row["id"], event
                    ))
                except (OSError, ServiceError):
                    logger.warning("上传文件回收失败，保留登记以重试", extra={"file_id": row["id"]})
                    continue
                deleted += await self.state.db.run(lambda tx, row=row: tx.execute(
                    "UPDATE uploaded_files SET state='deleted',deleted_at=? "
                    "WHERE id=? AND state='deleting'", (time.time(), row["id"]),
                ))
            pruned = await self.state.db.run(lambda tx: tx.execute(
                "DELETE FROM uploaded_files WHERE state='deleted' AND deleted_at<=? "
                "AND NOT EXISTS (SELECT 1 FROM file_refs WHERE file_id=uploaded_files.id)",
                (time.time() - self.settings.idempotency_seconds,),
            ))
            return {"uploads_deleted": deleted, "uploads_pruned": pruned}


@dataclass(frozen=True)
class MultipartPart:
    name: str
    is_file: bool
    reader: MultipartReader

    def stream(self) -> AsyncIterator[bytes]:
        return self.reader._body()


class MultipartReader:
    def __init__(
        self, stream: AsyncIterator[bytes], content_type: str, *,
        max_bytes: int, max_parts: int = 5,
    ):
        kind, options = parse_options_header(content_type)
        boundary = options.get(b"boundary", b"")
        if (
            kind != b"multipart/form-data" or not 1 <= len(boundary) <= 200
            or any(byte < 33 or byte > 126 for byte in boundary)
        ):
            raise ServiceError("INVALID_REQUEST", 400)
        self.stream = stream
        self.max_bytes = max_bytes
        self.max_parts = max_parts
        self.total_bytes = 0
        self.part_count = 0
        self._headers: dict[bytes, bytes] = {}
        self._field = bytearray()
        self._value = bytearray()
        self._header_bytes = 0
        self._events: deque[tuple[str, bytes | MultipartPart | None]] = deque()
        self._remaining = memoryview(b"")
        self._body_active = False
        self._ended = False
        self._exhausted = False
        self._parser = MultipartParser(boundary, callbacks={
            "on_part_begin": self._part_begin,
            "on_header_field": self._header_field,
            "on_header_value": self._header_value,
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._part_data,
            "on_part_end": self._part_end,
            "on_end": self._end,
        })

    def _part_begin(self) -> None:
        self.part_count += 1
        if self.part_count > self.max_parts:
            raise ServiceError("INVALID_REQUEST", 400)
        self._headers = {}
        self._field.clear()
        self._value.clear()
        self._header_bytes = 0

    def _header(self, target: bytearray, data: bytes, start: int, end: int) -> None:
        self._header_bytes += end - start
        if self._header_bytes > 8192:
            raise ServiceError("INVALID_REQUEST", 400)
        target.extend(data[start:end])

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._header(self._field, data, start, end)

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._header(self._value, data, start, end)

    def _header_end(self) -> None:
        field = bytes(self._field).lower()
        if field in self._headers or len(self._headers) >= 16:
            raise ServiceError("INVALID_REQUEST", 400)
        self._headers[field] = bytes(self._value)
        self._field.clear()
        self._value.clear()

    def _headers_finished(self) -> None:
        kind, options = parse_options_header(self._headers.get(b"content-disposition"))
        name = options.get(b"name", b"")
        if kind != b"form-data" or not re.fullmatch(rb"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
            raise ServiceError("INVALID_REQUEST", 400)
        self._events.append(("start", MultipartPart(
            name.decode("ascii"), b"filename" in options, self
        )))

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        if end > start:
            self._events.append(("data", data[start:end]))

    def _part_end(self) -> None:
        self._events.append(("end", None))

    def _end(self) -> None:
        self._ended = True

    async def _event(self) -> tuple[str, bytes | MultipartPart | None] | None:
        while not self._events:
            if not self._remaining:
                if self._exhausted:
                    if not self._ended:
                        raise ServiceError("INVALID_REQUEST", 400)
                    return None
                try:
                    data = await anext(self.stream)
                except StopAsyncIteration:
                    self._exhausted = True
                    self._parser.finalize()
                    continue
                self.total_bytes += len(data)
                if self.total_bytes > self.max_bytes:
                    raise ServiceError("REQUEST_TOO_LARGE", 413)
                self._remaining = memoryview(data)
                if not self._remaining:
                    continue
            data = bytes(self._remaining[:_CHUNK])
            self._remaining = self._remaining[_CHUNK:]
            try:
                self._parser.write(data)
            except MultipartParseError:
                raise ServiceError("INVALID_REQUEST", 400) from None
        return self._events.popleft()

    async def next_part(self) -> MultipartPart | None:
        if self._body_active:
            raise ServiceError("STATE_CONFLICT", 409)
        event = await self._event()
        if event is None:
            return None
        kind, value = event
        if kind != "start" or not isinstance(value, MultipartPart):
            raise ServiceError("INVALID_REQUEST", 400)
        self._body_active = True
        return value

    async def _body(self) -> AsyncIterator[bytes]:
        if not self._body_active:
            raise ServiceError("STATE_CONFLICT", 409)
        while True:
            event = await self._event()
            if event is None:
                raise ServiceError("INVALID_REQUEST", 400)
            kind, value = event
            if kind == "end":
                self._body_active = False
                return
            if kind != "data" or not isinstance(value, bytes):
                raise ServiceError("INVALID_REQUEST", 400)
            yield value
