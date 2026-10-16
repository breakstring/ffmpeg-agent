"""使用独立容器和卷验证持久接受、单实例及容器硬停止后的检查点。"""

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

_support_spec = importlib.util.spec_from_file_location(
    "deployment_env_support", Path(__file__).with_name("env_support.py"),
)
assert _support_spec and _support_spec.loader
env_support = importlib.util.module_from_spec(_support_spec)
_support_spec.loader.exec_module(env_support)


LABEL = "io.ffmpeg-agent.verification"
TERMINAL = {"succeeded", "partial", "failed", "canceled"}


def docker(*args: str, timeout: float = 60, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        # 不复制容器诊断输出，避免配置错误时意外回显凭据。
        raise RuntimeError(f"docker {args[0]} failed with exit {result.returncode}")
    return result.stdout.strip()


class Client:
    def __init__(self, base: str, token: str):
        self.base = base
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        key: str | None = None,
        authenticated: bool = True,
    ) -> tuple[int, Any]:
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.token}"
        if key:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status = response.getcode()
            assert isinstance(status, int)
            return status, json.load(response)

    def ready(self, seconds: float = 45) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if self.request("GET", "/health/ready", authenticated=False)[0] == 200:
                    return
            except OSError, ValueError:
                pass
            time.sleep(0.2)
        raise AssertionError("容器未在期限内就绪")

    def finished(self, job_id: str) -> dict:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            code, job = self.request("GET", f"/v1/jobs/{job_id}")
            assert code == 200, f"查询任务返回 {code}"
            if job["status"] in TERMINAL:
                assert job["inputs_released"] is True
                return job
            time.sleep(0.1)
        raise AssertionError("任务未在期限内结束")


