"""显式使用本地模型配置，在精确隔离镜像上验收自然语言 HTTP/MCP/A2A。"""

import argparse
import asyncio
import hashlib
import importlib.util
import io
import json
import logging
import re
import socket
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import a2a_pb2 as pb
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from PIL import Image
from pydantic import SecretStr

from media_service.agent_models import AgentTaskResult
from media_service.config import Settings
from media_service.errors import MESSAGES
from media_service.local_env import load_local_env

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "agent_parameter_verification", ROOT / "deploy/verify-parameters.py"
)
assert spec and spec.loader
parameters = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parameters)
shared = parameters.shared
TERMINAL = {"succeeded", "partial", "failed", "canceled", "needs_input", "unsupported"}
MODEL_FIELDS = {
    "llm_api_key", "llm_model", "llm_base_url", "llm_output_mode",
    "planning_timeout_seconds", "planning_max_output_tokens",
}
CASES = (
    {"name": "http_image", "transport": "http", "sources": ("image",),
     "intent": "将 image 转为 PNG 图片，宽度精确为 80 像素，等比缩放，提供一个图片输出。",
     "operations": ["image.resize.v1"], "expected": "succeeded"},
    {"name": "mcp_audio", "transport": "mcp", "sources": ("audio",),
     "intent": "将 audio 转为 FLAC 音频，采样率 48000 Hz，单声道，提供一个音频输出。",
     "operations": ["audio.transcode.v1"], "expected": "succeeded"},
    {"name": "a2a_video", "transport": "a2a", "sources": ("video",),
     "intent": "请从 video 生成三个输出：1. H.264 编码的 MP4 视频，宽度 320 像素，保持比例；"
               "2. 提取音轨并转成 MP3；3. 在 0 毫秒和 500 毫秒分别抽取 PNG 封面，"
               "每张宽度 80 像素并保持比例。三个输出都需要保留。",
     "operations": ["video.transcode.v1", "video.extract_audio.v1", "video.extract_frames.v1"],
     "expected": "succeeded"},
    {"name": "http_probe", "transport": "http", "sources": ("video", "audio"),
     "intent": "分别探测 video 和 audio 的媒体格式与流属性，提供两份独立的 JSON 探测报告。",
     "operations": ["media.probe.v1", "media.probe.v1"], "expected": "succeeded"},
    {"name": "http_unsupported", "transport": "http", "sources": ("video",),
     "intent": "识别 video 中的语音，生成逐字中文字幕并烧录进视频。需要完成字幕生成和烧录，"
               "不要用其他处理代替。",
     "operations": [], "expected": "unsupported"},
    {"name": "http_needs_input", "transport": "http", "sources": ("video",),
     "intent": "把 video 转成我之前指定的目标格式，但本次没有提供之前的要求。"
               "目标格式需要由我确认，请先询问缺失的格式，不要自行选择。",
     "operations": [], "expected": "needs_input"},
)


class VerificationFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def require(condition: bool, code: str) -> None:
    if not condition:
        raise VerificationFailure(code)


def stable_code(value: Any) -> str:
    return value if isinstance(value, str) and value in MESSAGES else "VERIFICATION_FAILED"


class DisclosureMonitor:
    def __init__(self, private_values: list[str]):
        self.needles = tuple({
            encoded for value in private_values if value for encoded in
            (value.encode(), json.dumps(value, ensure_ascii=True)[1:-1].encode())
        })
        self.checks: dict[str, bool] = {}

    def observe(self, value: bytes | str | dict, category: str = "public_responses") -> bool:
        if isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False)
        if isinstance(value, str):
            value = value.encode()
        clean = all(needle not in value for needle in self.needles)
        self.checks[category] = self.checks.get(category, True) and clean
        return clean

    async def response(self, response: httpx.Response) -> None:
        require(self.observe(await response.aread()), "sensitive_material_exposed")


def read_model_settings(path: Path) -> dict:
    # 验收只读取显式文件中的模型字段，不让环境文件改动存储、来源或容器权限。
    values = load_local_env(path, environ={})
    selected = {name: values[name] for name in MODEL_FIELDS if name in values}
    require(isinstance(selected.get("llm_api_key"), SecretStr), "missing_model_configuration")
    require(bool(selected.get("llm_model")) and bool(selected.get("llm_base_url")),
            "missing_model_configuration")
    return selected


