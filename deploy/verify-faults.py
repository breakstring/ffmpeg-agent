"""仅操作本次标签的临时容器/卷，验证真实媒体执行者的硬停止与发布恢复。"""

import argparse
import hashlib
import importlib.util
import json
import secrets
import subprocess
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("container_verification", ROOT / "deploy/verify-container.py")
assert spec and spec.loader
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
docker = base.docker
LABEL = base.LABEL


class Harness:
    def __init__(self, image: str, mode: str):
        self.image = image
        self.mode = mode
        self.owner = uuid.uuid4().hex
        self.name = f"ffmpeg-agent-fault-{self.owner[:12]}"
        self.containers = [self.name]
        self.volumes = {part: f"{self.name}-{part}" for part in ("state", "work", "output")}
        self.token = secrets.token_urlsafe(48)
        self.temp = tempfile.TemporaryDirectory(prefix="ffmpeg-agent-fault-")
        self.client: Any = None
        self.mounts: list[str] = []
        self.kill_evidence: list[dict] = []

    def __enter__(self):
        config_path = Path(self.temp.name) / "config.json"
        config = json.loads((ROOT / "deploy/media-config.example.json").read_text())
        config["callers"]["demo"]["tokens"] = [self.token]
        config["max_attempts"] = 1 if self.mode in {"staged", "renamed", "committed"} else 2
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o644)
        try:
            for part, volume in self.volumes.items():
                docker("volume", "create", "--label", f"{LABEL}={self.owner}", volume)
                target = f"/var/lib/ffmpeg-agent/{part}" if part != "output" else "/media/output"
                self.mounts.extend(["--mount", f"type=volume,src={volume},dst={target}"])
            for source, target in (
                (ROOT / "tests/fixtures", "/media/input"),
                (ROOT / "tests/container", "/fault-tests"),
                (config_path, "/run/secrets/media-config.json"),
            ):
                self.mounts.extend(["--mount", f"type=bind,src={source},dst={target},readonly"])
            command = ["python", "/fault-tests/entry.py", self.mode]
            if self.mode == "orphan_guard":
                command = ["python", "/fault-tests/supervisor.py"]
            self.run_container(self.name, command)
            self.connect()
            return self
        except BaseException:
            self.cleanup()
            raise

    def run_container(self, name: str, command: list[str]):
        docker(
            "run", "-d", "--name", name, "--label", f"{LABEL}={self.owner}",
            "--init", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
            "--cpus", "2", "--memory", "2g", "--pids-limit", "128",
            "-p", "127.0.0.1::8080", *self.mounts, self.image, *command,
        )

    def connect(self):
        port = docker("port", self.name, "8080/tcp").rsplit(":", 1)[1]
        self.client = base.Client(f"http://127.0.0.1:{port}", self.token)
        self.client.ready()

    def control(self, *args: str):
        return json.loads(docker("exec", self.name, "python", "/fault-tests/control.py", *args))

    def submit(self, video: bool = False):
        fixture = ROOT / "tests/fixtures" / (
            "Feline_Apocalypse.mp4" if video else "ffmpeg_h264_640x360.mkv"
        )
        port, output = ("video", "video") if video else ("media", "report")
        body = {
            "inputs": {"source": {
                "kind": "shared_file", "storage_id": "input", "key": fixture.name,
                "expected_size_bytes": fixture.stat().st_size,
                "expected_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
            }},
            "steps": [{"id": "step", "operation": "video.transcode.v1" if video else "media.probe.v1",
                       "inputs": {port: {"source": "source"}}}],
            "outputs": {"result": {"step": "step", "output": output}},
        }
        code, response = self.client.request("POST", "/v1/jobs", body, uuid.uuid4().hex)
        assert code == 202, f"accept_status={code}"
        return response["job_id"]

    def job(self, job_id):
        code, job = self.client.request("GET", f"/v1/jobs/{job_id}")
        assert code == 200
        return job

    def finish(self, job_id):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            job = self.job(job_id)
            if job["status"] in base.TERMINAL:
                assert job["inputs_released"] is True
                return job
            time.sleep(0.1)
        raise AssertionError("job_completion_timeout")

    def verify_artifact(self, job):
        artifact_id = job["outputs"]["result"]["artifact_id"]
        code, artifact = self.client.request("GET", f"/v1/artifacts/{artifact_id}")
        assert code == 200 and artifact["state"] == "ready"
        digest = self.control("digest", artifact["access"]["shared"]["key"])["sha256"]
        assert digest == artifact["sha256"]
        return {"artifact_id": artifact_id, "sha256": digest, "state": "ready"}

    def kill(self):
        before = json.loads(docker("inspect", self.name))[0]
        assert before["State"]["Running"]
        docker("kill", "--signal", "KILL", self.name)
        exit_code = int(docker("wait", self.name, timeout=30))
        state = json.loads(docker("inspect", self.name))[0]["State"]
        assert state["Running"] is False and exit_code == state["ExitCode"] == 137
        evidence = {"signal": "KILL", "exit_code": exit_code, "running": False,
                    "container_id": before["Id"], "started_at": state["StartedAt"],
                    "finished_at": state["FinishedAt"]}
        self.kill_evidence.append(evidence)

    def restart(self):
        docker("start", self.name)
        self.connect()

    def cleanup(self):
        failures = []
        for name in reversed(self.containers):
            try:
                base.owned_remove(name, self.owner)
            except Exception as error:
                failures.append(type(error).__name__)
        for volume in self.volumes.values():
            try:
                base.owned_remove(volume, self.owner, volume=True)
            except Exception as error:
                failures.append(type(error).__name__)
        self.temp.cleanup()
        if failures:
            raise RuntimeError("owned_resource_cleanup_failed")

    def __exit__(self, *_):
        self.cleanup()


