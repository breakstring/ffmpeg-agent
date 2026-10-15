import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from pydantic import ValidationError
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from media_service.agent_models import AgentRequest, AgentTaskResult, MediaPlan
from media_service.auth import authorize
from media_service.config import Settings
from media_service.content import ArtifactResponse
from media_service.database import wait_settled
from media_service.errors import ServiceError
from media_service.models import (
    ArtifactResult,
    ErrorEnvelope,
    FileResult,
    JobCreate,
    JobResult,
    LeaseRequest,
    LeaseResult,
    MembersResult,
    RetentionRequest,
)
from media_service.telemetry import event
from media_service.uploads import MultipartReader

logger = logging.getLogger("media_service")


def multipart_job_schema() -> dict:
    # 文件 part 由流式接收器解析；文档复用 JobCreate，只有上传占位输入不同。
    job = JobCreate.model_json_schema()
    reference = "#/components/schemas/JobCreate/properties/"
    inputs = job["properties"]["inputs"].copy()
    pattern = next(iter(inputs.pop("patternProperties")))
    pattern_pointer = pattern.replace("~", "~0").replace("/", "~1")
    inputs |= {
        "propertyNames": {"pattern": pattern},
        "description": "至少一个输入为 kind=upload；part 必须与输入名及随后文件 part 名一致。",
        "additionalProperties": {"anyOf": [
            {"$ref": reference + "inputs/patternProperties/" + pattern_pointer},
            {"type": "object", "additionalProperties": False, "required": ["kind", "part"],
             "properties": {"kind": {"type": "string", "const": "upload"},
                            "part": {"type": "string", "description": "与该输入的键及文件 part 名相同。"}}},
        ]},
    }
    return {"type": "object", "additionalProperties": False, "required": job["required"],
            "properties": {name: {"$ref": reference + name} for name in job["properties"]}
                          | {"inputs": inputs}}


def error_response(error: ServiceError, request_id: str) -> JSONResponse:
    headers = {"X-Request-ID": request_id}
    if error.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        {"error": error.public(request_id)}, status_code=error.status, headers=headers
    )


class RequestBoundary:
    def __init__(self, app: ASGIApp, config: Settings):
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        started = time.monotonic()
        status = 500
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        headers = Headers(scope=scope)
        path = scope["path"]
        if path.startswith("/v1/") or path in {
            "/mcp", "/mcp/", "/a2a", "/a2a/", "/.well-known/agent-card.json"
        }:
            try:
                scope["state"]["caller"] = authorize(self.config, headers)
            except ServiceError as exc:
                await error_response(exc, request_id)(scope, receive, send_with_id)
                return
        streaming = scope["method"] == "POST" and path in {"/v1/files", "/v1/jobs/upload"}
        max_bytes = self.config.max_request_bytes
        if streaming:
            max_bytes = self.config.max_input_bytes if path == "/v1/files" else (
                4 * self.config.max_input_bytes + self.config.max_request_bytes + 65536
            )
        lengths = headers.getlist("content-length")
        if lengths:
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                await error_response(ServiceError("INVALID_REQUEST", 400), request_id)(
                    scope, receive, send_with_id
                )
                return
            length = lengths[0].lstrip("0") or "0"
            if len(length) > len(str(max_bytes)) or int(length) > max_bytes:
                await error_response(ServiceError("REQUEST_TOO_LARGE", 413), request_id)(
                    scope, receive, send_with_id
                )
                return

        if streaming:
            received = 0

            async def bounded_receive() -> Message:
                nonlocal received
                try:
                    async with asyncio.timeout(self.config.upload_idle_seconds):
                        message = await receive()
                except TimeoutError:
                    raise ServiceError("UPLOAD_TIMEOUT", 408, True) from None
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > max_bytes:
                        raise ServiceError("REQUEST_TOO_LARGE", 413)
                return message

            try:
                await self.app(scope, bounded_receive, send_with_id)
            finally:
                route = scope.get("route")
                event("request_finished", request_id=request_id, method=scope["method"],
                      route=getattr(route, "path", "unmatched"), status=status,
                      duration_ms=round((time.monotonic() - started) * 1000, 2))
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            data = message.get("body", b"")
            if len(body) + len(data) > max_bytes:
                await error_response(ServiceError("REQUEST_TOO_LARGE", 413), request_id)(
                    scope, receive, send_with_id
                )
                return
            body.extend(data)
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay_body() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            # 读完请求体之后仍转交真实 disconnect，流响应的取消由响应执行者处理。
            return await receive()

        try:
            await self.app(scope, replay_body, send_with_id)
        finally:
            route = scope.get("route")
            event("request_finished", request_id=request_id, method=scope["method"],
                  route=getattr(route, "path", "unmatched"), status=status,
                  duration_ms=round((time.monotonic() - started) * 1000, 2))