def private_configuration(token: str, public_url: str, model: dict) -> dict:
    config = {
        "state_dir": "/var/lib/ffmpeg-agent/state", "work_dir": "/var/lib/ffmpeg-agent/work",
        "service_token": token, "service_caller": "demo",
        "storages": {"output": {"root": "/media/output", "role": "output", "callers": ["demo"]}},
        "callers": {"demo": {"default_output_storage": "output", "source_rules": []}},
        "worker_concurrency": 1, "runtime_mode": "container", "engine_series": "7.1",
        "public_base_url": public_url, "upload_timeout_seconds": 60,
        "max_input_bytes": 4 * 1024**2, "max_upload_bytes": 32 * 1024**2,
        "max_output_bytes": 32 * 1024**2, "max_work_bytes": 128 * 1024**2,
        "llm_enabled": True, "a2a_enabled": True,
        "planning_concurrency": 1, "max_agent_requests": 8,
        **{name: value for name, value in model.items() if name in MODEL_FIELDS},
    }
    Settings.model_validate(config)
    # 只在即将写入受保护配置文件时解包，不能传给 docker -e 或命令行。
    config["llm_api_key"] = config["llm_api_key"].get_secret_value()
    return config


class Harness(shared.Harness):
    def __init__(self, image: str, model: dict):
        super().__init__(image, True)
        self.name = f"ffmpeg-agent-agent-{self.owner[:12]}"
        self.volumes = {part: f"{self.name}-{part}" for part in ("state", "work", "output")}
        self.model = model
        self.monitor = DisclosureMonitor([
            model["llm_api_key"].get_secret_value(), self.token, model["llm_base_url"],
        ])

    def write_private_configuration(self) -> Path:
        parent = Path(self.temp.name)
        parent.chmod(0o700)
        path = parent / "config.json"
        shared.env_support.write_runtime_configuration(
            private_configuration(self.token, self.base, self.model), path,
        )
        # 父目录限制主机读取；单文件只读 bind 需允许容器的非 root UID 读取。
        path.chmod(0o644)
        return path

    def container_arguments(self, config_path: Path) -> list[str]:
        mounts = shared.env_support.environment_mount(config_path.with_suffix(".env"))
        for part, volume in self.volumes.items():
            target = f"/var/lib/ffmpeg-agent/{part}" if part != "output" else "/media/output"
            mounts.extend(["--mount", f"type=volume,src={volume},dst={target}"])
        mounts.extend([
            "--mount", f"type=bind,src={config_path},dst=/run/secrets/media-config.json,readonly",
        ])
        port = self.base.rsplit(":", 1)[1]
        return [
            "run", "-d", "--name", self.name, "--label", f"{shared.LABEL}={self.owner}",
            "--init", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
            "--cpus", "2", "--memory", "2g", "--pids-limit", "128",
            "--env", "MEDIA_TEST_ENGINE_SERIES=7.1", "-p", f"127.0.0.1:{port}:8080",
            *mounts, self.image,
        ]

    def start(self) -> None:
        require(bool(re.fullmatch(r"sha256:[a-f0-9]{64}", self.image)), "exact_image_id_required")
        # 显式固定发布端口，保证重启后能力卡与官方客户端仍使用同一 origin。
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        config_path = self.write_private_configuration()
        self.stage = "owned_volume_creation"
        for volume in self.volumes.values():
            require(not json.loads(shared.docker("volume", "inspect", volume, check=False) or "[]"),
                    "resource_name_collision")
            shared.docker("volume", "create", "--label", f"{shared.LABEL}={self.owner}", volume)
        self.stage = "owned_container_creation"
        require(not json.loads(shared.docker("inspect", self.name, check=False) or "[]"),
                "resource_name_collision")
        arguments = self.container_arguments(config_path)
        require(self.monitor.observe(" ".join(arguments), "container_arguments"),
                "sensitive_material_exposed")
        shared.docker(*arguments)
        self.stage = "container_baseline"
        info = json.loads(shared.docker("inspect", self.name))[0]
        require(info["Image"] == self.image, "image_identity_changed")
        host = info["HostConfig"]
        require(host["ReadonlyRootfs"] and host["Init"] is True, "container_isolation_mismatch")
        require(host["NanoCpus"] == 2_000_000_000 and host["Memory"] == 2 * 1024**3,
                "container_limits_mismatch")
        require(int(shared.docker("exec", self.name, "python", "-c",
                                  "import os; print(os.geteuid())")) > 0, "root_execution")
        require(self.monitor.observe(json.dumps(info["Config"]["Env"]), "container_environment"),
                "sensitive_material_exposed")
        require(not any(m["Destination"] in {"/media/input", "/fault-tests"} for m in info["Mounts"]),
                "unexpected_input_or_fault_mount")
        expected_base = self.base
        self.connect()
        require(self.base == expected_base, "published_origin_mismatch")

    def scan_logs(self) -> None:
        result = subprocess.run(["docker", "logs", self.name], capture_output=True, timeout=30)
        require(result.returncode == 0, "log_read_failed")
        require(self.monitor.observe(result.stdout + result.stderr, "container_logs"),
                "sensitive_material_exposed")


