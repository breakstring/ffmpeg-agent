import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

FIXTURE_SHA256 = "c29e17b6fa7293a62dddc2e3a7201eaccc43964b8f0cb6699a5dec4785e26527"
TERMINAL = {"succeeded", "partial", "failed", "canceled"}


class VerificationError(RuntimeError):
    pass


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def command(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise VerificationError("外部验证命令执行失败")
    return result.stdout.strip()


def host_identity() -> dict:
    if Path("/.dockerenv").exists():
        raise VerificationError("主机身份必须在宿主机采集")
    if platform.system() == "Linux":
        machine_id = Path("/etc/machine-id").read_text().strip()
    elif platform.system() == "Darwin":
        output = command("/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice")
        match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', output)
        if match is None:
            raise VerificationError("无法读取主机身份")
        machine_id = match.group(1)
    else:
        raise VerificationError("宿主机证据采集目前支持 Linux 和 macOS")
    if not machine_id:
        raise VerificationError("主机身份为空")
    return {
        "platform": platform.system(),
        "architecture": platform.machine(),
        "machine_sha256": digest(machine_id.encode()),
        "hostname_sha256": digest(platform.node().encode()),
    }


def collect_host(image: str) -> dict:
    result = host_identity()
    endpoint = os.environ.get("DOCKER_HOST") or json.loads(
        command("docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}")
    )
    if not endpoint.startswith("unix://"):
        raise VerificationError("请在媒体宿主机使用本机 Docker socket 采集证据")
    daemon = command("docker", "info", "--format", "{{.ID}}")
    inspected = json.loads(command("docker", "image", "inspect", image))[0]
    return {
        **result,
        "recorded_at": datetime.now(UTC).isoformat(),
        "docker_daemon_sha256": digest(daemon.encode()),
        "image_id": inspected["Id"],
        "image_architecture": inspected["Architecture"],
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # API 的认证头不能被 3xx 转发到另一个入口。
        return None


class Client:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def open(self, method: str, path: str, body=None, headers=None):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                **(headers or {}),
            },
            method=method,
        )
        try:
            return self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            error.close()
            raise VerificationError(f"API 返回非预期 HTTP {error.code}") from None

    def json(self, method: str, path: str, body=None, headers=None) -> tuple[int, Any]:
        with self.open(method, path, body, headers) as response:
            data = response.read(2 * 1024**2)
            return response.status, json.loads(data) if data else None