def compact(job):
    return {"job_id": job["job_id"], "status": job["status"],
            "inputs_released": job["inputs_released"], "attempts": job["execution"]["attempts"],
            "error_code": (job.get("error") or {}).get("code")}


def hard_restart(image):
    with Harness(image, "normal") as h:
        job_id = h.submit(video=True)
        child = h.control("freeze_media")
        before = h.job(job_id)
        snapshot = h.control("snapshot", job_id)
        assert before["status"] == "running" and before["inputs_released"] is False
        assert child["media"] and child["state"] in {"T", "t"}
        h.kill()
        h.restart()
        after = h.finish(job_id)
        recovered = h.control("snapshot", job_id)
        assert after["status"] == "succeeded" and after["execution"]["attempts"] == 2
        assert snapshot["runtime"]["generation"] != recovered["runtime"]["generation"]
        artifact = h.verify_artifact(after)

        exhausted_id = h.submit(video=True)
        exhausted_children = []
        for expected_attempt in (1, 2):
            exhausted_children.append(h.control("freeze_media"))
            running = h.job(exhausted_id)
            assert running["execution"]["attempts"] == expected_attempt
            assert running["status"] == "running" and not running["inputs_released"]
            h.kill()
            h.restart()
        exhausted = h.finish(exhausted_id)
        assert exhausted["status"] == "failed" and exhausted["execution"]["attempts"] == 2
        assert exhausted["error"]["code"] == "EXECUTION_INTERRUPTED"
        assert exhausted["outputs"]["result"]["artifact_id"] is None
        return {"before": compact(before), "real_ffmpeg": child, "after": compact(after),
                "artifact": artifact, "recovered_snapshot": recovered,
                "exhausted": compact(exhausted), "exhausted_children": exhausted_children,
                "exhausted_snapshot": h.control("snapshot", exhausted_id), "kills": h.kill_evidence}


def publishing(image, mode):
    with Harness(image, mode) as h:
        job_id = h.submit()
        marker = h.control("marker", mode)
        old_id = marker["artifact_id"]
        before = h.job(job_id)
        snapshot = h.control("snapshot", job_id)
        code, _ = h.client.request("GET", f"/v1/artifacts/{old_id}")
        expected = 200 if mode == "committed" else 404
        assert code == expected
        assert before["outputs"]["result"]["artifact_id"] == (old_id if mode == "committed" else None)
        assert before["status"] == "running" and not before["inputs_released"]
        h.kill()
        h.restart()
        after = h.finish(job_id)
        assert after["status"] == "succeeded"
        artifact = h.verify_artifact(after)
        recovered = h.control("snapshot", job_id)
        old = next(a for a in recovered["artifacts"] if a["id"] == old_id)
        if mode == "intent":
            assert artifact["artifact_id"] != old_id
            # 未发布目录只在任务终态后回收；给正常后台 GC 一个完整周期。
            deadline = time.monotonic() + 20
            while old["state"] != "deleted" and time.monotonic() < deadline:
                time.sleep(0.2)
                recovered = h.control("snapshot", job_id)
                old = next(a for a in recovered["artifacts"] if a["id"] == old_id)
            assert old["state"] == "deleted" and not old["directory_exists"] and not old["staging_exists"]
            assert h.client.request("GET", f"/v1/artifacts/{old_id}")[0] == 410
        else:
            assert artifact["artifact_id"] == old_id
            assert old["state"] == "ready" and old["directory_exists"] and not old["staging_exists"]
            # 最后允许的一次执行已完成发布检查点，恢复只收尾而非再消耗attempt。
            assert after["execution"]["attempts"] == before["execution"]["attempts"] == 1
        return {"checkpoint": mode, "marker": marker, "before": compact(before),
                "before_artifact_http": code, "before_snapshot": snapshot,
                "after": compact(after), "after_snapshot": recovered,
                "artifact": artifact, "kills": h.kill_evidence}