async def get_task(http: httpx.AsyncClient, task_id: str) -> dict:
    response = await http.get(f"/v1/agent/tasks/{task_id}")
    require(response.status_code == 200, "agent_query_failed")
    return AgentTaskResult.model_validate(response.json()).model_dump(mode="json")


async def final_task(http: httpx.AsyncClient, task_id: str, wait_seconds: float = 180) -> dict:
    async with asyncio.timeout(wait_seconds):
        while True:
            task = await get_task(http, task_id)
            if task["status"] in TERMINAL:
                return task
            await asyncio.sleep(0.2)


async def a2a_client(http: httpx.AsyncClient, harness: Harness):
    card = await A2ACardResolver(http, harness.base).get_agent_card()
    require(any(item.url == harness.base + "/a2a" and item.protocol_binding == "JSONRPC"
                and item.protocol_version == "1.0" for item in card.supported_interfaces),
            "a2a_card_origin_or_version_mismatch")
    require(not card.capabilities.streaming and not card.capabilities.push_notifications,
            "unexpected_a2a_capabilities")
    require(bool(card.security_requirements), "a2a_authentication_not_advertised")
    for index in range(len(card.supported_interfaces) - 1, -1, -1):
        if card.supported_interfaces[index].url != harness.base + "/a2a":
            del card.supported_interfaces[index]
    return ClientFactory(ClientConfig(
        httpx_client=http, streaming=False, polling=True, supported_protocol_bindings=["JSONRPC"],
    )).create(card)


async def submit(transport, request, key, http, session, a2a, monitor, *, replay=False) -> dict:
    if transport == "http":
        response = await http.post("/v1/agent/tasks", json=request,
                                   headers={"Idempotency-Key": key})
        if response.status_code not in (200, 202):
            body = response.json()
            raise VerificationFailure(stable_code(body.get("error", {}).get("code")))
        require(response.status_code == (200 if replay else 202), "http_replay_status_mismatch")
        raw = response.json()
    elif transport == "mcp":
        result = await session.call_tool("generate_media", {"request": request, "idempotency_key": key})
        require(monitor.observe(result.model_dump_json()), "sensitive_material_exposed")
        if result.is_error:
            raise VerificationFailure(stable_code((result.structured_content or {}).get("error", {}).get("code")))
        raw = result.structured_content
    else:
        message = pb.SendMessageRequest(
            message=pb.Message(message_id=key, role=pb.ROLE_USER,
                               parts=[pb.Part(data=ParseDict(request, Value()))]),
            configuration=pb.SendMessageConfiguration(return_immediately=True),
        )
        events = [event async for event in a2a.send_message(message)]
        require(len(events) == 1 and events[0].HasField("task"), "a2a_async_task_missing")
        task = events[0].task
        require(monitor.observe(MessageToDict(task)), "sensitive_material_exposed")
        raw = await get_task(http, task.id)
    return AgentTaskResult.model_validate(raw).model_dump(mode="json")