def check_video(path: Path) -> dict:
    probe = json.loads(
        command("ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path))
    )
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    audio = next(s for s in probe["streams"] if s["codec_type"] == "audio")
    assert video["codec_name"] == "h264" and video["pix_fmt"] == "yuv420p"
    assert 0 < video["width"] <= 1280 and 0 < video["height"] <= 720
    assert video["width"] % 2 == 0 and video["height"] % 2 == 0
    assert video["avg_frame_rate"] == "30/1"
    assert audio["codec_name"] == "aac"
    assert audio["sample_rate"] == "48000" and audio["channels"] == 2
    return {
        "video_codec": video["codec_name"],
        "width": video["width"],
        "height": video["height"],
        "pixel_format": video["pix_fmt"],
        "frame_rate": video["avg_frame_rate"],
        "audio_codec": audio["codec_name"],
        "audio_sample_rate": audio["sample_rate"],
        "audio_channels": audio["channels"],
    }


def verify(config_path: Path, output: Path, local_smoke: bool) -> dict:
    if config_path.stat().st_mode & 0o077:
        raise VerificationError("包含 token 和来源地址的配置必须为 0600")
    config = json.loads(config_path.read_text())
    base = urllib.parse.urlsplit(config["service_url"])
    if base.scheme not in {"http", "https"} or not base.hostname:
        raise VerificationError("服务地址必须为 HTTP(S)")
    if base.username or base.password or base.query or base.fragment:
        raise VerificationError("服务入口不能包含认证信息、查询或 fragment")
    if not local_smoke and base.scheme != "https":
        raise VerificationError("真实跨宿主机验收通过受控 HTTPS 入口传递认证头")
    fixture = Path(config["fixture_path"])
    assert digest(fixture.read_bytes()) == FIXTURE_SHA256, "必须使用仓库 Feline fixture"
    media_host = json.loads(Path(config["media_host_evidence"]).read_text())
    business_host = host_identity()
    if not local_smoke:
        if config.get("distinct_physical_hosts_confirmed") is not True:
            raise VerificationError("部署方尚未确认两台独立实际宿主机")
        if media_host["machine_sha256"] == business_host["machine_sha256"]:
            raise VerificationError("两侧主机身份相同，不能计为跨宿主机验收")
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", media_host["image_id"])
    if output.exists():
        raise VerificationError("下载目标已存在，拒绝覆盖")

    client = Client(config["service_url"], config["caller_token"])
    assert client.json("GET", "/health/ready")[0] == 200
    _, capabilities = client.json("GET", "/v1/capabilities")
    source = {
        "kind": "http_file",
        "url": config["source"]["url"],
        "headers": config["source"].get("headers", {}),
        "expected_size_bytes": fixture.stat().st_size,
        "expected_sha256": FIXTURE_SHA256,
    }
    if "expires_at" in config["source"]:
        source["expires_at"] = config["source"]["expires_at"]
    body = {
        "inputs": {"source": source},
        "steps": [{
            "id": "encode", "operation": "video.transcode.v1",
            "inputs": {"video": {"source": "source"}},
            "params": {"preset": "web_720p_v1"},
        }],
        "outputs": {"video": {"step": "encode", "output": "video"}},
        "output_policy": {"retention_seconds": 3600},
    }
    idempotency = {"Idempotency-Key": f"remote-verification-{uuid4().hex}"}
    started = time.monotonic()
    status, job = client.json("POST", "/v1/jobs", body, idempotency)
    assert status == 202
    job_id = job["job_id"]
    lease_id = artifact_id = None
    finished = False
    try:
        replay_status, replay = client.json("POST", "/v1/jobs", body, idempotency)
        assert replay_status == 200 and replay["job_id"] == job_id
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            _, job = client.json("GET", f"/v1/jobs/{job_id}")
            if job["status"] in TERMINAL:
                finished = True
                break
            time.sleep(0.25)
        assert finished and job["status"] == "succeeded" and job["inputs_released"]
        artifact_id = job["outputs"]["video"]["artifact_id"]
        artifact_path = f"/v1/artifacts/{artifact_id}"
        _, lease = client.json("POST", artifact_path + "/leases", {"duration_seconds": 900})
        lease_id = lease["lease_id"]
        headers = {"X-Artifact-Lease-Id": lease_id}
        _, artifact = client.json("GET", artifact_path, headers=headers)
        expected = artifact["size_bytes"]
        assert 64 < expected < 64 * 1024**2
        content_path = artifact_path + "/content"
        assert artifact["access"]["http"]["content"] == client.base + content_path
        with client.open("HEAD", content_path, headers=headers) as response:
            assert response.status == 200 and response.read() == b""
            assert int(response.headers["Content-Length"]) == expected
            etag = response.headers["ETag"]
            assert etag == '"' + artifact["sha256"] + '"'
        range_headers = headers | {"Range": "bytes=0-63", "If-Range": etag}
        with client.open("GET", content_path, headers=range_headers) as response:
            assert response.status == 206
            assert response.headers["Content-Range"] == f"bytes 0-63/{expected}"
            prefix = response.read(65)
            assert len(prefix) == 64
        hasher = hashlib.sha256()
        downloaded = 0
        with client.open("GET", content_path, headers=headers) as response, output.open("xb") as f:
            assert response.status == 200
            os.chmod(output, 0o600)
            assert int(response.headers["Content-Length"]) == expected
            download_deadline = time.monotonic() + 300
            while chunk := response.read(256 * 1024):
                downloaded += len(chunk)
                assert downloaded <= expected and time.monotonic() < download_deadline
                hasher.update(chunk)
                f.write(chunk)
        assert downloaded == expected and hasher.hexdigest() == artifact["sha256"]
        with output.open("rb") as f:
            assert f.read(64) == prefix
        decoded = check_video(output)
        assert digest(fixture.read_bytes()) == FIXTURE_SHA256
        assert client.json("DELETE", f"/v1/leases/{lease_id}")[0] == 204
        lease_id = None
        assert client.json("POST", artifact_path + "/release")[0] == 200
        return {
            "status": "passed",
            "scope": "single_host_http_smoke" if local_smoke else "two_physical_hosts_http",
            "host_separation_basis": (
                "not_applicable_single_host" if local_smoke else "operator_confirmed_host_evidence"
            ),
            "recorded_at": datetime.now(UTC).isoformat(),
            "business_host": business_host,
            "media_host": media_host,
            "service_engine": capabilities["engine"],
            "job_id": job_id, "artifact_id": artifact_id,
            "input_sha256": FIXTURE_SHA256,
            "output_sha256": hasher.hexdigest(), "output_size_bytes": downloaded,
            "elapsed_seconds": time.monotonic() - started,
            "output_media": decoded,
            "checks": [
                "http_input_integrity", "idempotency_replay", "video_transcode", "input_released",
                "explicit_lease", "head", "single_range_if_range", "full_download_integrity",
                "ffprobe_output", "lease_released", "owned_artifact_released", "source_preserved",
            ],
        }
    finally:
        if lease_id:
            client.json("DELETE", f"/v1/leases/{lease_id}")
        if not finished:
            client.json("POST", f"/v1/jobs/{job_id}/cancel")


def main() -> int:
    parser = argparse.ArgumentParser(description="真实第二宿主机 HTTP 输入和下载验收")
    subparsers = parser.add_subparsers(dest="command", required=True)
    host = subparsers.add_parser("host", help="在媒体宿主机采集脱敏证据")
    host.add_argument("--image", required=True)
    host.add_argument("--report", type=Path, required=True)
    run = subparsers.add_parser("run", help="在业务宿主机执行")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--report", type=Path, required=True)
    run.add_argument("--local-smoke", action="store_true", help="仅脚本自验，不计为跨机验收")
    args = parser.parse_args()
    try:
        report = (
            collect_host(args.image) if args.command == "host"
            else verify(args.config, args.output, args.local_smoke)
        )
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    except Exception as error:
        # 第三方异常可能含 URL、认证响应或本地路径，仅打印类型。
        reason = str(error) if isinstance(error, VerificationError) else type(error).__name__
        args.report.write_text(json.dumps({
            "status": "failed", "reason": reason,
            "recorded_at": datetime.now(UTC).isoformat(),
        }, ensure_ascii=False, indent=2) + "\n")
        print(f"验收未通过：{reason}；未写入成功报告。", file=sys.stderr)
        return 1
    print("脱敏主机证据已写入。" if args.command == "host" else f"验收通过：{report['scope']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
