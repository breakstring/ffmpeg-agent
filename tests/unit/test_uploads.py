import asyncio
import hashlib
import json
import os
import threading
import time

import pytest

import media_service.uploads as upload_module
from media_service.database import Database
from media_service.errors import ServiceError
from media_service.models import JobCreate
from media_service.state import State, uid
from media_service.uploads import MultipartReader, Uploads


@pytest.fixture
async def uploads(settings):
    settings.work_dir.mkdir()
    database = Database(settings.state_dir)
    await database.open()
    manager = Uploads(State(database, settings))
    await manager.initialize()
    try:
        yield manager
    finally:
        await manager.close()
        await database.close()


async def chunks(*items):
    for item in items:
        yield item


async def expect_error(awaitable, code, status=None):
    with pytest.raises(ServiceError) as failure:
        await awaitable
    assert failure.value.code == code
    if status is not None:
        assert failure.value.status == status


async def test_streamed_upload_is_immutable_and_same_key_checks_actual_content(uploads):
    body = b"uploaded-content"
    digest = hashlib.sha256(body).hexdigest()
    metadata, created = await uploads.receive_stream(
        "test", "upload-key", chunks(body[:4], b"", body[4:]),
        expected_size=len(body), expected_sha256=digest,
    )
    assert created and metadata["state"] == "ready"
    assert metadata["sha256"] == digest and metadata["size_bytes"] == len(body)
    assert metadata["retain_until"] is not None
    assert str(uploads.settings.work_dir) not in json.dumps(metadata)
    path = uploads.settings.uploads_dir / metadata["file_id"] / "content"
    assert path.read_bytes() == body
    assert os.stat(path).st_mode & 0o777 == 0o400
    replay, created = await uploads.receive_stream("test", "upload-key", chunks(body))
    assert not created and replay == metadata
    await expect_error(
        uploads.receive_stream("test", "upload-key", chunks(b"other-content")),
        "IDEMPOTENCY_CONFLICT", 409,
    )
    assert len(await uploads.state.db.all("SELECT * FROM uploaded_files")) == 1
    assert path.read_bytes() == body
    await expect_error(uploads.metadata("other", metadata["file_id"]), "NOT_FOUND", 404)


async def test_matching_uploads_with_concurrent_same_key_create_one_file(uploads):
    async def request():
        async def stream():
            yield b"first"
            await asyncio.sleep(0)
            yield b"second"

        return await uploads.receive_stream("test", "same-key", stream())

    results = await asyncio.gather(request(), request())
    assert {created for _, created in results} == {True, False}
    assert len({metadata["file_id"] for metadata, _ in results}) == 1
    assert len(await uploads.state.db.all("SELECT * FROM uploaded_files")) == 1


async def test_integrity_failure_retires_only_registered_file(uploads):
    unknown = uploads.settings.uploads_dir / "caller-owned"
    unknown.mkdir()
    (unknown / "source").write_bytes(b"keep")
    await expect_error(
        uploads.receive_stream("test", "wrong-hash", chunks(b"data"), expected_sha256="0" * 64),
        "INPUT_CHANGED",
    )
    row = await uploads.state.db.one("SELECT * FROM uploaded_files")
    assert row is not None and row["state"] == "retiring"
    assert await uploads.gc() == {"uploads_deleted": 1, "uploads_pruned": 0}
    assert not (uploads.settings.uploads_dir / row["id"]).exists()
    assert (unknown / "source").read_bytes() == b"keep"
    await expect_error(
        uploads.receive_stream("test", "wrong-hash", chunks(b"data")), "FILE_GONE", 410
    )


async def test_declared_and_actual_size_limits(uploads):
    uploads.settings.max_input_bytes = 4
    await expect_error(
        uploads.receive_stream("test", "declared", chunks(b"12345"), expected_size=5),
        "INPUT_TOO_LARGE", 413,
    )
    assert await uploads.state.db.all("SELECT * FROM uploaded_files") == []
    await expect_error(
        uploads.receive_stream("test", "actual", chunks(b"12", b"345")),
        "INPUT_TOO_LARGE", 413,
    )
    await expect_error(
        uploads.receive_stream("test", "short", chunks(b"12"), expected_size=3),
        "INPUT_CHANGED",
    )
    await expect_error(
        uploads.receive_stream("test", "long", chunks(b"123"), expected_size=2),
        "INPUT_CHANGED",
    )
    assert all(row["state"] == "retiring" for row in await uploads.state.db.all(
        "SELECT * FROM uploaded_files"
    ))