def inspect_bytes(container: str, data: bytes, operation: str) -> dict:
    if operation in {"image.resize.v1", "video.extract_frames.v1"}:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            require(image.format == "PNG" and image.width == 80, "image_format_or_width_mismatch")
            expected_height = 107 if operation == "image.resize.v1" else 45
            require(image.height == expected_height, "image_aspect_ratio_mismatch")
            require(not image.getexif() and "icc_profile" not in image.info,
                    "image_metadata_not_removed")
            return {"format": image.format, "width": image.width, "height": image.height}
    if operation == "media.probe.v1":
        result = json.loads(data)
        require(result["type"] in {"audio", "video"} and bool(result["streams"]),
                "probe_report_invalid")
        return {"type": result["type"], "stream_count": len(result["streams"])}
    result = parameters.probe_bytes(container, data)
    streams = result["streams"]
    if operation == "video.transcode.v1":
        video = next(item for item in streams if item["codec_type"] == "video")
        require((video["codec_name"], video["width"], video["height"]) == ("h264", 320, 180),
                "video_encoding_or_size_mismatch")
        require("mp4" in result["format"]["format_name"], "video_container_mismatch")
        return {"codec": "h264", "width": 320, "height": 180}
    require(len(streams) == 1 and streams[0]["codec_type"] == "audio", "audio_stream_mismatch")
    audio = streams[0]
    if operation == "audio.transcode.v1":
        require((audio["codec_name"], int(audio["sample_rate"]), audio["channels"]) ==
                ("flac", 48000, 1), "audio_encoding_mismatch")
    else:
        require(audio["codec_name"] == "mp3", "extracted_audio_codec_mismatch")
    return {"codec": audio["codec_name"], "sample_rate": int(audio["sample_rate"]),
            "channels": audio["channels"]}