def owned_remove(name: str, owner: str, volume: bool = False) -> None:
    kind = ["volume", "inspect"] if volume else ["inspect"]
    raw = docker(*kind, name, check=False)
    if not raw:
        return
    entries = json.loads(raw)
    if not entries:
        return
    info = entries[0]
    labels = info.get("Labels", {}) if volume else info["Config"].get("Labels", {})
    if (labels or {}).get(LABEL) != owner:
        raise RuntimeError("拒绝清理不属于本次验收的 Docker 资源")
    if volume:
        docker("volume", "rm", name)
    else:
        docker("rm", "--force", name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="ffmpeg-agent:v1-local")
    parser.add_argument("--workers", type=int, choices=(1, 2, 4), default=1)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    fixture = root / "tests/fixtures/video_h264_640x360.mkv"
    assert fixture.is_file(), "缺少基础 fixture"
    owner = uuid.uuid4().hex
    name = f"ffmpeg-agent-verify-{owner[:12]}"
    duplicate = name + "-duplicate"
    volumes = {part: f"{name}-{part}" for part in ("state", "work", "output")}
    token = secrets.token_urlsafe(48)
    report: dict[str, Any] = {"image": args.image, "workers": args.workers, "checks": []}
    with tempfile.TemporaryDirectory(prefix="ffmpeg-agent-container-") as temp:
        config_path = Path(temp) / "config.json"
        config = json.loads((root / "deploy/media-config.example.json").read_text())
        config["service_token"] = token
        config["worker_concurrency"] = args.workers
        environment_path = env_support.write_runtime_configuration(config, config_path)
        mounts = env_support.environment_mount(environment_path) + [
            "--mount",
            f"type=volume,src={volumes['state']},dst=/var/lib/ffmpeg-agent/state",
            "--mount",
            f"type=volume,src={volumes['work']},dst=/var/lib/ffmpeg-agent/work",
            "--mount",
            f"type=volume,src={volumes['output']},dst=/media/output",
            "--mount",
            f"type=bind,src={fixture.parent},dst=/media/input,readonly",
            "--mount",
            f"type=bind,src={config_path},dst=/run/secrets/media-config.json,readonly",
        ]
        try:
            for volume in volumes.values():
                docker("volume", "create", "--label", f"{LABEL}={owner}", volume)
            docker(
                "run",
                "-d",
                "--name",
                name,
                "--label",
                f"{LABEL}={owner}",
                "--init",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=64m",
                "--cpus",
                "2",
                "--memory",
                "2g",
                "--pids-limit",
                "128",
                "-p",
                "127.0.0.1::8080",
                *mounts,
                args.image,
            )
            port = docker("port", name, "8080/tcp").rsplit(":", 1)[1]
            client = Client(f"http://127.0.0.1:{port}", token)
            client.ready()
            report["image_id"] = json.loads(docker("inspect", name))[0]["Image"]
            assert client.request("GET", "/v1/capabilities", authenticated=False)[0] == 401
            report["checks"].append("ready_and_authentication")
            body = {
                "inputs": {
                    "source": {
                        "kind": "shared_file",
                        "storage_id": "input",
                        "key": fixture.name,
                        "expected_size_bytes": fixture.stat().st_size,
                        "expected_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
                    }
                },
                "steps": [
                    {
                        "id": "probe",
                        "operation": "media.probe.v1",
                        "inputs": {"media": {"source": "source"}},
                    }
                ],
                "outputs": {"report": {"step": "probe", "output": "report"}},
            }
            key = f"container-{owner}"
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                responses = list(
                    pool.map(lambda _: client.request("POST", "/v1/jobs", body, key), range(8))
                )
            codes = [response[0] for response in responses]
            assert codes.count(202) == 1 and codes.count(200) == 7, codes
            ids = {response[1]["job_id"] for response in responses}
            assert len(ids) == 1
            job_id = ids.pop()
            changed = dict(body, output_policy={"retention_seconds": 120})
            assert client.request("POST", "/v1/jobs", changed, key)[0] == 409
            job = client.finished(job_id)
            assert job["status"] == "succeeded", job["status"]
            artifact_id = job["outputs"]["report"]["artifact_id"]
            code, artifact = client.request("GET", f"/v1/artifacts/{artifact_id}")
            assert code == 200 and artifact["state"] == "ready"
            shared = artifact["access"]["shared"]
            assert shared["storage_id"] == "output"
            verification_code = (
                "import hashlib,json,sys; from pathlib import Path; "
                "root=Path('/media/output'); p=root/sys.argv[1]; "
                "assert p.resolve().is_relative_to(root); data=p.read_bytes(); "
                "assert hashlib.sha256(data).hexdigest()==sys.argv[2]; "
                "assert isinstance(json.loads(data),dict)"
            )
            docker(
                "exec",
                name,
                "python",
                "-c",
                verification_code,
                shared["key"],
                artifact["sha256"],
            )
            report["checks"].extend(["concurrent_idempotency", "conflict_409", "real_probe"])
            docker(
                "run",
                "-d",
                "--name",
                duplicate,
                "--label",
                f"{LABEL}={owner}",
                "--init",
                *mounts,
                args.image,
            )
            assert int(docker("wait", duplicate, timeout=20)) != 0
            report["checks"].append("second_instance_rejected")
            docker("kill", "--signal", "KILL", name)
            assert json.loads(docker("inspect", name))[0]["State"]["Running"] is False
            docker("start", name)
            port = docker("port", name, "8080/tcp").rsplit(":", 1)[1]
            client = Client(f"http://127.0.0.1:{port}", token)
            client.ready()
            recovered = client.finished(job_id)
            assert recovered["outputs"]["report"]["artifact_id"] == artifact_id
            code, after = client.request("GET", f"/v1/artifacts/{artifact_id}")
            assert code == 200 and after["sha256"] == artifact["sha256"]
            docker(
                "exec",
                name,
                "python",
                "-c",
                verification_code,
                after["access"]["shared"]["key"],
                after["sha256"],
            )
            assert client.request("POST", "/v1/jobs", body, key)[1]["job_id"] == job_id
            assert (
                hashlib.sha256(fixture.read_bytes()).hexdigest()
                == body["inputs"]["source"]["expected_sha256"]
            )
            report["checks"].append("hard_restart_preserves_published_checkpoint")
            report["job_id"] = job_id
            report["artifact_id"] = artifact_id
            report["status"] = "passed"
        finally:
            for container in (duplicate, name):
                owned_remove(container, owner)
            for volume in volumes.values():
                owned_remove(volume, owner, volume=True)
    result = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(result + "\n")
    print(result)


if __name__ == "__main__":
    main()
