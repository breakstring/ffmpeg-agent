"""用临时环境文件与本机假模型验收根 Compose 的配置，不读取真实凭据。"""

import argparse
import importlib.util
import json
import os
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("env_support", ROOT / "deploy/env_support.py")
assert _spec and _spec.loader
env_support = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(env_support)

MODEL = "env-verification-model"
OUTPUT_BUDGET = 768
PLANNING_TIMEOUT = 23
AGENT_REQUEST = {
    "intent": "请先询问需要哪种输出格式。",
    "inputs": {"image": {"kind": "uploaded_file", "file_id": "file_" + "0" * 32}},
}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25",
}
ENVIRONMENT_FIELDS = {
    "MEDIA_CONFIG_FILE", "MEDIA_SERVICE_TOKEN", "MEDIA_PUBLIC_BASE_URL", "MEDIA_LLM_ENABLED",
    "MEDIA_A2A_ENABLED", "MEDIA_LLM_API_KEY", "MEDIA_LLM_MODEL", "MEDIA_LLM_BASE_URL",
    "MEDIA_LLM_OUTPUT_MODE", "MEDIA_PLANNING_TIMEOUT_SECONDS", "MEDIA_PLANNING_CONCURRENCY",
    "MEDIA_PLANNING_MAX_OUTPUT_TOKENS", "MEDIA_MAX_AGENT_REQUESTS",
}


class VerificationFailure(Exception):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise VerificationFailure(code)