async def test_upload_quota_reservations_block_concurrent_streams_and_gc_releases_quota(uploads):
    uploads.settings.max_input_bytes = 8
    uploads.settings.max_upload_bytes = 8
    started = asyncio.Event()
    finish = asyncio.Event()

    async def held():
        yield b"x"
        started.set()
        await finish.wait()

    receiver = asyncio.create_task(uploads.receive_stream("test", "held", held()))
    await started.wait()
    await expect_error(
        uploads.receive_stream("test", "other", chunks(b"y")), "QUOTA_EXCEEDED", 429
    )
    assert await uploads.gc() == {"uploads_deleted": 0, "uploads_pruned": 0}
    receiver.cancel()
    with pytest.raises(asyncio.CancelledError):
        await receiver
    assert await uploads.gc() == {"uploads_deleted": 1, "uploads_pruned": 0}
    _, created = await uploads.receive_stream("test", "new", chunks(b"y"))
    assert created


async def test_cancel_waits_for_file_write_before_gc_can_remove_directory(uploads, monkeypatch):
    entered = threading.Event()
    unblock = threading.Event()
    original = upload_module._Target.write

    def blocked(self, data, event):
        entered.set()
        assert unblock.wait(5)
        original(self, data, event)

    monkeypatch.setattr(upload_module._Target, "write", blocked)
    receiver = asyncio.create_task(uploads.receive_stream("test", "blocked", chunks(b"body")))
    assert await asyncio.to_thread(entered.wait, 5)
    receiver.cancel()
    await asyncio.sleep(0.02)
    assert not receiver.done()
    assert await uploads.gc() == {"uploads_deleted": 0, "uploads_pruned": 0}
    row = await uploads.state.db.one("SELECT * FROM uploaded_files")
    assert row is not None and row["state"] == "uploading"
    assert (uploads.settings.uploads_dir / row["id"]).is_dir()
    unblock.set()
    with pytest.raises(asyncio.CancelledError):
        await receiver
    assert await uploads.gc() == {"uploads_deleted": 1, "uploads_pruned": 0}


async def test_shutdown_stops_requests_waits_for_cleanup_and_rejects_new_uploads(uploads):
    started = asyncio.Event()

    async def held():
        yield b"x"
        started.set()
        await asyncio.Event().wait()

    receiver = asyncio.create_task(uploads.receive_stream("test", "shutdown", held()))
    await started.wait()
    await uploads.close()
    assert receiver.done() and receiver.cancelled()
    row = await uploads.state.db.one("SELECT * FROM uploaded_files")
    assert row is not None and row["state"] == "retiring"
    await expect_error(
        uploads.receive_stream("test", "new", chunks(b"x")), "STATE_UNAVAILABLE", 503
    )
    assert await uploads.gc() == {"uploads_deleted": 1, "uploads_pruned": 0}


async def test_upload_timeout_releases_handles_and_registers_cleanup(uploads):
    uploads.settings.upload_timeout_seconds = 0.02

    async def stalled():
        yield b"x"
        await asyncio.Event().wait()

    await expect_error(
        uploads.receive_stream("test", "timeout", stalled()), "UPLOAD_TIMEOUT", 408
    )
    assert await uploads.gc() == {"uploads_deleted": 1, "uploads_pruned": 0}


