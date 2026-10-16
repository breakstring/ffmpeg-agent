import re
from uuid import uuid4

from .errors import ConsumerError, service_code
from .transport import Client, json_bytes, read_json

# mcp==2.1.0 的 LATEST_HANDSHAKE_VERSION；2026-07-28 已改用另一种握手。
MCP_VERSION = "2025-11-25"
A2A_VERSION = "1.0"


def _rpc_result(envelope: dict, request_id: str, prefix: str) -> dict:
    if (envelope.get("jsonrpc") != "2.0" or envelope.get("id") != request_id
            or ("result" in envelope) == ("error" in envelope)):
        raise ConsumerError(prefix + "_INVALID_RESPONSE")
    if "error" in envelope:
        error = envelope["error"]
        if not isinstance(error, dict) or type(error.get("code")) is not int:
            raise ConsumerError(prefix + "_INVALID_RESPONSE")
        raise ConsumerError(prefix + "_RPC_ERROR")
    if not isinstance(envelope["result"], dict):
        raise ConsumerError(prefix + "_INVALID_RESPONSE")
    return envelope["result"]


class MCP:
    def __init__(self, client: Client):
        self.client = client
        self._initialized = False

    def _request(self, method: str, params: dict) -> dict:
        request_id = uuid4().hex
        headers = {"Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": MCP_VERSION}
        result = self.client.api("POST", "/mcp", {
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
        }, headers=headers)
        return _rpc_result(result, request_id, "MCP")

    def __enter__(self):
        if self._initialized:
            raise ConsumerError("MCP_ALREADY_INITIALIZED")
        result = self._request("initialize", {
            "protocolVersion": MCP_VERSION, "capabilities": {},
            "clientInfo": {"name": "ffmpeg-agent-consumer", "version": "0.1.0"},
        })
        if result.get("protocolVersion") != MCP_VERSION:
            raise ConsumerError("MCP_VERSION_MISMATCH")
        capabilities = result.get("capabilities")
        if not isinstance(capabilities, dict) or not isinstance(capabilities.get("tools"), dict):
            raise ConsumerError("MCP_TOOLS_UNAVAILABLE")
        server = result.get("serverInfo")
        if (not isinstance(server, dict) or not isinstance(server.get("name"), str)
                or not isinstance(server.get("version"), str)):
            raise ConsumerError("MCP_INVALID_RESPONSE")
        with self.client.raw_request("POST", "/mcp", json_bytes({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }), {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
             "MCP-Protocol-Version": MCP_VERSION}) as response:
            if response.status not in {202, 204} or read_json(response):
                raise ConsumerError("MCP_INVALID_NOTIFICATION_RESPONSE")
        self._initialized = True
        return self

    def __exit__(self, *_):
        self._initialized = False

    def list_tools(self) -> list[dict]:
        if not self._initialized:
            raise ConsumerError("MCP_NOT_INITIALIZED")
        tools, names, cursors, params = [], set(), set(), {}
        for _ in range(100):
            result = self._request("tools/list", params)
            page = result.get("tools")
            if not isinstance(page, list) or len(tools) + len(page) > 10000:
                raise ConsumerError("MCP_INVALID_TOOLS")
            for tool in page:
                if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", tool["name"])
                        or tool["name"] in names or not isinstance(tool.get("inputSchema"), dict)):
                    raise ConsumerError("MCP_INVALID_TOOLS")
                names.add(tool["name"])
            tools.extend(page)
            cursor = result.get("nextCursor")
            if cursor is None:
                return tools
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ConsumerError("MCP_INVALID_PAGINATION")
            cursors.add(cursor)
            params = {"cursor": cursor}
        raise ConsumerError("MCP_INVALID_PAGINATION")

    def call(self, name: str, args: dict) -> dict:
        if not self._initialized:
            raise ConsumerError("MCP_NOT_INITIALIZED")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name):
            raise ConsumerError("MCP_INVALID_TOOL_NAME")
        if not isinstance(args, dict):
            raise ConsumerError("MCP_INVALID_ARGUMENTS")
        result = self._request("tools/call", {"name": name, "arguments": args})
        structured = result.get("structuredContent")
        if (not isinstance(structured, dict) or not isinstance(result.get("content"), list)
                or type(result.get("isError", False)) is not bool):
            raise ConsumerError("MCP_INVALID_TOOL_RESPONSE")
        if result.get("isError"):
            error = structured.get("error")
            code = service_code(error.get("code")) if isinstance(error, dict) else "MCP_TOOL_ERROR"
            raise ConsumerError(code)
        # JobResult/AgentTaskResult 自带 error 字段；媒体失败仍可通过成功的查询返回。
        # 只有 MCP 的 isError 表示工具调用失败，领域状态交给工作流判定。
        return structured