class MockModel(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, key: str):
        self.key = key
        self.records: list[dict[str, bool]] = []
        self.failures = 0
        # Docker Desktop 通过 host.docker.internal 访问；只接受本次随机假凭据。
        super().__init__(("0.0.0.0", 0), ModelHandler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    def handle_error(self, request, client_address) -> None:
        self.failures += 1


class ModelHandler(BaseHTTPRequestHandler):
    server: MockModel

    def log_message(self, format, *args) -> None:
        pass

    def do_POST(self) -> None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            require(0 < size < 1024 * 1024, "invalid_mock_request")
            self.connection.settimeout(10)
            body = json.loads(self.rfile.read(size))
            checks = {
                "path": self.path == "/v1/chat/completions",
                "key": self.headers.get("Authorization") == "Bearer " + self.server.key,
                "model": body.get("model") == MODEL,
                "output_budget": body.get("max_tokens") == OUTPUT_BUDGET,
                "messages": bool(body.get("messages")),
            }
            self.server.records.append(checks)
            if not all(checks.values()):
                self.respond(400, {"error": {"message": "invalid verification request"}})
                return
            outcome = {"decision": "needs_input", "message": "请指定目标输出格式。", "plan": None}
            self.respond(200, {
                "id": "env-verification-completion", "object": "chat.completion",
                "created": int(time.time()), "model": MODEL,
                "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": json.dumps(outcome, ensure_ascii=False),
                }, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            })
        except Exception:
            self.server.failures += 1
            self.respond(500, {"error": {"message": "verification model failure"}})

    def respond(self, status: int, body: dict) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class Harness:
    def __init__(self, image: str, directory: Path, model: MockModel, token: str):
        self.image = image
        self.directory = directory
        self.model = model
        self.token = token
        self.project = "ffmpeg-env-" + uuid4().hex[:16]
        self.container = ""
        self.started = False
        self.response_count = 0
        self.log_scans = 0
        self.needles = (token.encode(), model.key.encode())
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("MEDIA_", "COMPOSE_"))
        }
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.environment_path = directory / ".env"
        self.config_path = directory / "media-config.json"
        self.overlay_path = directory / "overlay.json"
        self.compose = [
            "compose", "--env-file", str(self.environment_path), "-p", self.project,
            "-f", str(ROOT / "docker-compose.yaml"), "-f", str(self.overlay_path),
        ]

    def clean(self, value: bytes | str) -> None:
        encoded = value.encode() if isinstance(value, str) else value
        require(all(needle not in encoded for needle in self.needles), "secret_exposed")

    def docker(self, *args: str, timeout: float = 90) -> str:
        self.clean(" ".join(args))
        try:
            result = subprocess.run(
                ["docker", *args], capture_output=True, text=True, env=self.environment,
                cwd=self.directory, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise VerificationFailure("docker_timeout") from None
        require(result.returncode == 0, "docker_command_failed")
        return result.stdout.strip()

    def configure(self, *, enabled: bool, protected: bool) -> None:
        config = json.loads((ROOT / "deploy/media-config.example.json").read_text())
        require(not set(config) & env_support.ENV_ONLY_FIELDS, "json_contains_environment_fields")
        self.clean(json.dumps(config))
        env_support.atomic_write(self.config_path, json.dumps(config) + "\n", 0o644)
        values = {
            "COMPOSE_PROJECT_NAME": self.project, "MEDIA_IMAGE": self.image,
            "MEDIA_BIND_ADDRESS": "127.0.0.1", "MEDIA_PORT": self.port,
            "MEDIA_CONFIG_PATH": str(self.config_path), "MEDIA_PUBLIC_BASE_URL": self.base,
            "MEDIA_SERVICE_TOKEN": self.token if protected else None,
            "MEDIA_LLM_ENABLED": enabled, "MEDIA_A2A_ENABLED": enabled,
            "MEDIA_LLM_API_KEY": self.model.key,
            "MEDIA_LLM_MODEL": MODEL,
            "MEDIA_LLM_BASE_URL": f"http://host.docker.internal:{self.model.server_port}/v1",
            "MEDIA_LLM_OUTPUT_MODE": "prompted", "MEDIA_PLANNING_TIMEOUT_SECONDS": PLANNING_TIMEOUT,
            "MEDIA_PLANNING_CONCURRENCY": 1, "MEDIA_PLANNING_MAX_OUTPUT_TOKENS": OUTPUT_BUDGET,
            "MEDIA_MAX_AGENT_REQUESTS": 7,
            # 未列入 Compose environment 的 MEDIA_* 不应自动进入容器。
            "MEDIA_ENV_VERIFICATION_UNLISTED": "must-not-enter-container",
        }
        env_support.atomic_write(self.environment_path, "".join(
            key + "=" + env_support.encode_value(value) + "\n"
            for key, value in values.items()
        ), 0o600)
        env_support.atomic_write(self.overlay_path, json.dumps({
            "services": {"media": {
                "restart": "no", "extra_hosts": ["host.docker.internal:host-gateway"],
            }},
        }) + "\n", 0o600)
        require(self.environment_path.stat().st_mode & 0o077 == 0, "env_permissions_mismatch")

    def recreate(self, *, enabled: bool, protected: bool) -> None:
        previous = self.container
        self.configure(enabled=enabled, protected=protected)
        self.started = True
        self.docker(*self.compose, "up", "-d", "--no-build", "--pull", "never",
                    "--force-recreate", "media")
        self.container = self.docker(*self.compose, "ps", "-q", "media")
        require(bool(self.container) and self.container != previous, "container_not_recreated")
        info = json.loads(self.docker("inspect", self.container))[0]
        require(info["Image"] == self.image, "image_identity_changed")
        require(info["Config"]["Labels"]["com.docker.compose.project"] == self.project,
                "compose_ownership_mismatch")
        require(info["HostConfig"]["ReadonlyRootfs"], "root_filesystem_writable")
        require(info["Config"]["User"] == "10001:10001", "container_user_mismatch")
        require(info["HostConfig"]["Init"] is True, "container_init_missing")
        actual = dict(value.split("=", 1) for value in info["Config"]["Env"])
        # 镜像自身携带版本字段；其余 MEDIA_* 必须来自 Compose 的显式白名单。
        require({key for key in actual if key.startswith("MEDIA_")}
                == ENVIRONMENT_FIELDS | {"MEDIA_IMAGE_VERSION"},
                "environment_whitelist_mismatch")
        require(actual["MEDIA_SERVICE_TOKEN"] == (self.token if protected else "null"),
                "service_token_not_loaded")
        require(actual["MEDIA_LLM_ENABLED"] == str(enabled).lower()
                and actual["MEDIA_A2A_ENABLED"] == str(enabled).lower(), "flags_not_loaded")
        require(actual["MEDIA_LLM_API_KEY"] == self.model.key, "model_key_not_loaded")
        mounts = {mount["Destination"]: mount for mount in info["Mounts"]}
        require(not mounts["/run/secrets/media-config.json"]["RW"], "config_mount_writable")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                if self.request("GET", "/health/ready")[0] == 200:
                    return
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.25)
        raise VerificationFailure("service_not_ready")

    def request(self, method: str, path: str, body=None, *, token=None, headers=None):
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        if token is not None:
            request_headers["Authorization"] = "Bearer " + token
        payload = body if isinstance(body, bytes) else (
            json.dumps(body).encode() if body is not None else None
        )
        request = urllib.request.Request(
            self.base + path, data=payload, headers=request_headers, method=method,
        )
        try:
            response = self.opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read()
            self.clean(content)
            self.clean(str(response.headers))
            self.response_count += 1
            return response.status, json.loads(content) if content else None

    def mcp_tools(self, token: str | None) -> set[str]:
        status, body = self.request("POST", "/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
        }, token=token, headers=MCP_HEADERS)
        require(status == 200 and "result" in body, "mcp_tools_failed")
        return {tool["name"] for tool in body["result"]["tools"]}

    def protected_routes(self) -> dict[str, int]:
        statuses = {}
        calls = {
            "http": ("GET", "/v1/capabilities", None, {}),
            "mcp": ("POST", "/mcp", {
                "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
            }, MCP_HEADERS),
            "a2a": ("POST", "/a2a", {
                "jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "unknown"},
            }, {"A2A-Version": "1.0"}),
            "a2a_card": ("GET", "/.well-known/agent-card.json", None, {}),
        }
        for name, (method, path, body, headers) in calls.items():
            for suffix, credential in (("missing", None), ("wrong", "invalid-verification-token")):
                status, _ = self.request(method, path, body, token=credential, headers=headers)
                require(status == 401, "authentication_boundary_mismatch")
                statuses[name + "_" + suffix] = status
        return statuses

    def disabled(self, *, protected: bool) -> dict:
        token = self.token if protected else None
        status, capabilities = self.request("GET", "/v1/capabilities", token=token)
        require(status == 200, "capabilities_failed")
        require(capabilities["agent"]["enabled"] is False
                and capabilities["agent"]["a2a_enabled"] is False, "disabled_flags_mismatch")
        agent_status, body = self.request("POST", "/v1/agent/tasks", AGENT_REQUEST, token=token,
                                         headers={"Idempotency-Key": uuid4().hex})
        require(agent_status == 503 and body["error"]["code"] == "AGENT_DISABLED",
                "disabled_agent_accepted")
        require("generate_media" not in self.mcp_tools(token), "disabled_mcp_tool_visible")
        card_status, _ = self.request("GET", "/.well-known/agent-card.json", token=token)
        a2a_status, _ = self.request("POST", "/a2a", {}, token=token)
        require(card_status == 404 and a2a_status == 404, "disabled_a2a_route_visible")
        return {"capabilities_status": status, "agent_status": agent_status,
                "a2a_card_status": card_status, "a2a_status": a2a_status,
                "mcp_authorized": True, "model_tools_absent": True}

    def enabled(self) -> dict:
        status, capabilities = self.request("GET", "/v1/capabilities", token=self.token)
        require(status == 200 and capabilities["agent"]["enabled"]
                and capabilities["agent"]["a2a_enabled"], "enabled_flags_mismatch")
        limits = capabilities["agent"]["limits"]
        require(limits["planning_concurrency"] == 1 and limits["max_pending_requests"] == 7
                and limits["planning_timeout_seconds"] == PLANNING_TIMEOUT,
                "planning_limits_not_loaded")
        require("generate_media" in self.mcp_tools(self.token), "enabled_mcp_tool_missing")
        card_status, card = self.request("GET", "/.well-known/agent-card.json", token=self.token)
        require(card_status == 200, "a2a_card_failed")
        require(any(interface["url"] == self.base + "/a2a"
                    and interface["protocolBinding"] == "JSONRPC"
                    and interface["protocolVersion"] == "1.0"
                    for interface in card["supportedInterfaces"]), "a2a_card_origin_mismatch")
        require(bool(card.get("securityRequirements")), "a2a_card_security_missing")
        fixture = (ROOT / "tests/fixtures/image_jpeg_portrait_odd.jpg").read_bytes()
        upload_status, uploaded = self.request("POST", "/v1/files", fixture, token=self.token,
            headers={"Content-Type": "application/octet-stream", "Idempotency-Key": uuid4().hex})
        require(upload_status == 201, "fixture_upload_failed")
        request = AGENT_REQUEST | {"inputs": {
            "image": {"kind": "uploaded_file", "file_id": uploaded["file_id"]},
        }}
        submit_status, task = self.request("POST", "/v1/agent/tasks", request, token=self.token,
                                          headers={"Idempotency-Key": uuid4().hex})
        require(submit_status == 202, "agent_submission_failed")
        task_id = task["agent_task_id"]
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            query_status, task = self.request("GET", "/v1/agent/tasks/" + task_id, token=self.token)
            require(query_status == 200, "agent_query_failed")
            if task["status"] in {"needs_input", "unsupported", "failed", "canceled", "succeeded"}:
                break
            time.sleep(0.1)
        require(task["status"] == "needs_input" and task["decision"] == "needs_input"
                and task["job_id"] is None, "mock_planning_did_not_finish")
        a2a_status, a2a = self.request("POST", "/a2a", {
            "jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": task_id},
        }, token=self.token, headers={"A2A-Version": "1.0"})
        require(a2a_status == 200 and a2a.get("result", {}).get("id") == task_id,
                "a2a_task_query_failed")
        require(len(self.model.records) == 1 and all(self.model.records[0].values())
                and self.model.failures == 0, "mock_model_configuration_mismatch")
        return {"capabilities_status": status, "a2a_card_status": card_status,
                "upload_status": upload_status, "agent_submit_status": submit_status,
                "a2a_query_status": a2a_status, "mcp_model_tools_visible": True,
                "planning_limits_loaded": True, "a2a_origin_loaded": True,
                "a2a_same_task_visible": True, "needs_input_completed": True,
                "model_key_matched": True, "model_name_matched": True,
                "model_output_budget_matched": True}

    def scan_logs(self) -> None:
        result = subprocess.run(
            ["docker", "logs", self.container], capture_output=True, env=self.environment, timeout=30,
        )
        require(result.returncode == 0, "container_logs_unavailable")
        self.clean(result.stdout + result.stderr)
        self.log_scans += 1

    def cleanup(self) -> None:
        if self.started:
            self.docker(*self.compose, "down", "--volumes", "--timeout", "20")
        for command in (("ps", "-aq"), ("volume", "ls", "-q"), ("network", "ls", "-q")):
            require(not self.docker(*command, "--filter", "label=com.docker.compose.project="
                                    + self.project), "owned_resources_remain")