async def test_expiration_release_and_references_protect_upload_until_inputs_finish(uploads):
    metadata, _ = await uploads.receive_stream("test", "reference", chunks(b"x"))
    request = JobCreate.model_validate({
        "inputs": {"main": {"kind": "uploaded_file", "file_id": metadata["file_id"]}},
        "steps": [{"id": "probe", "operation": "media.probe.v1",
                   "inputs": {"media": {"source": "main"}}}],
        "outputs": {"metadata": {"step": "probe", "output": "report"}},
    })
    job_id, _ = await uploads.state.accept("test", "ref-job", request, {})

    async def expire_with_reference(tx):
        await tx.execute(
            "UPDATE uploaded_files SET retain_until=? WHERE id=?",
            (time.time() - 1, metadata["file_id"]),
        )

    await uploads.state.db.run(expire_with_reference)
    assert (await uploads.metadata("test", metadata["file_id"]))["state"] == "retiring"
    assert await uploads.gc() == {"uploads_deleted": 0, "uploads_pruned": 0}
    assert (uploads.settings.uploads_dir / metadata["file_id"] / "content").read_bytes() == b"x"
    assert (await uploads.release("test", metadata["file_id"]))["state"] == "retiring"
    await expect_error(
        uploads.state.accept("test", "expired-new-ref", request, {}), "FILE_GONE", 410
    )
    await uploads.state.cancel("test", job_id)
    assert await uploads.gc() == {"uploads_deleted": 1, "uploads_pruned": 0}
    assert (await uploads.metadata("test", metadata["file_id"]))["state"] == "deleted"


async def test_combined_replay_can_validate_retired_tombstone_without_restoring_file(uploads):
    metadata, _ = await uploads.receive_stream("test", "combined-key", chunks(b"content"))
    await uploads.release("test", metadata["file_id"])
    await uploads.gc()
    path = uploads.settings.uploads_dir / metadata["file_id"]
    assert not path.exists()
    await expect_error(
        uploads.receive_stream("test", "combined-key", chunks(b"content")), "FILE_GONE", 410
    )
    replay, created = await uploads.receive_stream(
        "test", "combined-key", chunks(b"content"), allow_retired_replay=True,
    )
    assert not created and replay["file_id"] == metadata["file_id"]
    assert replay["state"] == "deleted" and replay["size_bytes"] == len(b"content")
    assert not path.exists()
    await expect_error(
        uploads.receive_stream(
            "test", "combined-key", chunks(b"changed"), allow_retired_replay=True,
        ), "IDEMPOTENCY_CONFLICT", 409,
    )


async def test_startup_recovers_only_registered_interrupted_uploads(uploads):
    completed, _ = await uploads.receive_stream("test", "ready", chunks(b"ready"))
    interrupted = uid("file")
    directory = uploads.settings.uploads_dir / interrupted
    directory.mkdir()
    (directory / "content.part").write_bytes(b"partial")
    unknown = uploads.settings.uploads_dir / uid("file")
    unknown.mkdir()
    (unknown / "content.part").write_bytes(b"keep")
    await uploads.state.db.run(lambda tx: tx.execute(
        "INSERT INTO uploaded_files(id,caller,upload_key,state,path,size_bytes,created) "
        "VALUES(?,?,?,'uploading',?,?,?)",
        (interrupted, "test", "interrupted", f"{interrupted}/content", 100, time.time()),
    ))
    assert await uploads.recover() == {"uploads_deleted": 1, "uploads_pruned": 0}
    assert not directory.exists()
    assert (unknown / "content.part").read_bytes() == b"keep"
    assert (await uploads.metadata("test", completed["file_id"]))["state"] == "ready"


async def test_deleted_upload_tombstone_is_pruned_only_after_idempotency_window(uploads):
    metadata, _ = await uploads.receive_stream("test", "prune-key", chunks(b"content"))
    await uploads.release("test", metadata["file_id"])
    assert await uploads.gc() == {"uploads_deleted": 1, "uploads_pruned": 0}
    row = await uploads.state.db.one(
        "SELECT deleted_at FROM uploaded_files WHERE id=?", (metadata["file_id"],)
    )
    assert row is not None and row["deleted_at"] is not None
    assert await uploads.gc() == {"uploads_deleted": 0, "uploads_pruned": 0}
    await uploads.state.db.run(lambda tx: tx.execute(
        "UPDATE uploaded_files SET deleted_at=? WHERE id=?",
        (time.time() - uploads.settings.idempotency_seconds - 1, metadata["file_id"]),
    ))
    assert await uploads.gc() == {"uploads_deleted": 0, "uploads_pruned": 1}
    await expect_error(uploads.metadata("test", metadata["file_id"]), "NOT_FOUND", 404)
    replacement, created = await uploads.receive_stream("test", "prune-key", chunks(b"new"))
    assert created and replacement["file_id"] != metadata["file_id"]