async def inspect_outputs(harness: Harness, http: httpx.AsyncClient, result: dict) -> list[dict]:
    job = result["job"]
    require(job is not None and job["inputs_released"] is True, "media_work_not_finished")
    operations = {step["id"]: step["operation"] for step in result["plan"]["steps"]}
    results = []
    for name, binding in result["plan"]["outputs"].items():
        operation = operations[binding["step"]]
        artifact_id = job["outputs"][name]["artifact_id"]
        response = await http.get(f"/v1/artifacts/{artifact_id}")
        require(response.status_code == 200, "artifact_metadata_failed")
        artifact = response.json()
        files = []
        if operation == "video.extract_frames.v1":
            require(artifact["kind"] == "collection", "frames_not_collection")
            response = await http.get(f"/v1/artifacts/{artifact_id}/members", params={"limit": 10})
            require(response.status_code == 200, "frame_members_failed")
            members = response.json()
            require(members["next_cursor"] is None and len(members["items"]) == 2,
                    "frame_count_mismatch")
            require([item["requested_time_ms"] for item in members["items"]] == [0, 500],
                    "frame_timestamps_mismatch")
            for item in members["items"]:
                require(item["error"] is None, "frame_member_failed")
                data = await parameters.download_member(harness, http, artifact, item)
                actual = await asyncio.to_thread(inspect_bytes, harness.name, data, operation)
                files.append({"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                              "actual": actual, "requested_time_ms": item["requested_time_ms"],
                              "actual_time_ms": item["actual_time_ms"]})
        else:
            require(artifact["kind"] == "file", "unexpected_artifact_kind")
            data = await shared.download(harness, http, artifact)
            actual = await asyncio.to_thread(inspect_bytes, harness.name, data, operation)
            files.append({"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                          "actual": actual})
        require(sum(item["size_bytes"] for item in files) == artifact["size_bytes"],
                "artifact_size_mismatch")
        results.append({"operation": operation, "files": files})
    return results


async def verify_case(harness, http, session, a2a, sources, case, record):
    started = time.monotonic()
    request = {"intent": case["intent"], "inputs": {name: sources[name] for name in case["sources"]}}
    key = "agent-verification-" + uuid.uuid4().hex
    try:
        record["stage"] = "submit"
        accepted = await submit(case["transport"], request, key, http, session, a2a, harness.monitor)
        record["stage"] = "idempotency_replay"
        replay = await submit(case["transport"], request, key, http, session, a2a, harness.monitor,
                              replay=True)
        require(replay["agent_task_id"] == accepted["agent_task_id"], "replayed_agent_changed")
        record["stage"] = "planning_and_media_completion"
        result = await final_task(
            http, accepted["agent_task_id"],
            wait_seconds=harness.model.get("planning_timeout_seconds", 60) + 120,
        )
        record["status"] = result["status"]
        record["decision"] = result["decision"]
        record["operations"] = sorted(step["operation"] for step in (result["plan"] or {}).get("steps", []))
        if result["error"]:
            record["error_code"] = stable_code(result["error"]["code"])
        if result["job"] and result["job"]["status"] == "failed":
            errors = [step.get("error") for step in result["job"]["steps"] if step.get("error")]
            if errors:
                record["error_code"] = stable_code(errors[0]["code"])
        require(result["status"] == case["expected"], "unexpected_terminal_state")
        require(record["operations"] == sorted(case["operations"]), "planned_operations_mismatch")
        if case["expected"] == "succeeded":
            record["stage"] = "media_output_validation"
            record["outputs"] = await inspect_outputs(harness, http, result)
            expected_outputs = 2 if case["name"] == "http_probe" else len(case["operations"])
            require(len(record["outputs"]) == expected_outputs, "declared_output_count_mismatch")
            if case["name"] == "http_probe":
                require({item["files"][0]["actual"]["type"] for item in record["outputs"]} ==
                        {"audio", "video"}, "probe_source_coverage_mismatch")
        else:
            require(result["job_id"] is None and result["plan"] is None,
                    "nonexecuting_decision_created_job")
        record["stage"] = "terminal_replay"
        replay = await submit(case["transport"], request, key, http, session, a2a, harness.monitor,
                              replay=True)
        require((replay["agent_task_id"], replay["job_id"]) ==
                (result["agent_task_id"], result["job_id"]), "terminal_replay_changed_job")
        if case["transport"] == "mcp":
            queried = await shared.call(session, "get_agent_task", {"agent_task_id": result["agent_task_id"]})
            require(harness.monitor.observe(queried), "sensitive_material_exposed")
            require(queried == result, "mcp_task_projection_mismatch")
        elif case["transport"] == "a2a":
            queried = await a2a.get_task(pb.GetTaskRequest(id=result["agent_task_id"]))
            require(queried.status.state == pb.TASK_STATE_COMPLETED, "a2a_terminal_projection_mismatch")
            require(queried.metadata["job_id"] == result["job_id"], "a2a_job_projection_mismatch")
            require(len(queried.artifacts) == len(record["outputs"]), "a2a_artifact_count_mismatch")
        record.update({"stage": "complete", "passed": True, "idempotent_agent_and_job": True})
        return result
    except Exception as error:
        record["passed"] = False
        record.setdefault("error_code", error.code if isinstance(error, VerificationFailure)
                          else "VERIFICATION_FAILED")
        if isinstance(error, VerificationFailure) and error.code == "sensitive_material_exposed":
            raise
        return None
    finally:
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)


async def restart_check(harness, completed: dict) -> dict:
    harness.stage = "restart_persisted_task_query"
    await asyncio.to_thread(shared.docker, "stop", "--time", "30", harness.name, timeout=40)
    info = json.loads(await asyncio.to_thread(shared.docker, "inspect", harness.name))[0]
    require(info["State"]["Running"] is False, "container_stop_unconfirmed")
    await asyncio.to_thread(shared.docker, "start", harness.name)
    original_base = harness.base
    await asyncio.to_thread(harness.connect)
    require(harness.base == original_base, "restart_origin_changed")
    await shared.ready(harness)
    async with httpx.AsyncClient(base_url=harness.base, headers=harness.headers, timeout=30,
                                  event_hooks={"response": [harness.monitor.response]}) as http:
        current = await get_task(http, completed["agent_task_id"])
        require(current["agent_task_id"] == completed["agent_task_id"] and
                current["job_id"] == completed["job_id"] and current["plan"] == completed["plan"] and
                current["job"]["outputs"] == completed["job"]["outputs"], "restart_task_identity_changed")
        official = await a2a_client(http, harness)
        projected = await official.get_task(pb.GetTaskRequest(id=current["agent_task_id"]))
        require(projected.metadata["job_id"] == current["job_id"], "restart_a2a_projection_changed")
    return {"same_agent_task": True, "same_job": True, "same_plan": True,
            "same_outputs": True, "query_only_no_resubmission": True}


def selected_cases(names: list[str] | None = None) -> tuple[dict, ...]:
    if names is None:
        return CASES
    selected = tuple(case for case in CASES if case["name"] in names)
    require(bool(selected) and len(selected) == len(names), "invalid_case_selection")
    return selected


async def verify(image: str, model: dict, report: dict) -> None:
    require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}", image)), "invalid_image_reference")
    info = json.loads(await asyncio.to_thread(shared.docker, "image", "inspect", image))[0]
    image_id = info["Id"]
    require(bool(re.fullmatch(r"sha256:[a-f0-9]{64}", image_id)), "exact_image_id_required")
    report["image_id"] = image_id
    contents = parameters.load_fixtures()
    report["fixtures"] = {name: {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                          for name, data in contents.items()}
    harness = Harness(image_id, model)
    report["resource_owner_id"] = harness.owner
    report["model"] = model["llm_model"]
    report["model_output_mode"] = model.get("llm_output_mode", "prompted")
    report["planning_timeout_seconds"] = model.get("planning_timeout_seconds", 60)
    try:
        require(harness.monitor.observe(report, "report"), "sensitive_material_exposed")
        await asyncio.to_thread(harness.start)
        harness.stage = "container_readiness"
        await shared.ready(harness)
        report["checks"].append("nonroot_readonly_exact_image_with_private_model_configuration")
        harness.stage = "protocol_discovery_and_authentication"
        completed = None
        async with httpx.AsyncClient(base_url=harness.base, headers=harness.headers, timeout=30,
                                     event_hooks={"response": [harness.monitor.response]}) as http:
            for method, path in (("POST", "/v1/agent/tasks"), ("POST", "/mcp"),
                                 ("POST", "/a2a"), ("GET", "/.well-known/agent-card.json")):
                denied = await http.request(method, path, content=b"not-read",
                                            headers={"Authorization": "Bearer invalid"})
                require(denied.status_code == 401, "entry_authentication_bypass")
            official = await a2a_client(http, harness)
            sources = {kind: await parameters.upload(http, data) for kind, data in contents.items()}
            async with shared.remote(harness) as session:
                tools = await session.list_tools()
                require(harness.monitor.observe(tools.model_dump_json()), "sensitive_material_exposed")
                require(len(tools.tools) == 22, "mcp_tool_discovery_mismatch")
                response = await http.get("/v1/capabilities")
                require(response.status_code == 200, "capability_discovery_failed")
                capabilities = response.json()
                require(capabilities["agent"]["enabled"] and capabilities["agent"]["a2a_enabled"],
                        "agent_protocol_not_enabled")
                report["engine"] = {name: capabilities["engine"][name] for name in
                                    ("ffmpeg", "ffprobe", "image_version", "implementation_sha256")}
                report["checks"].extend(["protected_http_mcp_a2a_and_card", "official_a2a_1_0_discovery",
                                          "mcp_22_tools", "verified_project_fixture_uploads"])
                for case in selected_cases(report.get("selected_cases")):
                    harness.stage = case["name"]
                    record = {"name": case["name"], "transport": case["transport"]}
                    report["cases"].append(record)
                    result = await verify_case(harness, http, session, official, sources, case, record)
                    if result and result["status"] == "succeeded":
                        completed = result
        if completed is not None:
            report["restart"] = await restart_check(harness, completed)
            report["checks"].append("restart_queries_existing_plan_and_media_job")
        else:
            report["restart"] = {"verified": False, "reason": "no_successful_media_case"}
        require(parameters.load_fixtures() == contents, "project_fixture_changed")
        report["checks"].append("project_fixtures_unchanged")
        harness.stage = "disclosure_scan"
        await asyncio.to_thread(harness.scan_logs)
        require(harness.monitor.observe(report, "report"), "sensitive_material_exposed")
        harness.stage = "cases_validation"
        require(all(item.get("passed") for item in report["cases"]), "media_cases_failed")
        report["status"] = "passed"
    except BaseException:
        report["failed_stage"] = harness.stage
        raise
    finally:
        try:
            if json.loads(await asyncio.to_thread(shared.docker, "inspect", harness.name, check=False) or "[]"):
                await asyncio.to_thread(harness.scan_logs)
        finally:
            try:
                await asyncio.to_thread(harness.cleanup)
                remaining_containers = await asyncio.to_thread(
                    shared.docker, "ps", "-aq", "--filter", f"label={shared.LABEL}={harness.owner}")
                remaining_volumes = await asyncio.to_thread(
                    shared.docker, "volume", "ls", "-q", "--filter", f"label={shared.LABEL}={harness.owner}")
                require(not remaining_containers and not remaining_volumes, "owned_resources_remain")
                report["cleanup"] = {"containers_remaining": 0, "volumes_remaining": 0}
            finally:
                report["disclosure_checks"] = harness.monitor.checks
                if not harness.monitor.observe(report, "report"):
                    report.clear()
                    report.update({"status": "failed", "error_code": "sensitive_material_exposed",
                                   "disclosure_checks": harness.monitor.checks})


def planning_timeout(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("规划时限必须是 (0, 300] 秒内的数值") from None
    if not 0 < parsed <= 300:
        raise argparse.ArgumentTypeError("规划时限必须是 (0, 300] 秒内的数值")
    return parsed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="本地镜像 ID 或 tag，启动前解析并固定精确 ID")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--report", type=Path, default=ROOT / "docs/verification/agent-validation.json")
    parser.add_argument("--case", choices=[case["name"] for case in CASES],
                        help="只验收指定场景；省略时执行全部六个场景")
    parser.add_argument("--planning-timeout-seconds", type=planning_timeout,
                        help="显式覆盖本次验收规划时限，范围 (0, 300] 秒；未配置时默认 60 秒")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.disable(logging.CRITICAL)
    report: dict[str, Any] = {"created_at": datetime.now(UTC).isoformat(), "cases": [], "checks": []}
    report["selected_cases"] = [args.case] if args.case else [case["name"] for case in CASES]
    model: dict = {}
    try:
        require(args.report.resolve() != args.env_file.resolve() and
                not args.report.name.startswith(".env"), "report_overlaps_private_configuration")
        report["failed_stage"] = "local_model_configuration"
        model = read_model_settings(args.env_file)
        if args.planning_timeout_seconds is not None:
            model["planning_timeout_seconds"] = args.planning_timeout_seconds
        report["planning_timeout_seconds"] = model.get("planning_timeout_seconds", 60)
        report["failed_stage"] = "image_preflight"
        asyncio.run(verify(args.image, model, report))
        report.pop("failed_stage", None)
    except BaseException as error:
        report["status"] = "failed"
        report.setdefault("error_code", error.code if isinstance(error, VerificationFailure)
                          else "VERIFICATION_FAILED")
    if model:
        guard = DisclosureMonitor([model["llm_api_key"].get_secret_value(), model["llm_base_url"]])
        if not guard.observe(report, "report"):
            report = {"status": "failed", "error_code": "sensitive_material_exposed",
                      "disclosure_checks": {"report": False}}
    try:
        if report.get("error_code") != "report_overlaps_private_configuration":
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        report["status"] = "failed"
        report["error_code"] = "report_write_failed"
    print(json.dumps({"status": report["status"],
                      "cases_passed": sum(bool(case.get("passed")) for case in report.get("cases", [])),
                      "cases_total": len(report.get("cases", [])),
                      "disclosure_checks": report.get("disclosure_checks", {}),
                      **({"failed_stage": report["failed_stage"]} if "failed_stage" in report else {})},
                     ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
