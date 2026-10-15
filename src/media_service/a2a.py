import hashlib
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import TYPE_CHECKING

from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue import Event
from a2a.server.request_handlers import RequestHandler
from a2a.server.routes import (
    ServerCallContextBuilder,
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.types import a2a_pb2 as pb
from a2a.utils.errors import (
    A2A_ERROR_REASONS,
    JSON_RPC_ERROR_CODE_MAP,
    A2AError,
    ContentTypeNotSupportedError,
    InternalError,
    InvalidParamsError,
    PushNotificationNotSupportedError,
    TaskNotCancelableError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from media_service.agent_models import AgentRequest
from media_service.config import Settings
from media_service.errors import MESSAGES, ServiceError

if TYPE_CHECKING:
    from media_service.service import Service

_STATES = {
    "succeeded": pb.TASK_STATE_COMPLETED,
    "partial": pb.TASK_STATE_FAILED,
    "failed": pb.TASK_STATE_FAILED,
    "canceled": pb.TASK_STATE_CANCELED,
    "needs_input": pb.TASK_STATE_REJECTED,
    "unsupported": pb.TASK_STATE_REJECTED,
}
_ERROR_MESSAGES = {
    -32700: "Invalid JSON payload",
    -32600: "Invalid Request",
    -32601: "Method not found",
    -32602: "Invalid params",
    -32603: "Internal error",
    -32001: "Task not found",
    -32002: "Task cannot be canceled",
    -32003: "Push Notification is not supported",
    -32004: "This operation is not supported",
    -32005: "Incompatible content types",
    -32009: "Version not supported",
}


def data_part(data: dict) -> pb.Part:
    return pb.Part(data=ParseDict(data, Value()), media_type="application/json")


def agent_card(settings: Settings) -> pb.AgentCard:
    card = pb.AgentCard(
        name="ffmpeg-agent",
        description=("描述媒体处理需求并提供具名输入，生成受限计划后异步执行。"
                     "SendMessage 返回当前任务，使用 GetTask 轮询最终结果；"
                     "需补充信息时使用新 messageId 提交完整请求。文件通过 HTTP 上传下载。"),
        version=settings.image_version,
        supported_interfaces=[pb.AgentInterface(
            url=f"{settings.public_base_url.rstrip('/')}/a2a",
            protocol_binding="JSONRPC", protocol_version="1.0",
        )],
        capabilities=pb.AgentCapabilities(
            streaming=False, push_notifications=False, extended_agent_card=False,
        ),
        default_input_modes=["application/json", "text/plain"],
        default_output_modes=["application/json"],
        skills=[pb.AgentSkill(
            id="generate_media", name="自然语言媒体处理",
            description="探测、视频/音频转码、音轨提取、视频抽帧和图片缩放。",
            tags=["media", "ffmpeg", "natural-language"],
            input_modes=["application/json", "text/plain"],
            output_modes=["application/json"],
        )],
    )
    if "service_token" not in settings.model_fields_set or settings.service_token is not None:
        card.security_schemes["bearer"].http_auth_security_scheme.CopyFrom(
            pb.HTTPAuthSecurityScheme(scheme="bearer", description="服务调用凭据，下载复用同一凭据")
        )
        requirement = card.security_requirements.add()
        requirement.schemes["bearer"].SetInParent()
    return card


class MediaCallContextBuilder(ServerCallContextBuilder):
    def build(self, request: Request) -> ServerCallContext:
        caller = request.scope.get("state", {}).get("caller")
        if not isinstance(caller, str) or not caller:
            raise InvalidParamsError("UNAUTHORIZED")
        # SDK 版本校验需要此 header；凭据与其他请求头不进入 SDK context。
        return ServerCallContext(state={
            "caller": caller, "headers": {"a2a-version": request.headers.get("a2a-version", "")},
        })


def _caller(context: ServerCallContext) -> str:
    caller = context.state.get("caller")
    if not isinstance(caller, str) or not caller:
        raise InvalidParamsError("UNAUTHORIZED")
    return caller


def _json_numbers(value):
    # Protobuf Value 将 JSON 整数存为 double；恢复整数，供严格的共享模型校验。
    if isinstance(value, float) and value.is_integer() and abs(value) < 2**53:
        return int(value)
    if isinstance(value, dict):
        return {key: _json_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_numbers(item) for item in value]
    return value


def _request(params: pb.SendMessageRequest) -> AgentRequest:
    message = params.message
    if (params.tenant or message.task_id or message.context_id or message.reference_task_ids
            or message.extensions):
        raise UnsupportedOperationError()
    if (not message.message_id or len(message.message_id) > 256
            or any(ord(c) < 32 for c in message.message_id) or message.role != pb.ROLE_USER):
        raise InvalidParamsError()
    configuration = params.configuration
    if configuration.HasField("task_push_notification_config"):
        raise PushNotificationNotSupportedError()
    if configuration.HasField("history_length") and configuration.history_length != 0:
        raise UnsupportedOperationError()
    if configuration.accepted_output_modes and not {
        "application/json", "*/*",
    }.intersection(configuration.accepted_output_modes):
        raise ContentTypeNotSupportedError()
    texts, data = [], []
    for part in message.parts:
        kind = part.WhichOneof("content")
        if kind == "text":
            texts.append(part.text)
        elif kind == "data":
            value = _json_numbers(MessageToDict(part.data))
            if not isinstance(value, dict):
                raise InvalidParamsError()
            data.append(value)
        else:
            raise ContentTypeNotSupportedError()
    if len(data) != 1 or len(texts) > 1:
        raise InvalidParamsError()
    body = data[0]
    if texts:
        if "intent" in body:
            raise InvalidParamsError()
        body = body | {"intent": texts[0]}
    try:
        return AgentRequest.model_validate(body)
    except ValidationError:
        raise InvalidParamsError() from None


def _service_error(error: ServiceError) -> A2AError:
    if error.code == "NOT_FOUND":
        return TaskNotFoundError()
    data = {"code": error.code if error.code in MESSAGES else "STATE_UNAVAILABLE",
            "retryable": error.retryable}
    if error.status >= 500:
        return InternalError(data=data)
    return InvalidParamsError(data=data)


class MediaRequestHandler(RequestHandler):
    def __init__(self, service: Service):
        self.service = service

    async def _project(self, caller: str, result: dict) -> pb.Task:
        task_id = result["agent_task_id"]
        status = result["status"]
        message = result.get("message") or ""
        if status == "partial":
            message = "部分媒体步骤失败；已成功产物仍可获取。"
        elif status in {"needs_input", "unsupported"}:
            message += " 请使用新的 messageId 提交完整请求。"
        task = pb.Task(
            id=task_id, context_id=task_id,
            status=pb.TaskStatus(state=_STATES.get(status, pb.TASK_STATE_WORKING)),
            metadata=result,
        )
        if message:
            task.status.message.CopyFrom(pb.Message(
                message_id=f"{task_id}-{status}", task_id=task_id, context_id=task_id,
                role=pb.ROLE_AGENT, parts=[pb.Part(text=message)],
            ))
        for name, output in (result.get("job") or {}).get("outputs", {}).items():
            artifact_id = output.get("artifact_id")
            if not artifact_id:
                continue
            try:
                metadata = await self.service.artifacts.metadata(caller, artifact_id)
            except ServiceError as error:
                if error.code not in {"ARTIFACT_GONE", "NOT_FOUND"}:
                    raise
                task.artifacts.append(pb.Artifact(
                    artifact_id=artifact_id, name=name,
                    parts=[data_part({"artifact_id": artifact_id, "available": False,
                                      "error": {"code": error.code}})],
                ))
                continue
            # 不公开共享存储位置；集合只返回元数据/成员接口，不能伪装成单文件。
            public_metadata = {key: value for key, value in metadata.items() if key != "access"}
            http = metadata["access"]["http"]
            public_metadata["access"] = {"http": http}
            parts = [data_part(public_metadata)]
            if metadata["kind"] == "file":
                parts.append(pb.Part(url=http["content"], media_type=(
                    metadata["media"].get("content_type") or "application/octet-stream"
                )))
            task.artifacts.append(pb.Artifact(
                artifact_id=artifact_id, name=name, parts=parts,
            ))
        return task

    async def on_message_send(
        self, params: pb.SendMessageRequest, context: ServerCallContext,
    ) -> pb.Task:
        caller = _caller(context)
        request = _request(params)
        key = "a2a:" + hashlib.sha256(params.message.message_id.encode()).hexdigest()
        try:
            task_id, _ = await self.service.agent_tasks.submit(caller, key, request)
            result = await self.service.agent_tasks.get(caller, task_id)
            return await self._project(caller, result)
        except ServiceError as error:
            raise _service_error(error) from None
        except Exception:
            raise InternalError() from None

    async def on_get_task(self, params: pb.GetTaskRequest, context: ServerCallContext) -> pb.Task:
        caller = _caller(context)
        if params.tenant or (params.HasField("history_length") and params.history_length != 0):
            raise UnsupportedOperationError()
        try:
            result = await self.service.agent_tasks.get(caller, params.id)
            return await self._project(caller, result)
        except ServiceError as error:
            raise _service_error(error) from None
        except Exception:
            raise InternalError() from None

    async def on_cancel_task(
        self, params: pb.CancelTaskRequest, context: ServerCallContext,
    ) -> pb.Task:
        caller = _caller(context)
        if params.tenant:
            raise UnsupportedOperationError()
        try:
            result = await self.service.agent_tasks.cancel(caller, params.id)
            if result["status"] in {"succeeded", "partial", "failed", "needs_input", "unsupported"}:
                raise TaskNotCancelableError()
            return await self._project(caller, result)
        except ServiceError as error:
            raise _service_error(error) from None
        except A2AError:
            raise
        except Exception:
            raise InternalError() from None

    async def on_list_tasks(
        self, params: pb.ListTasksRequest, context: ServerCallContext,
    ) -> pb.ListTasksResponse:
        raise UnsupportedOperationError()

    async def on_message_send_stream(
        self, params: pb.SendMessageRequest, context: ServerCallContext,
    ) -> AsyncGenerator[Event]:
        raise UnsupportedOperationError()
        yield

    async def on_subscribe_to_task(
        self, params: pb.SubscribeToTaskRequest, context: ServerCallContext,
    ) -> AsyncGenerator[Event]:
        raise UnsupportedOperationError()
        yield

    async def on_create_task_push_notification_config(
        self, params: pb.TaskPushNotificationConfig, context: ServerCallContext,
    ) -> pb.TaskPushNotificationConfig:
        raise PushNotificationNotSupportedError()

    async def on_get_task_push_notification_config(
        self, params: pb.GetTaskPushNotificationConfigRequest, context: ServerCallContext,
    ) -> pb.TaskPushNotificationConfig:
        raise PushNotificationNotSupportedError()

    async def on_list_task_push_notification_configs(
        self, params: pb.ListTaskPushNotificationConfigsRequest, context: ServerCallContext,
    ) -> pb.ListTaskPushNotificationConfigsResponse:
        raise PushNotificationNotSupportedError()

    async def on_delete_task_push_notification_config(
        self, params: pb.DeleteTaskPushNotificationConfigRequest, context: ServerCallContext,
    ) -> None:
        raise PushNotificationNotSupportedError()

    async def on_get_extended_agent_card(
        self, params: pb.GetExtendedAgentCardRequest, context: ServerCallContext,
    ) -> pb.AgentCard:
        raise UnsupportedOperationError()


def _safe_endpoint(endpoint: Callable[[Request], Awaitable[Response]]):
    async def handle(request: Request) -> Response:
        response = await endpoint(request)
        if isinstance(response, JSONResponse):
            payload = json.loads(bytes(response.body))
            if "error" in payload:
                error = payload["error"]
                code = error.get("code", -32603)
                safe_error = {"code": code, "message": _ERROR_MESSAGES.get(code, "Request failed")}
                reason = next((A2A_ERROR_REASONS[cls] for cls, number in
                               JSON_RPC_ERROR_CODE_MAP.items()
                               if number == code and cls in A2A_ERROR_REASONS), None)
                if reason:
                    metadata = {}
                    details = error.get("data")
                    if isinstance(details, list):
                        for detail in details:
                            if isinstance(detail, dict) and isinstance(detail.get("metadata"), dict):
                                semantic_code = detail["metadata"].get("code")
                                if isinstance(semantic_code, str) and semantic_code in MESSAGES:
                                    metadata = {"code": semantic_code}
                    safe_error["data"] = [{
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": reason, "domain": "a2a-protocol.org", "metadata": metadata,
                    }]
                return JSONResponse({"jsonrpc": "2.0", "id": payload.get("id"),
                                     "error": safe_error})
        return response
    return handle


def register_a2a(app: FastAPI, service: Service) -> None:
    if not service.settings.a2a_enabled:
        return
    # SDK 会记录原始 body 与异常，全部封闭在自己的 logger 层级，避免扩散到服务日志。
    sdk_logger = logging.getLogger("a2a")
    sdk_logger.handlers = [logging.NullHandler()]
    sdk_logger.propagate = False
    for name, logger in logging.Logger.manager.loggerDict.items():
        if name.startswith("a2a.") and isinstance(logger, logging.Logger):
            logger.handlers.clear()
            logger.disabled = True
    routes = create_jsonrpc_routes(
        MediaRequestHandler(service), rpc_url="/a2a", context_builder=MediaCallContextBuilder(),
        enable_v0_3_compat=False,
    )
    for route in routes:
        # 官方 dispatcher 的解析错误会带原输入；只保留协议码与已批准的语义码。
        route.endpoint = _safe_endpoint(route.endpoint)
    add_a2a_routes_to_fastapi(
        app, agent_card_routes=create_agent_card_routes(agent_card(service.settings)),
        jsonrpc_routes=routes,
    )