def multipart(*parts, boundary="test-boundary", complete=True):
    data = b""
    for name, body, filename in parts:
        data += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"".encode()
        if filename:
            data += b'; filename="caller-filename-ignored.mp4"'
        data += b"\r\n\r\n" + body + b"\r\n"
    if complete:
        data += f"--{boundary}--\r\n".encode()
    return data


async def test_multipart_streams_small_chunks_without_losing_split_boundaries():
    payload = multipart(("job", b'{"inputs":{}}', False), ("main", b"0123456789", True))
    stream = chunks(*(payload[index:index + 3] for index in range(0, len(payload), 3)))
    reader = MultipartReader(stream, "multipart/form-data; boundary=test-boundary", max_bytes=4096)
    job = await reader.next_part()
    assert job is not None and job.name == "job" and not job.is_file
    assert b"".join([data async for data in job.stream()]) == b'{"inputs":{}}'
    media = await reader.next_part()
    assert media is not None and media.name == "main" and media.is_file
    assert b"".join([data async for data in media.stream()]) == b"0123456789"
    assert await reader.next_part() is None


async def test_multipart_reading_requires_consuming_previous_part():
    reader = MultipartReader(
        chunks(multipart(("job", b"data", False))),
        "multipart/form-data; boundary=test-boundary", max_bytes=4096,
    )
    part = await reader.next_part()
    assert part is not None
    await expect_error(reader.next_part(), "STATE_CONFLICT", 409)
    assert b"".join([data async for data in part.stream()]) == b"data"
    assert await reader.next_part() is None


@pytest.mark.parametrize("payload", [
    multipart(("job", b"body", False), complete=False),
    b"--test-boundary\r\nContent-Disposition: form-data; name=\"bad/name\"\r\n\r\nx\r\n"
    b"--test-boundary--\r\n",
    b"--test-boundary\r\nContent-Disposition: form-data; name=\"job\"\r\n"
    b"Content-Disposition: form-data; name=\"job\"\r\n\r\nx\r\n--test-boundary--\r\n",
    b"--test-boundary\r\nX-Large: " + b"x" * 8193 + b"\r\n\r\nx\r\n--test-boundary--\r\n",
])
async def test_multipart_rejects_truncated_or_invalid_headers(payload):
    reader = MultipartReader(
        chunks(payload), "multipart/form-data; boundary=test-boundary", max_bytes=20000,
    )

    async def read_all():
        while part := await reader.next_part():
            async for _ in part.stream():
                pass

    await expect_error(read_all(), "INVALID_REQUEST", 400)


async def test_multipart_checks_total_bytes_and_part_count():
    reader = MultipartReader(
        chunks(multipart(("job", b"x", False))),
        "multipart/form-data; boundary=test-boundary", max_bytes=10,
    )
    await expect_error(reader.next_part(), "REQUEST_TOO_LARGE", 413)
    reader = MultipartReader(
        chunks(multipart(("job", b"x", False), ("main", b"y", True))),
        "multipart/form-data; boundary=test-boundary", max_bytes=4096, max_parts=1,
    )
    await expect_error(reader.next_part(), "INVALID_REQUEST", 400)


async def test_multipart_bounds_parser_chunks_even_when_transport_chunk_is_large():
    body = b"x" * (3 * upload_module._CHUNK)
    reader = MultipartReader(
        chunks(multipart(("main", body, True))),
        "multipart/form-data; boundary=test-boundary", max_bytes=len(body) + 4096,
    )
    part = await reader.next_part()
    assert part is not None
    received = [data async for data in part.stream()]
    assert max(map(len, received)) <= upload_module._CHUNK
    assert b"".join(received) == body
    assert await reader.next_part() is None