def verify(image: str) -> dict:
    require(bool(re.fullmatch(r"sha256:[a-f0-9]{64}", image)), "exact_image_id_required")
    token = secrets.token_urlsafe(48) + "$literal\\'"
    key = secrets.token_urlsafe(48) + "$literal\\'"
    model = MockModel(key)
    model.thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="ffmpeg-env-verification-") as temp:
            harness = Harness(image, Path(temp), model, token)
            report = {"image_id": image}
            try:
                require(not harness.docker("ps", "-aq", "--filter",
                    "label=com.docker.compose.project=" + harness.project), "project_collision")
                harness.recreate(enabled=False, protected=True)
                report["protected_disabled"] = harness.disabled(protected=True)
                report["protected_http_statuses"] = harness.protected_routes()
                require(not model.records, "disabled_model_was_called")
                harness.scan_logs()
                harness.recreate(enabled=True, protected=True)
                report["protected_enabled_http_statuses"] = harness.protected_routes()
                report["enabled"] = harness.enabled()
                harness.scan_logs()
                harness.recreate(enabled=False, protected=False)
                report["open_disabled"] = harness.disabled(protected=False)
                require(len(model.records) == 1, "disabled_model_was_called")
                harness.scan_logs()
                report.update({
                    "passed": True, "container_recreations": 3, "mock_model_requests": 1,
                    "public_responses_scanned": harness.response_count,
                    "container_log_scans": harness.log_scans,
                    "secret_disclosure_absent": True, "environment_whitelist_verified": True,
                    "structural_json_only": True, "private_env_permissions": True,
                    "readonly_rootfs": True, "nonroot_uid": 10001,
                    "root_env_not_loaded": True, "inherited_media_compose_environment_removed": True,
                })
            finally:
                harness.cleanup()
            report["owned_resources_removed"] = True
            return report
    finally:
        model.shutdown()
        model.server_close()
        model.thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description="隔离验证根 Compose 的 .env 与模型配置加载")
    parser.add_argument("--image", required=True, help="精确 sha256 镜像 ID")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = verify(args.image)
    except VerificationFailure as error:
        report = {"passed": False, "failure": str(error)}
    except Exception:
        # 外部异常可能含环境或响应正文，验收结果仅保留固定错误码。
        report = {"passed": False, "failure": "verification_failed"}
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text)
    print(text, end="")
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
