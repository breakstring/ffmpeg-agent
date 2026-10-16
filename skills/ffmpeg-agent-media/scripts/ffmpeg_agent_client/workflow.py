from __future__ import annotations

import os
import re
import stat
import time
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from .artifacts import save_artifact
from .errors import ConsumerError, service_code
from .protocols import A2A, MCP
from .state import Record, fingerprint
from .transport import Client

NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
TERMINAL = {"succeeded", "partial", "failed", "canceled", "needs_input", "unsupported"}
STATUSES = TERMINAL | {"queued", "planning", "planned", "running", "canceling"}


def identity(value: object, prefix: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(prefix + r"_[a-f0-9]{32}", value):
        raise ConsumerError("INVALID_REMOTE_ID")
    return value


def input_paths(values: list[str]) -> dict[str, Path]:
    result = {}
    for value in values:
        name, separator, path = value.partition("=")
        if not separator or not NAME.fullmatch(name) or not path or name in result:
            raise ConsumerError("INVALID_INPUT_ARGUMENT")
        result[name] = Path(path).expanduser().absolute()
    if not 1 <= len(result) <= 4:
        raise ConsumerError("INVALID_INPUT_COUNT")
    return result


def capabilities(client: Client) -> dict:
    document = client.api("GET", "/v1/capabilities")
    if not isinstance(document.get("operations"), dict) or not isinstance(document.get("limits"), dict):
        raise ConsumerError("INVALID_CAPABILITIES")
    return document


def check(client: Client, protocol: str) -> dict:
    result = {"ok": True, "authentication": "bearer" if client.authenticated else "open",
              "checks": {}}
    document = None
    for name, route, expected in [("live", "/health/live", "ok"),
                                   ("ready", "/health/ready", "ready")]:
        try:
            if client.api("GET", route).get("status") != expected:
                raise ConsumerError("INVALID_HEALTH_RESPONSE")
            result["checks"][name] = {"ok": True}
        except ConsumerError as error:
            result["ok"] = False
            result["checks"][name] = {"ok": False, "code": error.code}
    try:
        document = capabilities(client)
        result["checks"]["http"] = {"ok": True, "operations": sorted(document["operations"])}
    except ConsumerError as error:
        result["ok"] = False
        result["checks"]["http"] = {"ok": False, "code": error.code}
    if protocol in {"mcp", "all"}:
        try:
            with MCP(client) as mcp:
                names = [tool["name"] for tool in mcp.list_tools()]
                if not {"list_capabilities", "submit_job", "get_job"}.issubset(names):
                    raise ConsumerError("MCP_REQUIRED_TOOLS_MISSING")
                mcp.call("list_capabilities", {})
            result["checks"]["mcp"] = {"ok": True, "tools": names}
        except ConsumerError as error:
            result["ok"] = False
            result["checks"]["mcp"] = {"ok": False, "code": error.code}
    if protocol in {"a2a", "all"}:
        enabled = (document or {}).get("agent", {}).get("a2a_enabled")
        if enabled is False and protocol == "all":
            result["checks"]["a2a"] = {"enabled": False}
        else:
            try:
                A2A(client).card()
                result["checks"]["a2a"] = {"ok": True, "enabled": True}
            except ConsumerError as error:
                result["ok"] = False
                result["checks"]["a2a"] = {"ok": False, "code": error.code}
    return result


class Workflow:
    def __init__(self, client: Client, record: Record):
        self.client, self.record = client, record
        self.stack = ExitStack()
        self.mcp = None
        self.a2a = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stack.__exit__(*args)

    @property
    def data(self):
        return self.record.data

    def _protocol(self):
        transport = self.data.get("transport", "http")
        if transport == "mcp" and self.mcp is None:
            self.mcp = self.stack.enter_context(MCP(self.client))
        if transport == "a2a" and self.a2a is None:
            self.a2a = A2A(self.client)
        return transport

    def prepare(self, paths: dict[str, Path], spec: dict | None = None,
                output_dir: Path | None = None, transport: str = "http"):
        if self.data:
            # run/upload 可重复调用，但只有完全相同的本地输入与请求才能复用该记录。
            if set(paths) != set(self.data["inputs"]):
                raise ConsumerError("STATE_REQUEST_MISMATCH")
            for name, path in paths.items():
                current = fingerprint(path)
                if any(current[key] != self.data["inputs"][name][key] for key in current):
                    raise ConsumerError("INPUT_CHANGED")
            if spec is not None and (self.data.get("spec") != spec
                    or self.data.get("transport") != transport
                    or self.data.get("output_dir") != str(output_dir)):
                raise ConsumerError("STATE_REQUEST_MISMATCH")
            return
        if output_dir is not None:
            self._new_output_path(output_dir)
        self.record.data = {"version": 1, "base": self.client.base,
                            "request_id": uuid4().hex, "created_at": time.time(),
                            "inputs": {name: fingerprint(path) for name, path in paths.items()},
                            "phase": "prepared", "transport": transport}
        if spec is not None:
            self.data.update(spec=spec, output_dir=str(output_dir))
        self.record.save()

    @staticmethod
    def _new_output_path(path: Path):
        if path.exists() or path.is_symlink() or ".." in path.parts:
            raise ConsumerError("OUTPUT_EXISTS_OR_UNSAFE")
        for parent in path.parents:
            if parent.exists() and not stat.S_ISDIR(parent.lstat().st_mode):
                raise ConsumerError("UNSAFE_OUTPUT_PATH")

    def upload(self):
        self.record.require_existing()
        for name, entry in self.data["inputs"].items():
            if entry.get("released"):
                raise ConsumerError("INPUT_ALREADY_RELEASED")
            if entry.get("file_id"):
                continue
            current = fingerprint(Path(entry["path"]))
            if any(current[key] != entry[key] for key in current):
                raise ConsumerError("INPUT_CHANGED")
            started = entry.setdefault("upload_started_at", time.time())
            # 服务至少保留幂等键 24 小时；未知接收结果超出此窗口时禁止重新提交。
            if time.time() - started >= 86400:
                raise ConsumerError("UPLOAD_ACCEPTANCE_WINDOW_EXPIRED")
            self.record.save()
            result = self.client.upload(Path(entry["path"]), self.data["request_id"] + ":" + name,
                                        entry["sha256"])
            if (result.get("state") != "ready" or result.get("size_bytes") != entry["size_bytes"]
                    or result.get("sha256") != entry["sha256"]):
                raise ConsumerError("UPLOAD_IDENTITY_MISMATCH")
            entry["file_id"] = identity(result.get("file_id"), "file")
            entry["retain_until"] = result.get("retain_until")
            self.record.save()
        self.data["phase"] = "uploaded"
        self.record.save()
        return {"request_id": self.data["request_id"], "inputs": self.references()}

    def references(self):
        return {name: {"kind": "uploaded_file", "file_id": identity(entry.get("file_id"), "file")}
                for name, entry in self.data["inputs"].items()}

    def _request(self):
        spec = self.data.get("spec")
        if not isinstance(spec, dict):
            raise ConsumerError("NO_REQUEST_TO_RESUME")
        if "request" in self.data:
            return
        document = capabilities(self.client)
        mode = spec["mode"]
        if mode == "intent":
            if not document.get("agent", {}).get("enabled"):
                raise ConsumerError("AGENT_DISABLED")
            if self.data["transport"] == "a2a" and not document["agent"].get("a2a_enabled"):
                raise ConsumerError("A2A_DISABLED")
            request = {"intent": spec["intent"], "inputs": self.references()}
        elif mode == "definition":
            request = {**spec["definition"], "inputs": self.references()}
        else:
            operation = spec["operation"]
            contract = document["operations"].get(operation)
            if not isinstance(contract, dict):
                raise ConsumerError("UNSUPPORTED_OPERATION")
            ports = contract.get("input_ports")
            output = contract.get("output_port")
            if (not isinstance(ports, list) or len(ports) != 1
                    or not isinstance(ports[0], str) or not NAME.fullmatch(ports[0])
                    or not isinstance(output, str) or not NAME.fullmatch(output)):
                raise ConsumerError("INVALID_CAPABILITIES")
            if len(self.data["inputs"]) != 1:
                raise ConsumerError("SINGLE_OPERATION_REQUIRES_ONE_INPUT")
            step = {"id": "process", "operation": operation,
                    "inputs": {ports[0]: {"source": next(iter(self.data["inputs"]))}}}
            if spec.get("params") is not None:
                step["params"] = spec["params"]
            request = {"inputs": self.references(), "steps": [step],
                       "outputs": {"result": {"step": "process", "output": output}}}
        if self.data["transport"] == "a2a" and mode != "intent":
            raise ConsumerError("A2A_REQUIRES_INTENT")
        self.data["request"] = request
        self.record.save()

    def submit(self):
        if self.data.get("job_id") or self.data.get("agent_task_id"):
            return
        self.upload()
        self._request()
        started = self.data.setdefault("submit_started_at", time.time())
        if time.time() - started >= 86400:
            raise ConsumerError("ACCEPTANCE_WINDOW_EXPIRED")
        self.data["phase"] = "submitting"
        self.record.save()
        transport = self._protocol()
        agent = self.data["spec"]["mode"] == "intent"
        request, key = self.data["request"], self.data["request_id"]
        if transport == "a2a":
            result = self.a2a.send(request, key)["metadata"]
        elif transport == "mcp":
            result = self.mcp.call("generate_media" if agent else "submit_job",
                {"request" if agent else "definition": request, "idempotency_key": key})
        else:
            result = self.client.api("POST", "/v1/agent/tasks" if agent else "/v1/jobs",
                                     request, {"Idempotency-Key": key})
        field, prefix = ("agent_task_id", "agent") if agent else ("job_id", "job")
        self.data[field] = identity(result.get(field), prefix)
        self.data["phase"] = "accepted"
        self.record.save()

    def attach(self, job_id: str | None, agent_task_id: str | None, output_dir: Path):
        self.record.require_existing()
        field, value, prefix = ("agent_task_id", agent_task_id, "agent") if agent_task_id else (
            "job_id", job_id, "job")
        identity(value, prefix)
        if self.data.get("job_id") or self.data.get("agent_task_id"):
            if self.data.get(field) != value or self.data.get("output_dir") != str(output_dir):
                raise ConsumerError("STATE_REQUEST_MISMATCH")
            return
        if not all(entry.get("file_id") for entry in self.data["inputs"].values()):
            raise ConsumerError("UPLOADS_NOT_COMPLETE")
        self._new_output_path(output_dir)
        self.data.update({field: value, "output_dir": str(output_dir), "phase": "accepted",
                          "transport": "http", "external_submission": True})
        self.record.save()

    def query(self, cancel: bool = False):
        self.record.require_existing()
        agent = bool(self.data.get("agent_task_id"))
        field, prefix = ("agent_task_id", "agent") if agent else ("job_id", "job")
        task_id = identity(self.data.get(field), prefix)
        transport = self._protocol()
        if transport == "a2a":
            result = (self.a2a.cancel(task_id) if cancel else self.a2a.get(task_id))["metadata"]
        elif transport == "mcp":
            name = ("cancel_" if cancel else "get_") + ("agent_task" if agent else "job")
            result = self.mcp.call(name, {field: task_id})
        else:
            route = "/v1/agent/tasks/" if agent else "/v1/jobs/"
            result = self.client.api("POST" if cancel else "GET", route + task_id + (
                "/cancel" if cancel else ""))
        if (result.get(field) != task_id or not isinstance(result.get("status"), str)
                or result["status"] not in STATUSES):
            raise ConsumerError("INVALID_TASK_RESPONSE")
        job = result.get("job") if agent else result
        if agent and result.get("job_id"):
            self.data["job_id"] = identity(result["job_id"], "job")
        if job is not None and (not isinstance(job, dict)
                or job.get("job_id") != self.data.get("job_id")
                or type(job.get("inputs_released")) is not bool
                or not isinstance(job.get("status"), str) or job["status"] not in STATUSES
                or not isinstance(job.get("outputs"), dict)):
            raise ConsumerError("INVALID_TASK_RESPONSE")
        status = result["status"]
        released = bool(job and job["inputs_released"])
        if agent and job is None and status in {"needs_input", "unsupported", "failed", "canceled"}:
            released = True
        summary = {field: task_id, "status": status, "inputs_released": released,
                   "terminal": status in TERMINAL and released, "outputs": {}}
        if job:
            summary["job_id"] = job["job_id"]
            summary["terminal"] = summary["terminal"] and job["status"] in TERMINAL
            for name, output in job["outputs"].items():
                if (not isinstance(name, str) or not NAME.fullmatch(name)
                        or not isinstance(output, dict) or not isinstance(output.get("status"), str)
                        or output["status"] not in STATUSES | {
                            "pending", "skipped"}):
                    raise ConsumerError("INVALID_TASK_RESPONSE")
                entry = {"status": output["status"]}
                if output.get("artifact_id"):
                    entry["artifact_id"] = identity(output["artifact_id"], "art")
                if isinstance(output.get("error"), dict):
                    entry["code"] = service_code(output["error"].get("code"))
                summary["outputs"][name] = entry
        if isinstance(result.get("error"), dict):
            summary["code"] = service_code(result["error"].get("code"))
        if isinstance(result.get("decision"), str) and result["decision"] in {
                "execute", "needs_input", "unsupported"}:
            summary["decision"] = result["decision"]
        if status in {"needs_input", "unsupported"}:
            message = self.client.safe_message(result.get("message"))
            if message:
                summary["message"] = message
        self.data["last_status"] = summary
        self.record.save()
        return summary

    def _output_directory(self):
        path = Path(self.data["output_dir"])
        marker = path / ".ffmpeg-agent-owner"
        if not path.exists():
            self._new_output_path(path)
            path.mkdir(mode=0o700, parents=True)
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(self.data["request_id"])
                stream.flush()
                os.fsync(stream.fileno())
        for component in [path, *path.parents]:
            if not stat.S_ISDIR(component.lstat().st_mode):
                raise ConsumerError("UNSAFE_OUTPUT_PATH")
        if not stat.S_ISREG(marker.lstat().st_mode) or marker.read_text() != self.data["request_id"]:
            raise ConsumerError("OUTPUT_NOT_OWNED")
        return path

    def collect(self, summary: dict):
        if not summary["terminal"]:
            raise ConsumerError("TASK_STILL_ACTIVE")
        path = self._output_directory()
        outputs = {}
        for name, output in summary["outputs"].items():
            artifact_id = output.get("artifact_id")
            if not artifact_id:
                outputs[name] = {"partial": True, "code": output.get("code", "OUTPUT_UNAVAILABLE")}
                continue
            lease_id = None
            try:
                lease = self.client.api("POST", "/v1/artifacts/" + artifact_id + "/leases",
                                         {"duration_seconds": 900})
                lease_id = identity(lease.get("lease_id"), "lease")
                artifact = self.client.api("GET", "/v1/artifacts/" + artifact_id,
                                            headers={"X-Artifact-Lease-Id": lease_id})
                if artifact.get("artifact_id") != artifact_id:
                    raise ConsumerError("INVALID_ARTIFACT_METADATA")
                outputs[name] = save_artifact(self.client, artifact, path / name, lease_id)
            except ConsumerError as error:
                outputs[name] = {"partial": True, "code": error.code}
            finally:
                if lease_id:
                    try:
                        self.client.api("DELETE", "/v1/leases/" + lease_id)
                    except ConsumerError:
                        # 有界租约自行到期，清理失败不能抹去已经校验过的下载结果。
                        self.data["lease_release_pending"] = True
            self.data["collected"] = outputs
            self.record.save()
        self.data["collected"] = outputs
        self.data["phase"] = "collected"
        self.record.save()
        return outputs

    def release(self):
        self.record.require_existing()
        if self.data.get("job_id") or self.data.get("agent_task_id"):
            if not self.query()["terminal"]:
                raise ConsumerError("TASK_STILL_ACTIVE")
        elif self.data.get("submit_started_at"):
            raise ConsumerError("SUBMISSION_UNCONFIRMED")
        for entry in self.data["inputs"].values():
            if not entry.get("file_id") or entry.get("released"):
                continue
            result = self.client.api("POST", "/v1/files/" + identity(entry["file_id"], "file")
                                     + "/release")
            if result.get("state") not in {"retiring", "deleting", "deleted"}:
                raise ConsumerError("INVALID_RELEASE_RESPONSE")
            entry["released"] = True
            self.record.save()
        return {"uploads_released": all(entry.get("released") for entry in self.data["inputs"].values()
                                         if entry.get("file_id"))}

    def wait(self, seconds: float, cancel: bool = False):
        deadline = time.monotonic() + seconds
        if cancel:
            self.query(cancel=True)
        while True:
            summary = self.query()
            if summary["terminal"]:
                result = dict(summary)
                if summary["status"] in {"succeeded", "partial"}:
                    result["downloads"] = self.collect(summary)
                    result.update(self.release())
                    partial = summary["status"] == "partial" or any(
                        item.get("partial") for item in result["downloads"].values())
                    result["ok"] = not partial
                    return result, 2 if partial else 0
                result["ok"] = cancel and summary["status"] == "canceled"
                return result, 0 if result["ok"] else 1
            if time.monotonic() >= deadline:
                return {**summary, "ok": False, "code": "WAIT_TIMEOUT", "resumable": True}, 3
            time.sleep(min(1, max(0, deadline - time.monotonic())))
