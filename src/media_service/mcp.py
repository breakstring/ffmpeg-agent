import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, get_args
from urllib.parse import urlsplit
from uuid import uuid4

from mcp import types
from mcp.server import Server, ServerRequestContext
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, TypeAdapter, ValidationError, create_model
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from media_service.auth import authorize
from media_service.config import Settings
from media_service.errors import ServiceError
from media_service.media import capability_document
from media_service.models import (
    OPERATIONS,
    ArtifactResult,
    FileResult,
    Input,
    JobCreate,
    JobResult,
    LeaseRequest,
    LeaseResult,
    MembersResult,
    OutputPolicy,
    RetentionRequest,
    StrictModel,
)
from media_service.telemetry import event

IdempotencyKey = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[\x20-\x7e]+$")]
JobId = Annotated[str, Field(pattern=r"^job_[a-f0-9]{32}$")]
ArtifactId = Annotated[str, Field(pattern=r"^art_[a-f0-9]{32}$")]
LeaseId = Annotated[str, Field(pattern=r"^lease_[a-f0-9]{32}$")]
FileId = Annotated[str, Field(pattern=r"^file_[a-f0-9]{32}$")]
_CLIENT_REQUEST = TypeAdapter(types.ClientRequest)
_CLIENT_NOTIFICATION = TypeAdapter(types.ClientNotification)
_CLIENT_METHODS = frozenset(
    model.model_fields["method"].default for model in get_args(types.ClientRequest)
)


class EmptyArgs(StrictModel):
    pass


class SubmitArgs(StrictModel):
    definition: JobCreate
    idempotency_key: IdempotencyKey


class JobArgs(StrictModel):
    job_id: JobId


class ArtifactArgs(StrictModel):
    artifact_id: ArtifactId
    lease_id: LeaseId | None = None


class MembersArgs(ArtifactArgs):
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)


class CreateLeaseArgs(LeaseRequest):
    artifact_id: ArtifactId


class RenewLeaseArgs(LeaseRequest):
    lease_id: LeaseId


class LeaseArgs(StrictModel):
    lease_id: LeaseId


class ExtendArgs(RetentionRequest):
    artifact_id: ArtifactId


class ReleaseArtifactArgs(StrictModel):
    artifact_id: ArtifactId


class UploadedArgs(StrictModel):
    file_id: FileId


class CapabilitiesResult(StrictModel):
    operations: dict
    limits: dict
    engine: dict
    presets: dict
    media_rules: dict
    input_formats: list[str]
    file_transfer: dict
    media_parameters: dict


class SubmittedResult(JobResult):
    created: bool


class ReleasedLeaseResult(StrictModel):
    lease_id: str
    released: bool


class RetentionResult(StrictModel):
    artifact_id: str
    retain_until: str


class ReleasedArtifactResult(RetentionResult):
    state: str


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    arguments: type[StrictModel]
    result: type[StrictModel] | None
    handler: Callable[[str, Any], Awaitable[dict]]
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = True

    def schema(self) -> types.Tool:
        return types.Tool(
            name=self.name,
            description=self.description,
            input_schema=self.arguments.model_json_schema(),
            output_schema=self.result.model_json_schema() if self.result else {"type": "object"},
            annotations=types.ToolAnnotations(
                read_only_hint=self.read_only,
                destructive_hint=self.destructive,
                idempotent_hint=self.idempotent,
                open_world_hint=False,
            ),
        )


