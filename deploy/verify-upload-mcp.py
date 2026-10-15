"""在精确生产镜像上验证 HTTP 上传、远程 MCP、下载和上传强停恢复。"""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import logging
import re
import secrets
import subprocess
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("container_verification", ROOT / "deploy/verify-container.py")
assert spec and spec.loader
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
docker = base.docker
LABEL = base.LABEL


class Harness:
    def __init__(self, image: str, authenticated: bool):
        self.image = image
        self.authenticated = authenticated
        self.owner = uuid.uuid4().hex
        self.name = f"ffmpeg-agent-upload-mcp-{self.owner[:12]}"
        self.volumes = {part: f"{self.name}-{part}" for part in ("state", "work", "output")}
        self.token = secrets.token_urlsafe(48)
        self.temp = tempfile.TemporaryDirectory(prefix="ffmpeg-agent-upload-mcp-")
        self.base = ""
        self.stage = "private_configuration"

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.authenticated else {}

    def start(self) -> None:
        config = {
            "state_dir": "/var/lib/ffmpeg-agent/state",
            "work_dir": "/var/lib/ffmpeg-agent/work",
            "service_token": self.token if self.authenticated else None,
            "service_caller": "demo",
            "storages": {"output": {"root": "/media/output", "role": "output", "callers": ["demo"]}},
            "callers": {"demo": {"default_output_storage": "output", "source_rules": []}},
            "worker_concurrency": 1, "runtime_mode": "container", "engine_series": "7.1",
            "public_base_url": "http://localhost:8080", "upload_timeout_seconds": 60,
            "max_input_bytes": 4 * 1024**2, "max_upload_bytes": 32 * 1024**2,
            "max_work_bytes": 128 * 1024**2,
        }
        config_path = Path(self.temp.name) / "config.json"
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o644)
        mounts: list[str] = []
        self.stage = "owned_volume_creation"
        for part, volume in self.volumes.items():
            assert not json.loads(docker("volume", "inspect", volume, check=False) or "[]"), "resource_name_collision"
            docker("volume", "create", "--label", f"{LABEL}={self.owner}", volume)
            target = f"/var/lib/ffmpeg-agent/{part}" if part != "output" else "/media/output"
            mounts.extend(["--mount", f"type=volume,src={volume},dst={target}"])
        mounts.extend([
            "--mount", f"type=bind,src={config_path},dst=/run/secrets/media-config.json,readonly",
            "--mount", f"type=bind,src={ROOT / 'tests/container'},dst=/fault-tests,readonly",
        ])
        self.stage = "owned_container_creation"
        assert not json.loads(docker("inspect", self.name, check=False) or "[]"), "resource_name_collision"
        docker("run", "-d", "--name", self.name, "--label", f"{LABEL}={self.owner}",
               "--init", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
               "--cpus", "2", "--memory", "2g", "--pids-limit", "128",
               "-p", "127.0.0.1::8080", *mounts, self.image)
        self.stage = "container_baseline_assertions"
        info = json.loads(docker("inspect", self.name))[0]
        assert info["Image"] == self.image, "image_identity_changed"
        host = info["HostConfig"]
        assert host["ReadonlyRootfs"] and host["Init"] is True, "container_isolation_mismatch"
        assert host["NanoCpus"] == 2_000_000_000 and host["Memory"] == 2 * 1024**3, "container_limits_mismatch"
        assert int(docker("exec", self.name, "python", "-c", "import os; print(os.geteuid())")) > 0, "root_execution"
        assert not any(mount["Destination"] == "/media/input" for mount in info["Mounts"]), "shared_input_mounted"
        self.connect()

    def connect(self) -> None:
        port = docker("port", self.name, "8080/tcp").rsplit(":", 1)[1]
        self.base = f"http://127.0.0.1:{port}"

    def cleanup(self) -> None:
        failures = 0
        try:
            if json.loads(docker("inspect", self.name, check=False) or "[]"):
                base.owned_remove(self.name, self.owner)
        except Exception:
            failures += 1
        for volume in self.volumes.values():
            try:
                if json.loads(docker("volume", "inspect", volume, check=False) or "[]"):
                    base.owned_remove(volume, self.owner, volume=True)
            except Exception:
                failures += 1
        self.temp.cleanup()
        if failures:
            raise RuntimeError("owned_resource_cleanup_failed")