def cancel_restart(image):
    with Harness(image, "cancel_gate") as h:
        job_id = h.submit(video=True)
        child = h.control("freeze_media")
        code, _ = h.client.request("POST", f"/v1/jobs/{job_id}/cancel")
        assert code in {200, 202}
        marker = h.control("marker", "cancel_gate")
        before = h.job(job_id)
        snapshot = h.control("snapshot", job_id)
        live = h.control("process", str(child["pid"]))
        assert live and live["media"] and live["state"] in {"T", "t"}
        assert before["status"] == "canceling" and not before["inputs_released"]
        assert snapshot["job"]["cancel_requested"] == 1
        h.kill()
        h.restart()
        after = h.finish(job_id)
        recovered = h.control("snapshot", job_id)
        assert after["status"] == "canceled" and after["execution"]["attempts"] == 1
        assert after["outputs"]["result"]["artifact_id"] is None and not recovered["artifacts"]
        return {"before": compact(before), "before_snapshot": snapshot, "real_ffmpeg": live,
                "marker": marker, "after": compact(after), "after_snapshot": recovered,
                "kills": h.kill_evidence}


def hot_guard(image):
    with Harness(image, "hot_guard") as h:
        code, release = h.client.request("POST", "/_fault/release_lock")
        assert code == 200 and release == {"released": True, "clean_stop": False}
        attempt = subprocess.run(
            ["docker", "exec", h.name, "python", "/fault-tests/control.py", "try_start"],
            capture_output=True, text=True, timeout=30,
        )
        response = json.loads(attempt.stdout)
        assert attempt.returncode == 78 and response == {"rejected": True, "reason": "runtime_guard"}
        h.kill()
        h.restart()
        return {"released": release, "new_api": response, "exit_code": attempt.returncode,
                "whole_container_restart_ready": True, "kills": h.kill_evidence}


def orphan_guard(image):
    with Harness(image, "orphan_guard") as h:
        job_id = h.submit(video=True)
        child = h.control("freeze_media")
        assert child["instance_lock_inherited"]
        killed_api = h.control("kill_api")
        api_exit = h.control("marker", "api-exit")
        assert api_exit["returncode"] == -9
        state = json.loads(docker("inspect", h.name))[0]["State"]
        live = h.control("process", str(child["pid"]))
        assert state["Running"] and live and live["media"] and live["instance_lock_inherited"]
        duplicate = h.name + "-duplicate"
        h.containers.append(duplicate)
        h.run_container(duplicate, ["python", "/fault-tests/control.py", "try_start"])
        code = int(docker("wait", duplicate, timeout=30))
        response = json.loads(docker("logs", duplicate))
        assert code == 78 and response == {"rejected": True, "reason": "instance_lock"}
        h.kill()
        h.restart()
        after = h.finish(job_id)
        assert after["status"] == "succeeded" and after["execution"]["attempts"] == 2
        return {"killed_api": killed_api, "api_exit": api_exit, "container_kept_running": True,
                "orphan_ffmpeg": live, "second_container": response, "second_exit_code": code,
                "after": compact(after), "artifact": h.verify_artifact(after),
                "after_snapshot": h.control("snapshot", job_id), "kills": h.kill_evidence}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="ffmpeg-agent:faults-local")
    parser.add_argument("--report", type=Path, default=ROOT / "docs/verification/container-faults-arm64.json")
    parser.add_argument("--case", choices=("hard_restart", "intent", "staged", "renamed", "committed",
                                         "cancel_restart", "hot_guard", "orphan_guard"))
    args = parser.parse_args()
    image_info = json.loads(docker("image", "inspect", args.image))[0]
    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(), "image": args.image,
        "image_id": image_info["Id"], "architecture": image_info["Architecture"],
        "checks": [], "cleanup": "label_verified_owned_resources_only",
    }
    cases = [("hard_restart", hard_restart),
             *[(mode, lambda image, mode=mode: publishing(image, mode))
               for mode in ("intent", "staged", "renamed", "committed")],
             ("cancel_restart", cancel_restart), ("hot_guard", hot_guard), ("orphan_guard", orphan_guard)]
    name = "setup"
    try:
        for name, test in cases:
            if args.case and name != args.case:
                continue
            started = time.monotonic()
            result = test(args.image)
            report["checks"].append({"case": name, "passed": True,
                                     "duration_seconds": round(time.monotonic() - started, 3),
                                     "evidence": result})
            print(json.dumps({"case": name, "passed": True}), flush=True)
        report["passed"] = True
    except Exception as error:
        report["passed"] = False
        report["failure"] = {"case": name, "type": type(error).__name__}
        raise
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