class MCPBoundary:
    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings
        public = urlsplit(settings.public_base_url)
        self.host = public.netloc.lower()
        self.origin = f"{public.scheme}://{public.netloc}".lower()

    def allowed_host(self, host: str) -> bool:
        if host.lower() == self.host:
            return True
        try:
            parsed = urlsplit(f"http://{host}")
            port = parsed.port
        except ValueError:
            return False
        return (parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                and not parsed.username and not parsed.path and not parsed.query
                and not parsed.fragment and (port is None or 1 <= port <= 65535))

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = scope.get("state", {}).get("request_id", uuid4().hex)
        try:
            headers = Headers(scope=scope)
            authorize(self.settings, headers)
            if len(headers.getlist("host")) != 1 or len(headers.getlist("origin")) > 1:
                raise ServiceError("INVALID_REQUEST", 400)
            if not self.allowed_host(headers.get("host", "")):
                raise ServiceError("INVALID_REQUEST", 421)
            origin = headers.get("origin")
            if origin and origin.lower() != self.origin:
                raise ServiceError("FORBIDDEN", 403)
            if scope["method"] == "POST":
                body = bytearray()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    if message["type"] != "http.request":
                        continue
                    body.extend(message.get("body", b""))
                    if len(body) > self.settings.max_request_bytes:
                        raise ServiceError("REQUEST_TOO_LARGE", 413)
                    if not message.get("more_body", False):
                        break
                # SDK 的旧协议解析错误包含原始参数；在进入 SDK 前统一截断该泄露路径。
                try:
                    message = types.jsonrpc_message_adapter.validate_json(bytes(body), by_name=False)
                except (ValidationError, ValueError):
                    raise ServiceError("INVALID_REQUEST", 400) from None
                try:
                    if isinstance(message, types.JSONRPCRequest):
                        _CLIENT_REQUEST.validate_python(
                            message.model_dump(by_alias=True), strict=True, by_name=False
                        )
                    elif isinstance(message, types.JSONRPCNotification):
                        _CLIENT_NOTIFICATION.validate_python(
                            message.model_dump(by_alias=True), strict=True, by_name=False
                        )
                except ValidationError:
                    if isinstance(message, types.JSONRPCRequest):
                        unknown = message.method not in _CLIENT_METHODS
                        await JSONResponse({"jsonrpc": "2.0", "id": message.id,
                            "error": {"code": -32601 if unknown else -32602,
                                      "message": "Method not found" if unknown else
                                                 "Invalid request parameters"}},
                        )(scope, receive, send)
                        return
                    raise ServiceError("INVALID_REQUEST", 400) from None
                original_receive = receive
                replayed = False

                async def replay_body() -> Message:
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await original_receive()

                receive = replay_body
        except ServiceError as error:
            headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else {}
            await JSONResponse(
                {"error": error.public(request_id)}, status_code=error.status, headers=headers
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_mcp(service: Any, settings: Settings) -> tuple[ASGIApp, Server]:
    # SDK 可能记录协议参数及外部异常；业务遥测只从统一语义事件入口输出。
    sdk_logger = logging.getLogger("mcp")
    if not sdk_logger.handlers:
        sdk_logger.addHandler(logging.NullHandler())
    sdk_logger.propagate = False

    async def accept(caller: str, arguments: SubmitArgs) -> dict:
        job_id, created = await service.accept(
            caller, arguments.idempotency_key, arguments.definition
        )
        return (await service.state.job(caller, job_id)) | {"created": created}

    async def list_capabilities(caller: str, arguments: EmptyArgs) -> dict:
        return capability_document(service)

    async def get_job(caller: str, arguments: JobArgs) -> dict:
        return await service.state.job(caller, arguments.job_id)

    async def cancel_job(caller: str, arguments: JobArgs) -> dict:
        if await service.state.cancel(caller, arguments.job_id):
            service.scheduler.notify_cancel(arguments.job_id)
        return await service.state.job(caller, arguments.job_id)

    async def get_artifact(caller: str, arguments: ArtifactArgs) -> dict:
        return await service.artifacts.metadata(caller, arguments.artifact_id, arguments.lease_id)

    async def list_members(caller: str, arguments: MembersArgs) -> dict:
        return await service.artifacts.members(
            caller, arguments.artifact_id, arguments.lease_id, arguments.cursor, arguments.limit
        )

    async def create_lease(caller: str, arguments: CreateLeaseArgs) -> dict:
        return await service.artifacts.lease_create(
            caller, arguments.artifact_id, arguments.duration_seconds
        )

    async def renew_lease(caller: str, arguments: RenewLeaseArgs) -> dict:
        return await service.artifacts.lease_renew(
            caller, arguments.lease_id, arguments.duration_seconds
        )

    async def release_lease(caller: str, arguments: LeaseArgs) -> dict:
        await service.artifacts.lease_release(caller, arguments.lease_id)
        return {"lease_id": arguments.lease_id, "released": True}

    async def extend_artifact(caller: str, arguments: ExtendArgs) -> dict:
        return await service.artifacts.extend(
            caller, arguments.artifact_id, arguments.retention_seconds
        )

    async def release_artifact(caller: str, arguments: ReleaseArtifactArgs) -> dict:
        return await service.artifacts.release(caller, arguments.artifact_id)

    async def get_uploaded_file(caller: str, arguments: UploadedArgs) -> dict:
        return await service.uploads.metadata(caller, arguments.file_id)

    async def release_uploaded_file(caller: str, arguments: UploadedArgs) -> dict:
        return await service.uploads.release(caller, arguments.file_id)

    definitions = [
        ToolDefinition("list_capabilities", "查询版本化媒体操作、限制和 HTTP 文件传输入口。",
                       EmptyArgs, CapabilitiesResult, list_capabilities, True),
        ToolDefinition("submit_job", "提交异步媒体任务图；幂等键复用须保持定义相同。立即返回任务，随后查询状态。",
                       SubmitArgs, SubmittedResult, accept),
        ToolDefinition("get_job", "查询任务状态和产物 ID；inputs_released 表示输入已停止使用。",
                       JobArgs, JobResult, get_job, True),
        ToolDefinition("cancel_job", "请求取消任务；查询到 inputs_released 后才能确认输入已释放。",
                       JobArgs, JobResult, cancel_job, destructive=True),
        ToolDefinition("get_artifact", "查询产物和 HTTP 下载地址；下载须携带同一服务 Token。",
                       ArtifactArgs, ArtifactResult, get_artifact, True),
        ToolDefinition("list_artifact_members", "分页查询抽帧集合，包括逐项成功或失败结果。",
                       MembersArgs, MembersResult, list_members, True),
        ToolDefinition("create_lease", "创建产物使用租约；共享存储读取前申请并在读取期间保护文件。",
                       CreateLeaseArgs, LeaseResult, create_lease, idempotent=False),
        ToolDefinition("renew_lease", "续租尚未失效且仍可续租的产物使用租约。",
                       RenewLeaseArgs, LeaseResult, renew_lease, idempotent=False),
        ToolDefinition("release_lease", "结束产物使用租约；不再读取文件后调用。",
                       LeaseArgs, ReleasedLeaseResult, release_lease, destructive=True),
        ToolDefinition("extend_artifact", "延长产物保留期；秒数从正式发布时刻计算。",
                       ExtendArgs, RetentionResult, extend_artifact),
        ToolDefinition("release_artifact", "结束产物保留；现有引用和租约释放后由后台回收。",
                       ReleaseArtifactArgs, ReleasedArtifactResult, release_artifact,
                       destructive=True),
        ToolDefinition("get_uploaded_file", "查询 HTTP 上传得到的文件 ID、完整性和保留状态。",
                       UploadedArgs, FileResult, get_uploaded_file, True),
        ToolDefinition("release_uploaded_file", "结束上传文件保留；已有任务停止使用后由后台回收。",
                       UploadedArgs, FileResult, release_uploaded_file, destructive=True),
    ]
    intent_tools = (
        ("probe_media", "media.probe.v1", "提交媒体探测任务"),
        ("transcode_video", "video.transcode.v1", "提交视频转码任务，可按需覆盖默认参数"),
        ("transcode_audio", "audio.transcode.v1", "提交音频转码任务，可按需覆盖默认参数"),
        ("extract_audio", "video.extract_audio.v1", "提交视频音轨提取任务"),
        ("extract_frames", "video.extract_frames.v1", "提交指定时间点的逐项抽帧任务"),
        ("resize_image", "image.resize.v1", "提交图片缩放任务"),
    )

    def intent_handler(operation: str):
        async def submit(caller: str, arguments: Any) -> dict:
            port, output, _, _ = OPERATIONS[operation]
            definition = JobCreate.model_validate({
                "inputs": {"main": arguments.source.model_dump()},
                "steps": [{"id": "step", "operation": operation,
                           "inputs": {port: {"source": "main"}},
                           "params": arguments.params.model_dump()}],
                "outputs": {"result": {"step": "step", "output": output}},
                "output_policy": arguments.output_policy.model_dump(),
            })
            return await accept(caller, SubmitArgs(
                definition=definition, idempotency_key=arguments.idempotency_key
            ))
        return submit

    for name, operation, description in intent_tools:
        params_model = OPERATIONS[operation][2]
        arguments = create_model(
            f"{name}Args", __base__=StrictModel,
            source=(Input, ...), idempotency_key=(IdempotencyKey, ...),
            params=(params_model, Field(default_factory=params_model)),
            output_policy=(OutputPolicy, Field(default_factory=OutputPolicy)),
        )
        definitions.append(ToolDefinition(
            name, f"{description}；params 可省略，与 HTTP 同参。立即返回任务 ID，使用 get_job 查询。文件通过 HTTP 上传下载。",
            arguments, SubmittedResult, intent_handler(operation),
        ))
    tools = {definition.name: definition for definition in definitions}

    async def list_tools(
        context: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[definition.schema() for definition in definitions])

    async def call_tool(
        context: ServerRequestContext, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        request_id = getattr(getattr(context.request, "state", None), "request_id", uuid4().hex)
        try:
            if context.request is None:
                raise ServiceError("UNAUTHORIZED", 401)
            caller = authorize(settings, context.request.headers)
            tool = tools.get(params.name)
            if tool is None:
                raise ServiceError("UNSUPPORTED_OPERATION")
            try:
                arguments = tool.arguments.model_validate(params.arguments or {})
            except ValidationError:
                raise ServiceError("INVALID_REQUEST") from None
            result = await tool.handler(caller, arguments)
            if tool.result:
                result = tool.result.model_validate(result).model_dump(mode="json")
            event("mcp_tool_finished", request_id=request_id, operation=tool.name,
                  status="succeeded")
            return types.CallToolResult(
                content=[types.TextContent(text=json.dumps(result, ensure_ascii=False))],
                structured_content=result,
            )
        except ServiceError as error:
            public = {"error": error.public(request_id)}
        except Exception:
            public = {"error": ServiceError("STATE_UNAVAILABLE", 503, True).public(request_id)}
        event("mcp_tool_finished", request_id=request_id, status="failed",
              code=public["error"]["code"])
        return types.CallToolResult(
            content=[types.TextContent(text=json.dumps(public, ensure_ascii=False))],
            structured_content=public, is_error=True,
        )

    server = Server(
        "ffmpeg-agent", version="1.0.0", on_list_tools=list_tools, on_call_tool=call_tool,
        instructions=("远程媒体处理服务。先通过 HTTP 上传获取 file_id，或使用受控 HTTP URL、"
                      "共享存储和已有产物引用。工具提交任务后立即返回；通过 get_job 查询后，"
                      "从 get_artifact 的 HTTP 地址携带同一服务 Token 下载。"),
    )
    application = server.streamable_http_app(
        streamable_http_path="/mcp", json_response=True, stateless_http=True,
        max_request_body_size=settings.max_request_bytes,
        # SDK 会把被拒绝的 Host/Origin 写入日志；外层边界先完成相同检查并返回脱敏错误。
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return MCPBoundary(application, settings), server