@asynccontextmanager
async def running(image: str, authenticated: bool, record: dict):
    harness = Harness(image, authenticated)
    try:
        await asyncio.to_thread(harness.start)
        harness.stage = "container_readiness"
        await ready(harness)
        harness.stage = "media_workflow"
        yield harness
    except BaseException:
        record["failed_check"] = harness.stage
        raise
    finally:
        await asyncio.to_thread(harness.cleanup)


async def ready(harness: Harness) -> None:
    async with httpx.AsyncClient(base_url=harness.base, timeout=5) as client:
        async with asyncio.timeout(45):
            while True:
                try:
                    if (await client.get("/health/ready")).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)


@asynccontextmanager
async def remote(harness: Harness):
    async with httpx2.AsyncClient(headers=harness.headers, timeout=15) as client:
        async with streamable_http_client(f"{harness.base}/mcp", http_client=client) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=15) as session:
                result = await session.initialize()
                assert result.server_info.name == "ffmpeg-agent", "unexpected_mcp_server"
                yield session


async def call(session: ClientSession, name: str, arguments: dict) -> dict:
    result = await session.call_tool(name, arguments)
    assert not result.is_error and isinstance(result.structured_content, dict), "mcp_tool_failed"
    return result.structured_content


async def finished(session: ClientSession, job_id: str) -> dict:
    async with asyncio.timeout(90):
        while True:
            job = await call(session, "get_job", {"job_id": job_id})
            if job["inputs_released"]:
                assert job["status"] == "succeeded", "media_job_failed"
                return job
            await asyncio.sleep(0.1)


async def multipart_probe(harness: Harness, client: httpx.AsyncClient,
                          content: bytes, record: dict) -> None:
    definition = {
        "inputs": {"main": {"kind": "upload", "part": "main"}},
        "steps": [{"id": "probe", "operation": "media.probe.v1",
                   "inputs": {"media": {"source": "main"}}}],
        "outputs": {"report": {"step": "probe", "output": "report"}},
    }
    accepted = await client.post("/v1/jobs/upload", files=[
        ("job", (None, json.dumps(definition), "application/json")),
        ("main", ("fixture.mkv", content, "video/x-matroska")),
    ], headers={"Idempotency-Key": "upload-mcp-" + uuid.uuid4().hex})
    assert accepted.status_code == 202, "multipart_submission_failed"
    job_id = accepted.json()["job_id"]
    async with asyncio.timeout(90):
        while True:
            response = await client.get(f"/v1/jobs/{job_id}")
            assert response.status_code == 200, "multipart_job_query_failed"
            job = response.json()
            if job["inputs_released"]:
                assert job["status"] == "succeeded", "multipart_probe_failed"
                break
            await asyncio.sleep(0.1)
    artifact_id = job["outputs"]["report"]["artifact_id"]
    metadata = await client.get(f"/v1/artifacts/{artifact_id}")
    assert metadata.status_code == 200, "multipart_artifact_query_failed"
    result = await download(harness, client, metadata.json())
    assert json.loads(result)["type"] == "video", "multipart_probe_result_invalid"
    record["multipart_job_id"] = job_id
    record["multipart_artifact_id"] = artifact_id
    record["multipart_artifact_sha256"] = hashlib.sha256(result).hexdigest()
    record["checks"].extend(["real_tcp_multipart_upload_submit", "multipart_http_query_probe_download"])