def create_app(settings: Settings | None = None, service: Any = None) -> FastAPI:
    if service is None:
        from media_service.service import Service

        settings = settings or Settings.from_file()
        service = Service(settings)
    config: Settings = settings or service.settings
    from media_service.mcp import create_mcp

    mcp_app, mcp_server = create_mcp(service, config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await service.start()
        try:
            async with mcp_server.session_manager.run():
                yield
        finally:
            await wait_settled(asyncio.create_task(service.close()))

    app = FastAPI(title="ffmpeg-agent", version="1.0.0", lifespan=lifespan,
                  responses={code: {"model": ErrorEnvelope} for code in
                             (400, 401, 403, 404, 409, 410, 413, 416, 422, 429, 503)})
    app.state.service = service
    app.add_middleware(RequestBoundary, config=config)

    def request_id(request: Request) -> str:
        return getattr(request.state, "request_id", uuid4().hex)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, error: ServiceError) -> JSONResponse:
        event("request_error", request_id=request_id(request), code=error.code)
        return error_response(error, request_id(request))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        status = 400 if any(item["type"] == "json_invalid" for item in error.errors()) else 422
        return error_response(ServiceError("INVALID_REQUEST", status), request_id(request))

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        code = "NOT_FOUND" if error.status_code == 404 else "INVALID_REQUEST"
        return error_response(ServiceError(code, error.status_code), request_id(request))

    @app.exception_handler(sqlite3.Error)
    @app.exception_handler(RuntimeError)
    async def state_unavailable(request: Request, _: Exception) -> JSONResponse:
        return error_response(ServiceError("STATE_UNAVAILABLE", 503, True), request_id(request))

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        logger.error(
            "request_failed",
            extra={"request_id": request_id(request), "error_type": type(error).__name__},
        )
        return error_response(ServiceError("STATE_UNAVAILABLE", 503, True), request_id(request))

    bearer = HTTPBearer(auto_error=False)

    async def caller(request: Request, _: Annotated[Any, Depends(bearer)]) -> str:
        return authorize(config, request.headers)

    def idempotency_key(request: Request) -> str:
        values = request.headers.getlist("idempotency-key")
        if (len(values) != 1 or not 1 <= len(values[0]) <= 128
                or any(ord(c) < 32 or ord(c) > 126 for c in values[0])):
            raise ServiceError("INVALID_REQUEST", 400)
        return values[0]

    def expected_headers(request: Request) -> tuple[int | None, str | None]:
        sizes = request.headers.getlist("x-file-size")
        hashes = request.headers.getlist("x-file-sha256")
        if len(sizes) > 1 or len(hashes) > 1:
            raise ServiceError("INVALID_REQUEST", 400)
        size = None
        if sizes:
            value = sizes[0]
            if not value.isascii() or not value.isdigit() or len(value) > 20:
                raise ServiceError("INVALID_REQUEST", 400)
            size = int(value)
            if size > config.max_input_bytes:
                raise ServiceError("INPUT_TOO_LARGE", 413)
        digest = hashes[0] if hashes else None
        if digest is not None and (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
            raise ServiceError("INVALID_REQUEST", 400)
        return size, digest

    @app.get("/health/live")
    async def live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict:
        if (not service.ready or service.scheduler.failed or not service.db.healthy
                or getattr(getattr(service, "agent_tasks", None), "failed", False)):
            raise ServiceError("STATE_UNAVAILABLE", 503, True)
        return {"status": "ready"}

    @app.get("/v1/capabilities")
    async def get_capabilities(identity: str = Depends(caller)) -> dict:
        from media_service.media import capability_document

        return capability_document(service)

    def require_agent() -> None:
        if not config.llm_enabled:
            raise ServiceError("AGENT_DISABLED", 503)

    @app.post("/v1/agent/tasks", response_model=AgentTaskResult,
              responses={202: {"model": AgentTaskResult}})
    async def generate_media(
        definition: AgentRequest,
        request: Request,
        response: Response,
        identity: str = Depends(caller),
    ) -> dict:
        require_agent()
        key = idempotency_key(request)
        if not service.ready:
            raise ServiceError("STATE_UNAVAILABLE", 503, True)
        task_id, created = await service.agent_tasks.submit(identity, key, definition)
        response.status_code = 202 if created else 200
        response.headers["Location"] = f"/v1/agent/tasks/{task_id}"
        return await service.agent_tasks.get(identity, task_id)

    @app.get("/v1/agent/tasks/{agent_task_id}", response_model=AgentTaskResult)
    async def get_agent_task(agent_task_id: str, identity: str = Depends(caller)) -> dict:
        return await service.agent_tasks.get(identity, agent_task_id)

    @app.post("/v1/agent/tasks/{agent_task_id}/cancel", response_model=AgentTaskResult,
              responses={202: {"model": AgentTaskResult}})
    async def cancel_agent_task(
        agent_task_id: str, response: Response, identity: str = Depends(caller)
    ) -> dict:
        result = await service.agent_tasks.cancel(identity, agent_task_id)
        response.status_code = 202 if result["status"] == "canceling" else 200
        return result

    @app.post("/v1/jobs", response_model=JobResult, responses={202: {"model": JobResult}})
    async def create_job(
        definition: JobCreate,
        request: Request,
        response: Response,
        identity: str = Depends(caller),
    ) -> dict:
        key = idempotency_key(request)
        if not service.ready:
            raise ServiceError("STATE_UNAVAILABLE", 503, True)
        job_id, created = await service.accept(identity, key, definition)
        event("job_accepted", request_id=request_id(request), job_id=job_id, created=created)
        response.status_code = 202 if created else 200
        response.headers["Location"] = f"/v1/jobs/{job_id}"
        return await service.state.job(identity, job_id)

    @app.post("/v1/files", response_model=FileResult, status_code=201,
              responses={200: {"model": FileResult}},
              openapi_extra={"requestBody": {"required": True, "content": {
                  "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
              }}})
    async def upload_file(request: Request, response: Response,
                          identity: str = Depends(caller)) -> dict:
        if not service.ready:
            raise ServiceError("STATE_UNAVAILABLE", 503, True)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/octet-stream":
            raise ServiceError("INVALID_REQUEST", 400)
        key = idempotency_key(request)
        size, digest = expected_headers(request)
        metadata, created = await service.uploads.receive_stream(
            identity, key, request.stream(), expected_size=size, expected_sha256=digest
        )
        response.status_code = 201 if created else 200
        response.headers["Location"] = f"/v1/files/{metadata['file_id']}"
        return metadata

    @app.get("/v1/files/{file_id}", response_model=FileResult)
    async def uploaded_file_metadata(file_id: str, identity: str = Depends(caller)) -> dict:
        return await service.uploads.metadata(identity, file_id)

    @app.post("/v1/files/{file_id}/release", response_model=FileResult)
    async def release_uploaded_file(file_id: str, identity: str = Depends(caller)) -> dict:
        return await service.uploads.release(identity, file_id)

    async def accept_multipart(request: Request, identity: str, key: str) -> tuple[str, bool]:
        reader = MultipartReader(
            request.stream(), request.headers.get("content-type", ""),
            max_bytes=4 * config.max_input_bytes + config.max_request_bytes + 65536,
            max_parts=5,
        )
        first = await reader.next_part()
        if first is None or first.name != "job" or first.is_file:
            raise ServiceError("INVALID_REQUEST", 400)
        body = bytearray()
        async for chunk in first.stream():
            if len(body) + len(chunk) > config.max_request_bytes:
                raise ServiceError("REQUEST_TOO_LARGE", 413)
            body.extend(chunk)
        try:
            raw = json.loads(body)
            if not isinstance(raw, dict) or not isinstance(raw.get("inputs"), dict):
                raise ValueError
            parts: set[str] = set()
            for name, source in raw["inputs"].items():
                if isinstance(source, dict) and source.get("kind") == "upload":
                    if set(source) != {"kind", "part"} or source["part"] != name:
                        raise ValueError
                    parts.add(name)
                    raw["inputs"][name] = {"kind": "uploaded_file", "file_id": "file_" + "0" * 32}
            if not parts:
                raise ValueError
            JobCreate.model_validate(raw)
        except (ValueError, TypeError, ValidationError):
            raise ServiceError("INVALID_REQUEST", 422) from None
        existing = await service.db.one(
            "SELECT id FROM jobs WHERE caller=? AND idempotency_key=?", (identity, key)
        )
        seen: set[str] = set()
        while (part := await reader.next_part()) is not None:
            if not part.is_file or part.name not in parts or part.name in seen:
                raise ServiceError("INVALID_REQUEST", 400)
            upload_key = "job:" + hashlib.sha256(
                json.dumps([key, part.name], separators=(",", ":")).encode()
            ).hexdigest()
            metadata, _ = await service.uploads.receive_stream(
                identity, upload_key, part.stream(), allow_retired_replay=existing is not None
            )
            raw["inputs"][part.name]["file_id"] = metadata["file_id"]
            seen.add(part.name)
        if seen != parts:
            raise ServiceError("INVALID_REQUEST", 400)
        return await service.accept(identity, key, JobCreate.model_validate(raw))

    @app.post("/v1/jobs/upload", response_model=JobResult,
              responses={202: {"model": JobResult}},
              openapi_extra={"requestBody": {"required": True, "content": {
                  "multipart/form-data": {"schema": {"type": "object", "required": ["job"],
                      "properties": {"job": {"type": "string", "contentMediaType": "application/json",
                          "contentSchema": multipart_job_schema(),
                          "description": "任务 JSON 字符串，必须先于命名文件 parts；steps/params 与 POST /v1/jobs "
                                         "及 MCP 相同，见 contentSchema。至少一个输入为 {kind:upload,part:输入名}。",
                          "examples": [json.dumps({
                              "inputs": {"main": {"kind": "upload", "part": "main"}},
                              "steps": [{"id": "convert", "operation": "video.transcode.v1",
                                         "inputs": {"video": {"source": "main"}}, "params": {"codec": "hevc"}}],
                              "outputs": {"result": {"step": "convert", "output": "video"}},
                          })]}},
                      "additionalProperties": {"type": "string", "format": "binary"}}
              }}}})
    async def upload_job(request: Request, response: Response,
                         identity: str = Depends(caller)) -> dict:
        if not service.ready:
            raise ServiceError("STATE_UNAVAILABLE", 503, True)
        key = idempotency_key(request)
        try:
            async with asyncio.timeout(config.upload_timeout_seconds):
                job_id, created = await accept_multipart(request, identity, key)
        except TimeoutError:
            raise ServiceError("UPLOAD_TIMEOUT", 408, True) from None
        response.status_code = 202 if created else 200
        response.headers["Location"] = f"/v1/jobs/{job_id}"
        return await service.state.job(identity, job_id)

    @app.get("/v1/jobs/{job_id}", response_model=JobResult)
    async def get_job(job_id: str, identity: str = Depends(caller)) -> dict:
        return await service.state.job(identity, job_id)

    @app.post("/v1/jobs/{job_id}/cancel", response_model=JobResult, responses={202: {"model": JobResult}})
    async def cancel_job(job_id: str, response: Response, identity: str = Depends(caller)) -> dict:
        changed = await service.state.cancel(identity, job_id)
        if changed:
            service.scheduler.notify_cancel(job_id)
        response.status_code = 202 if changed else 200
        return await service.state.job(identity, job_id)

    def lease_header(request: Request) -> str | None:
        values = request.headers.getlist("x-artifact-lease-id")
        if not values:
            return None
        if (
            len(values) != 1
            or not 1 <= len(values[0]) <= 128
            or any(ord(char) < 32 or ord(char) > 126 for char in values[0])
        ):
            raise ServiceError("INVALID_REQUEST", 400)
        return values[0]

    @app.get("/v1/artifacts/{artifact_id}", response_model=ArtifactResult)
    async def get_artifact(
        artifact_id: str, request: Request, identity: str = Depends(caller)
    ) -> dict:
        return await service.artifacts.metadata(identity, artifact_id, lease_header(request))

    @app.get("/v1/artifacts/{artifact_id}/members", response_model=MembersResult)
    async def get_members(
        artifact_id: str,
        request: Request,
        cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=100),
        identity: str = Depends(caller),
    ) -> dict:
        return await service.artifacts.members(
            identity, artifact_id, lease_header(request), cursor, limit
        )

    @app.post("/v1/artifacts/{artifact_id}/leases", response_model=LeaseResult, status_code=201)
    async def create_lease(
        artifact_id: str, body: LeaseRequest, identity: str = Depends(caller)
    ) -> dict:
        return await service.artifacts.lease_create(identity, artifact_id, body.duration_seconds)

    @app.patch("/v1/leases/{lease_id}", response_model=LeaseResult)
    async def renew_lease(
        lease_id: str, body: LeaseRequest, identity: str = Depends(caller)
    ) -> dict:
        return await service.artifacts.lease_renew(identity, lease_id, body.duration_seconds)

    @app.delete("/v1/leases/{lease_id}", status_code=204)
    async def release_lease(lease_id: str, identity: str = Depends(caller)) -> Response:
        await service.artifacts.lease_release(identity, lease_id)
        return Response(status_code=204)

    @app.patch("/v1/artifacts/{artifact_id}/retention")
    async def extend_retention(
        artifact_id: str, body: RetentionRequest, identity: str = Depends(caller)
    ) -> dict:
        return await service.artifacts.extend(identity, artifact_id, body.retention_seconds)

    @app.post("/v1/artifacts/{artifact_id}/release")
    async def release_artifact(artifact_id: str, identity: str = Depends(caller)) -> dict:
        return await service.artifacts.release(identity, artifact_id)

    @app.get("/v1/artifacts/{artifact_id}/content", response_class=Response,
             responses={200: {"content": {"application/octet-stream": {}}}, 206: {"content": {"application/octet-stream": {}}}})
    @app.head("/v1/artifacts/{artifact_id}/content", response_class=Response)
    async def artifact_content(
        artifact_id: str, request: Request, identity: str = Depends(caller)
    ) -> Response:
        return ArtifactResponse(
            service.artifacts, identity, artifact_id, lease_id=lease_header(request)
        )

    @app.get("/v1/artifacts/{artifact_id}/files/{member_key:path}", response_class=Response,
             responses={200: {"content": {"application/octet-stream": {}}}, 206: {"content": {"application/octet-stream": {}}}})
    @app.head("/v1/artifacts/{artifact_id}/files/{member_key:path}", response_class=Response)
    async def artifact_member_content(
        artifact_id: str, member_key: str, request: Request, identity: str = Depends(caller)
    ) -> Response:
        return ArtifactResponse(
            service.artifacts,
            identity,
            artifact_id,
            member_key=member_key,
            lease_id=lease_header(request),
        )

    app.router.routes.append(Route("/mcp", endpoint=mcp_app, methods=["GET", "POST", "DELETE"]))
    if config.a2a_enabled:
        from media_service.a2a import register_a2a

        register_a2a(app, service)

    default_openapi = app.openapi

    def documented_openapi() -> dict:
        if app.openapi_schema is None:
            document = default_openapi()
            # FastAPI 序列化 OpenAPI 时递归排除 None，会丢掉条件默认中的“不适用”值。
            # 从共享模型恢复请求 schema，保证 HTTP 与 MCP 看到相同的默认规则。
            for model in (JobCreate, AgentRequest, MediaPlan, AgentTaskResult):
                schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
                document["components"]["schemas"].update(schema.pop("$defs", {}))
                document["components"]["schemas"][model.__name__] = schema
        return app.openapi_schema or {}

    app.openapi = documented_openapi
    return app
