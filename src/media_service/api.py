import asyncio
import hmac
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from media_service.config import Settings
from media_service.content import ArtifactResponse
from media_service.database import wait_settled
from media_service.errors import ServiceError
from media_service.models import (
    ArtifactResult,
    ErrorEnvelope,
    JobCreate,
    JobResult,
    LeaseRequest,
    LeaseResult,
    MembersResult,
    RetentionRequest,
    capabilities,
)
from media_service.telemetry import event

logger = logging.getLogger("media_service")


def error_response(error: ServiceError, request_id: str) -> JSONResponse:
    headers = {"X-Request-ID": request_id}
    if error.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        {"error": error.public(request_id)}, status_code=error.status, headers=headers
    )


class RequestBoundary:
    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

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

        lengths = Headers(scope=scope).getlist("content-length")
        if lengths:
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                await error_response(ServiceError("INVALID_REQUEST", 400), request_id)(
                    scope, receive, send_with_id
                )
                return
            length = lengths[0].lstrip("0") or "0"
            if len(length) > len(str(self.max_bytes)) or int(length) > self.max_bytes:
                await error_response(ServiceError("REQUEST_TOO_LARGE", 413), request_id)(
                    scope, receive, send_with_id
                )
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            data = message.get("body", b"")
            if len(body) + len(data) > self.max_bytes:
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

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await service.start()
        try:
            yield
        finally:
            await wait_settled(asyncio.create_task(service.close()))

    app = FastAPI(title="ffmpeg-agent", version="1.0.0", lifespan=lifespan,
                  responses={code: {"model": ErrorEnvelope} for code in
                             (400, 401, 403, 404, 409, 410, 413, 416, 422, 429, 503)})
    app.state.service = service
    app.add_middleware(RequestBoundary, max_bytes=config.max_request_bytes)

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

    async def caller(request: Request, _: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> str:
        authorizations = request.headers.getlist("authorization")
        if len(authorizations) != 1:
            raise ServiceError("UNAUTHORIZED", 401)
        scheme, separator, value = authorizations[0].partition(" ")
        if scheme.lower() != "bearer" or not separator or not value or not value.isascii():
            raise ServiceError("UNAUTHORIZED", 401)
        matched = None
        supplied = value.encode("ascii")
        for name, account in config.callers.items():
            for token in account.tokens:
                if hmac.compare_digest(supplied, token.get_secret_value().encode("utf-8")):
                    matched = name
        if matched is None:
            raise ServiceError("UNAUTHORIZED", 401)
        return matched

    @app.get("/health/live")
    async def live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict:
        if not service.ready or service.scheduler.failed or not service.db.healthy:
            raise ServiceError("STATE_UNAVAILABLE", 503, True)
        return {"status": "ready"}

    @app.get("/v1/capabilities")
    async def get_capabilities(identity: str = Depends(caller)) -> dict:
        from media_service.media import FORMATS, MEDIA_RULES, PRESETS

        return {
            "operations": capabilities(),
            "limits": config.public_limits(),
            "engine": service.engine,
            "presets": PRESETS,
            "media_rules": MEDIA_RULES,
            "input_formats": FORMATS.split(","),
        }

    @app.post("/v1/jobs", response_model=JobResult, responses={202: {"model": JobResult}})
    async def create_job(
        definition: JobCreate,
        request: Request,
        response: Response,
        identity: str = Depends(caller),
    ) -> dict:
        values = request.headers.getlist("idempotency-key")
        if (
            len(values) != 1
            or not 1 <= len(values[0]) <= 128
            or any(ord(char) < 32 or ord(char) > 126 for char in values[0])
        ):
            raise ServiceError("INVALID_REQUEST", 400)
        if not service.ready:
            raise ServiceError("STATE_UNAVAILABLE", 503, True)
        job_id, created = await service.accept(identity, values[0], definition)
        event("job_accepted", request_id=request_id(request), job_id=job_id, created=created)
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

    return app