def probe_download(container: str, content: bytes) -> dict:
    result = subprocess.run(
        ["docker", "exec", "-i", container, "ffprobe", "-v", "error", "-threads", "1",
         "-show_streams", "-show_format", "-of", "json", "pipe:0"],
        input=content, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, "downloaded_media_probe_failed"
    metadata = json.loads(result.stdout)
    videos = [stream for stream in metadata["streams"] if stream.get("codec_type") == "video"]
    assert len(videos) == 1 and videos[0]["codec_name"] == "h264", "downloaded_video_codec_mismatch"
    assert videos[0]["width"] == 640 and videos[0]["height"] == 360, "downloaded_video_size_mismatch"
    assert float(metadata["format"]["duration"]) > 0, "downloaded_video_duration_invalid"
    return {"codec": "h264", "width": 640, "height": 360,
            "duration_seconds": float(metadata["format"]["duration"])}


async def download(harness: Harness, client: httpx.AsyncClient, artifact: dict) -> bytes:
    path = urlsplit(artifact["access"]["http"]["content"]).path
    assert path == f"/v1/artifacts/{artifact['artifact_id']}/content", "download_endpoint_mismatch"
    if harness.authenticated:
        denied = await client.get(path, headers={"Authorization": "Bearer invalid"})
        assert denied.status_code == 401, "download_authentication_bypass"
    head = await client.head(path)
    assert head.status_code == 200 and not head.content, "download_head_failed"
    content = await client.get(path)
    assert content.status_code == 200, "download_failed"
    assert int(head.headers["content-length"]) == len(content.content), "download_length_mismatch"
    assert hashlib.sha256(content.content).hexdigest() == artifact["sha256"], "download_sha_mismatch"
    last = min(31, len(content.content) - 1)
    ranged = await client.get(path, headers={"Range": f"bytes=0-{last}"})
    assert ranged.status_code == 206 and ranged.content == content.content[:last + 1], "download_range_failed"
    assert ranged.headers["content-range"] == f"bytes 0-{last}/{len(content.content)}", "download_range_metadata_mismatch"
    return content.content


async def interrupted_upload(harness: Harness, record: dict) -> None:
    key = "upload-mcp-" + uuid.uuid4().hex
    first_sent = asyncio.Event()

    async def slow():
        yield b"\0" * 65536
        first_sent.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(base_url=harness.base, headers=harness.headers, timeout=20) as client:
        receiver = asyncio.create_task(client.post("/v1/files", content=slow(), headers={
            "Content-Type": "application/octet-stream", "Idempotency-Key": key,
        }))
        try:
            await asyncio.wait_for(first_sent.wait(), 10)
            raw = await asyncio.to_thread(
                docker, "exec", harness.name, "python", "/fault-tests/upload_control.py",
                "wait-uploading", "--key", key, "--timeout", "15", timeout=20,
            )
            before = json.loads(raw)
            assert before["state"] == "uploading" and before["partial_bytes"] > 0, "upload_intent_not_observed"
            await asyncio.to_thread(docker, "kill", "--signal", "KILL", harness.name)
            info = json.loads(await asyncio.to_thread(docker, "inspect", harness.name))[0]
            assert info["State"]["Running"] is False and info["State"]["ExitCode"] == 137, "whole_container_kill_unconfirmed"
            record["kill"] = {"signal": "SIGKILL", "exit_code": 137,
                              "file_id": before["file_id"], "registered_partial_observed": True}
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
    await asyncio.to_thread(docker, "start", harness.name)
    await asyncio.to_thread(harness.connect)
    await ready(harness)
    after = json.loads(await asyncio.to_thread(
        docker, "exec", harness.name, "python", "/fault-tests/upload_control.py",
        "snapshot", "--key", key, timeout=15,
    ))
    assert after["file_id"] == before["file_id"] and after["state"] == "deleted", "interrupted_upload_not_reclaimed"
    assert not after["partial_exists"] and not after["complete_exists"], "interrupted_upload_file_remains"
    record["checks"].append("hard_kill_reclaims_registered_incomplete_upload")


async def verify(image: str, report: dict) -> None:
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", image), "exact_image_id_required"
    info = json.loads(await asyncio.to_thread(docker, "image", "inspect", image))[0]
    assert info["Id"] == image, "image_identity_mismatch"
    fixture = ROOT / "tests/fixtures/video_h264_640x360.mkv"
    content = fixture.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    manifest = json.loads((ROOT / "tests/fixtures/checksums.json").read_text())
    expected = next(entry for entry in manifest if entry["name"] == fixture.name)
    assert len(content) == expected["size_bytes"] and digest == expected["sha256"], "fixture_integrity_failed"
    report["fixture"] = {"size_bytes": len(content), "sha256": digest}
    for authenticated in (True, False):
        record: dict[str, Any] = {"token_enabled": authenticated, "checks": []}
        report["modes"].append(record)
        async with running(image, authenticated, record) as harness:
            record["resource_owner_id"] = harness.owner
            record["checks"].append("nonroot_readonly_2cpu_2gib_init_without_shared_input")
            async with httpx.AsyncClient(base_url=harness.base, headers=harness.headers, timeout=30) as client:
                if authenticated:
                    for method, path in (("GET", "/v1/capabilities"), ("POST", "/v1/files"), ("POST", "/mcp")):
                        denied = await client.request(method, path, content=b"not-read", headers={"Authorization": "Bearer invalid"})
                        assert denied.status_code == 401, "entry_authentication_bypass"
                uploaded = await client.post("/v1/files", content=content, headers={
                    "Content-Type": "application/octet-stream", "Idempotency-Key": "upload-mcp-" + uuid.uuid4().hex,
                    "X-File-Size": str(len(content)), "X-File-Sha256": digest,
                })
                assert uploaded.status_code == 201, "raw_upload_failed"
                file = uploaded.json()
                assert file["sha256"] == digest and file["state"] == "ready", "uploaded_identity_mismatch"
                record["file_id"] = file["file_id"]
                record["checks"].append("authenticated_raw_http_upload" if authenticated else "token_off_raw_http_upload")
                async with remote(harness) as session:
                    tools = await session.list_tools()
                    assert len(tools.tools) == 19, "mcp_tool_discovery_mismatch"
                    operation = "transcode_video" if authenticated else "probe_media"
                    accepted = await call(session, operation, {
                        "source": {"kind": "uploaded_file", "file_id": file["file_id"]},
                        "idempotency_key": "upload-mcp-" + uuid.uuid4().hex,
                        "params": {"preset": "web_720p_v1"} if authenticated else {},
                    })
                    job = await finished(session, accepted["job_id"])
                    record["job_id"] = job["job_id"]
                    artifact_id = job["outputs"]["result"]["artifact_id"]
                    artifact = await call(session, "get_artifact", {"artifact_id": artifact_id})
                    downloaded = await download(harness, client, artifact)
                    record["artifact_id"] = artifact_id
                    record["artifact_sha256"] = hashlib.sha256(downloaded).hexdigest()
                    if authenticated:
                        record["media"] = await asyncio.to_thread(probe_download, harness.name, downloaded)
                    else:
                        assert json.loads(downloaded)["type"] == "video", "probe_result_invalid"
                    record["checks"].extend(["remote_mcp_handshake_and_tools", operation,
                                              "mcp_query_artifact", "http_head_range_sha_download"])
                harness.stage = "real_tcp_multipart_workflow"
                await multipart_probe(harness, client, content, record)
            if authenticated:
                harness.stage = "interrupted_upload_recovery"
                await interrupted_upload(harness, record)
                async with remote(harness) as session:
                    file_after = await call(session, "get_uploaded_file", {"file_id": file["file_id"]})
                    assert file_after["state"] == "ready" and file_after["sha256"] == digest, "completed_upload_not_preserved"
                    accepted = await call(session, "probe_media", {
                        "source": {"kind": "uploaded_file", "file_id": file["file_id"]},
                        "idempotency_key": "upload-mcp-" + uuid.uuid4().hex, "params": {},
                    })
                    recovered = await finished(session, accepted["job_id"])
                    record["recovery_job_id"] = recovered["job_id"]
                    record["checks"].append("restart_reuses_completed_upload_via_remote_mcp")
            assert hashlib.sha256(fixture.read_bytes()).hexdigest() == digest, "source_fixture_changed"
            record["checks"].append("project_fixture_remains_unchanged")
        remaining_containers = await asyncio.to_thread(
            docker, "ps", "-aq", "--filter", f"label={LABEL}={harness.owner}"
        )
        remaining_volumes = await asyncio.to_thread(
            docker, "volume", "ls", "-q", "--filter", f"label={LABEL}={harness.owner}"
        )
        assert not remaining_containers and not remaining_volumes, "owned_resources_remain"
        record["cleanup"] = {"containers_remaining": 0, "volumes_remaining": 0}
        record["checks"].append("only_owned_resources_removed")
    report["status"] = "passed"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="精确 sha256 镜像 ID")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    # 第三方网络库及 SDK 不进入本验收的输出；失败仅报告语义状态。
    logging.disable(logging.CRITICAL)
    report: dict[str, Any] = {"image_id": args.image,
                              "created_at": datetime.now(UTC).isoformat(), "modes": []}
    try:
        asyncio.run(verify(args.image, report))
    except BaseException:
        report["status"] = "failed"
        report["error"] = "verification_failed"
    result = json.dumps(report, ensure_ascii=False, indent=2)
    args.report.write_text(result + "\n")
    print(result)
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
