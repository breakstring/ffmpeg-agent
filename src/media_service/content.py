import asyncio
import json
import math
import os
import re
from contextlib import suppress
from email.utils import formatdate, parsedate_to_datetime

from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from media_service.artifacts import Artifacts
from media_service.database import wait_settled
from media_service.errors import ServiceError
from media_service.files import file_job, safe_open


def byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    if value is None:
        return None
    if len(value) > 4096 or not value.startswith("bytes="):
        raise ServiceError("INVALID_RANGE", 416)
    parts = value[6:].split(",")
    parsed = []
    for part in parts:
        match = re.fullmatch(r"\s*([0-9]*)-([0-9]*)\s*", part)
        if match is None or not any(match.groups()):
            raise ServiceError("INVALID_RANGE", 416)
        start, end = match.groups()
        if len(start) > 20 or len(end) > 20:
            raise ServiceError("INVALID_RANGE", 416)
        if start and end and int(start) > int(end):
            raise ServiceError("INVALID_RANGE", 416)
        parsed.append((start, end))
    if len(parsed) > 1:
        # 首版明确忽略合法多区间，回完整200；不生成multipart。
        return None
    start, end = parsed[0]
    if not start:
        length = int(end)
        if length == 0 or size == 0:
            raise ServiceError("INVALID_RANGE", 416)
        return max(0, size - length), size - 1
    first = int(start)
    if first >= size:
        raise ServiceError("INVALID_RANGE", 416)
    return first, min(int(end), size - 1) if end else size - 1


def matches_if_range(value: str, etag: str, published: float) -> bool:
    if value == etag:
        return True
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.tzinfo is not None and parsed.timestamp() == math.floor(published)
    except TypeError, ValueError, OverflowError:
        return False


class ArtifactResponse(Response):
    def __init__(
        self,
        artifacts: Artifacts,
        caller: str,
        artifact_id: str,
        member_key: str | None = None,
        lease_id: str | None = None,
    ):
        super().__init__()
        self.artifacts = artifacts
        self.caller = caller
        self.artifact_id = artifact_id
        self.member_key = member_key
        self.lease_id = lease_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        artifact, ref_id, entry = await self.artifacts.acquire_send(
            self.caller, self.artifact_id, self.member_key, self.lease_id
        )
        fd = -1
        sender: asyncio.Task | None = None
        disconnect: asyncio.Task | None = None
        try:
            root = self.artifacts.settings.storages[artifact["storage_id"]].root
            fd = safe_open(
                root,
                f"{artifact['directory']}/{entry['key']}",
                self.artifacts.settings.max_output_bytes,
            )
            size = os.fstat(fd).st_size
            if size != entry["size_bytes"]:
                raise ServiceError("STATE_UNAVAILABLE", 503)
            etag = '"' + entry["sha256"] + '"'
            headers = {
                key.decode("latin1").lower(): value.decode("latin1")
                for key, value in scope["headers"]
            }
            range_value = headers.get("range")
            if (
                range_value
                and headers.get("if-range")
                and not matches_if_range(headers["if-range"], etag, artifact["published"])
            ):
                range_value = None
            try:
                selected = byte_range(range_value, size)
            except ServiceError as exc:
                request_id = scope.get("state", {}).get("request_id")
                await JSONResponse(
                    {"error": exc.public(request_id)},
                    status_code=416,
                    headers={"Content-Range": f"bytes */{size}", "ETag": etag},
                )(scope, receive, send)
                return
            first, last = selected or (0, size - 1)
            media = json.loads(artifact["media"])
            content_type = media.get("content_type") if artifact["kind"] == "file" else None
            if content_type is None:
                content_type = {
                    ".jpg": "image/jpeg",
                    ".png": "image/png",
                    ".webp": "image/webp",
                }.get(os.path.splitext(entry["key"])[1], "application/octet-stream")
            response_headers = {
                "content-type": content_type,
                "content-length": str(max(0, last - first + 1)),
                "accept-ranges": "bytes",
                "etag": etag,
                "last-modified": formatdate(artifact["published"], usegmt=True),
                "x-content-type-options": "nosniff",
            }
            if selected:
                response_headers["content-range"] = f"bytes {first}-{last}/{size}"

            async def transmit():
                await send(
                    {
                        "type": "http.response.start",
                        "status": 206 if selected else 200,
                        "headers": [
                            (k.encode("ascii"), v.encode("ascii"))
                            for k, v in response_headers.items()
                        ],
                    }
                )
                if scope["method"] == "HEAD":
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                    return
                offset = first
                while offset <= last:
                    data = await file_job(
                        lambda event, offset=offset: os.pread(
                            fd, min(256 * 1024, last - offset + 1), offset
                        )
                    )
                    if not data:
                        raise ClientDisconnect
                    offset += len(data)
                    await send(
                        {"type": "http.response.body", "body": data, "more_body": offset <= last}
                    )
                if size == 0:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})

            async def watch_disconnect():
                while True:
                    if (await receive())["type"] == "http.disconnect":
                        return

            sender = asyncio.create_task(transmit())
            disconnect = asyncio.create_task(watch_disconnect())
            done, _ = await asyncio.wait(
                {sender, disconnect},
                timeout=self.artifacts.settings.http_send_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if sender in done:
                await sender
            elif disconnect not in done:
                raise ClientDisconnect
        finally:
            for task in (sender, disconnect):
                if task is not None:
                    task.cancel()
                    with suppress(BaseException):
                        await wait_settled(task)
            if fd >= 0:
                os.close(fd)
            await wait_settled(asyncio.create_task(self.artifacts.release_send(ref_id)))