class A2A:
    def __init__(self, client: Client):
        self.client = client
        self._card = None

    def card(self) -> dict:
        card = self.client.api("GET", "/.well-known/agent-card.json",
                               headers={"A2A-Version": A2A_VERSION})
        interfaces = card.get("supportedInterfaces")
        expected = self.client.base + "/a2a"
        if (not isinstance(interfaces, list) or not interfaces
                or any(not isinstance(item, dict) or item.get("url") != expected
                       or item.get("protocolBinding") != "JSONRPC"
                       or item.get("protocolVersion") != A2A_VERSION for item in interfaces)):
            raise ConsumerError("A2A_UNSUPPORTED_INTERFACE")
        requirements = card.get("securityRequirements", [])
        if not isinstance(requirements, list):
            raise ConsumerError("A2A_AUTH_MISMATCH")
        if self.client.authenticated:
            schemes = card.get("securitySchemes")
            bearer = schemes.get("bearer") if isinstance(schemes, dict) else None
            http = bearer.get("httpAuthSecurityScheme") if isinstance(bearer, dict) else None
            if (not isinstance(http, dict) or not isinstance(http.get("scheme"), str)
                    or http["scheme"].lower() != "bearer"
                    or not any(isinstance(item, dict) and isinstance(item.get("schemes"), dict)
                               and "bearer" in item["schemes"] for item in requirements)):
                raise ConsumerError("A2A_AUTH_MISMATCH")
        elif requirements:
            raise ConsumerError("A2A_AUTH_MISMATCH")
        self._card = card
        return card

    def _request(self, method: str, params: dict) -> dict:
        if self._card is None:
            self.card()
        request_id = uuid4().hex
        envelope = self.client.api("POST", "/a2a", {
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
        }, headers={"A2A-Version": A2A_VERSION})
        return _rpc_result(envelope, request_id, "A2A")

    @staticmethod
    def _task(value: dict, task_id: str | None = None) -> dict:
        identity = value.get("id")
        status = value.get("status")
        states = {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_COMPLETED",
                  "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED",
                  "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"}
        if (not isinstance(identity, str) or not re.fullmatch(r"agent_[a-f0-9]{32}", identity)
                or (task_id is not None and identity != task_id)
                or not isinstance(status, dict) or not isinstance(status.get("state"), str)
                or status["state"] not in states
                or not isinstance(value.get("metadata"), dict)
                or value["metadata"].get("agent_task_id") != identity):
            raise ConsumerError("A2A_INVALID_TASK")
        return value

    def send(self, request: dict, key: str) -> dict:
        if not isinstance(request, dict):
            raise ConsumerError("INVALID_AGENT_REQUEST")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise ConsumerError("INVALID_IDEMPOTENCY_KEY")
        result = self._request("SendMessage", {
            "message": {"messageId": key, "role": "ROLE_USER",
                        "parts": [{"data": request, "mediaType": "application/json"}]},
            "configuration": {"returnImmediately": True, "acceptedOutputModes": ["application/json"],
                              "historyLength": 0},
        })
        task = result.get("task")
        if not isinstance(task, dict):
            raise ConsumerError("A2A_INVALID_TASK")
        return self._task(task)

    def get(self, task_id: str) -> dict:
        if not isinstance(task_id, str) or not re.fullmatch(r"agent_[a-f0-9]{32}", task_id):
            raise ConsumerError("INVALID_AGENT_TASK_ID")
        return self._task(self._request("GetTask", {"id": task_id, "historyLength": 0}), task_id)

    def cancel(self, task_id: str) -> dict:
        if not isinstance(task_id, str) or not re.fullmatch(r"agent_[a-f0-9]{32}", task_id):
            raise ConsumerError("INVALID_AGENT_TASK_ID")
        return self._task(self._request("CancelTask", {"id": task_id}), task_id)
