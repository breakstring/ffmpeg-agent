import argparse
import hashlib
import importlib.util
import json
import math
import secrets
import statistics
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

_support_spec = importlib.util.spec_from_file_location(
    "deployment_env_support", Path(__file__).with_name("env_support.py"),
)
assert _support_spec and _support_spec.loader
env_support = importlib.util.module_from_spec(_support_spec)
_support_spec.loader.exec_module(env_support)


LABEL = "io.ffmpeg-agent.verification"
TERMINAL = {"succeeded", "partial", "failed", "canceled"}
SAMPLE_CODE = """
import json,os,time
from pathlib import Path
sampled_at=time.monotonic()
cpu={k:int(v) for k,v in (line.split() for line in Path('/sys/fs/cgroup/cpu.stat').read_text().splitlines())}
rss=0
counts={'ffmpeg':0,'ffprobe':0}
for p in Path('/proc').iterdir():
    if not p.name.isdigit() or int(p.name)==os.getpid(): continue
    try:
        name=(p/'comm').read_text().strip()
        if name in counts: counts[name]+=1
        for line in (p/'status').read_text().splitlines():
            if line.startswith('VmRSS:'): rss+=int(line.split()[1])*1024
    except (OSError,ValueError): pass
logical=allocated=0
for p in Path('/var/lib/ffmpeg-agent/work').rglob('*'):
    try:
        if p.is_file():
            stat=p.stat()
            logical+=stat.st_size
            allocated+=stat.st_blocks*512
    except OSError: pass
print(json.dumps({'rss_bytes':rss,'cpu_usage_usec':cpu['usage_usec'],'sample_monotonic':sampled_at,
    'memory_current_bytes':int(Path('/sys/fs/cgroup/memory.current').read_text()),
    'memory_peak_bytes':int(Path('/sys/fs/cgroup/memory.peak').read_text()),
    'work_logical_bytes':logical,'work_allocated_bytes':allocated,
    'ffmpeg_count':counts['ffmpeg'],'ffprobe_count':counts['ffprobe']}))
"""
QUEUE_CODE = """
import json,sqlite3,sys
db=sqlite3.connect('file:/var/lib/ffmpeg-agent/state/state.sqlite?mode=ro',uri=True)
rows=[]
for job_id in json.loads(sys.argv[1]):
    row=db.execute('SELECT j.created,j.terminal_at,min(a.started) FROM jobs j JOIN attempts a ON a.job_id=j.id WHERE j.id=? GROUP BY j.id',(job_id,)).fetchone()
    assert row is not None and row[1] is not None
    rows.append({'job_id':job_id,'queue_seconds':row[2]-row[0],'total_seconds':row[1]-row[0]})
db.close()
print(json.dumps(rows))
"""


def docker(*args: str, timeout: float = 60, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"docker {args[0]} failed with exit {result.returncode}")
    return result.stdout.strip()


class Client:
    def __init__(self, port: str, token: str):
        self.base = f"http://127.0.0.1:{port}"
        self.token = token
        self.latencies: list[float] = []

    def request(self, method: str, path: str, body=None, key=None) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        started = time.monotonic()
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            payload = json.load(response)
            status = response.getcode()
            assert isinstance(status, int)
        if path.startswith("/v1/jobs/") and method == "GET":
            self.latencies.append((time.monotonic() - started) * 1000)
        return status, payload

    def ready(self) -> None:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                if self.request("GET", "/health/ready")[0] == 200:
                    return
            except OSError, ValueError:
                pass
            time.sleep(0.2)
        raise AssertionError("容器未就绪")


def percentile(values: list[float], percent: int) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percent * len(ordered) / 100) - 1)]


def cleanup(owner: str, volumes: list[str]) -> None:
    containers = docker("ps", "-aq", "--filter", f"label={LABEL}={owner}").split()
    for container in containers:
        data = json.loads(docker("inspect", container))[0]
        assert data["Config"]["Labels"][LABEL] == owner
        docker("rm", "--force", container)
    for volume in volumes:
        result = docker("volume", "inspect", volume, check=False)
        if result:
            assert json.loads(result)[0]["Labels"][LABEL] == owner
            docker("volume", "rm", volume)


def run(image: str, workers: int, jobs: int) -> dict:
    root = Path(__file__).resolve().parents[1]
    fixture = root / "tests/fixtures/video_h264_aac_square.mp4"
    digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    owner = uuid4().hex
    name = f"ffmpeg-benchmark-{owner[:12]}"
    volumes = {part: f"{name}-{part}" for part in ("state", "work", "input", "output")}
    token = secrets.token_urlsafe(48)
    samples = []
    with tempfile.TemporaryDirectory(prefix="ffmpeg-benchmark-") as temp:
        config = json.loads((root / "deploy/media-config.example.json").read_text())
        config["service_token"] = token
        config["worker_concurrency"] = workers
        config_path = Path(temp) / "config.json"
        environment_path = env_support.write_runtime_configuration(config, config_path)
        try:
            for volume in volumes.values():
                docker("volume", "create", "--label", f"{LABEL}={owner}", volume)
            copy_code = (
                "import hashlib,shutil,sys; from pathlib import Path; "
                "source=Path('/fixture.mp4'); target=Path('/media/input/video_h264_aac_square.mp4'); "
                "shutil.copyfile(source,target); target.chmod(0o444); "
                "assert hashlib.sha256(target.read_bytes()).hexdigest()==sys.argv[1]"
            )
            docker(
                "run",
                "--rm",
                "--label",
                f"{LABEL}={owner}",
                "--mount",
                f"type=volume,src={volumes['input']},dst=/media/input",
                "--mount",
                f"type=bind,src={fixture},dst=/fixture.mp4,readonly",
                image,
                "python",
                "-c",
                copy_code,
                digest,
            )
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
                "--mount",
                f"type=volume,src={volumes['state']},dst=/var/lib/ffmpeg-agent/state",
                "--mount",
                f"type=volume,src={volumes['work']},dst=/var/lib/ffmpeg-agent/work",
                "--mount",
                f"type=volume,src={volumes['input']},dst=/media/input,readonly",
                "--mount",
                f"type=volume,src={volumes['output']},dst=/media/output",
                "--mount",
                f"type=bind,src={config_path},dst=/run/secrets/media-config.json,readonly",
                *env_support.environment_mount(environment_path),
                image,
            )
            port = docker("port", name, "8080/tcp").rsplit(":", 1)[1]
            client = Client(port, token)
            client.ready()
            image_id = json.loads(docker("inspect", name))[0]["Image"]

            def sample():
                return json.loads(docker("exec", name, "python", "-c", SAMPLE_CODE, timeout=15))

            body = {
                "inputs": {
                    "main": {
                        "kind": "shared_file",
                        "storage_id": "input",
                        "key": fixture.name,
                        "expected_size_bytes": fixture.stat().st_size,
                        "expected_sha256": digest,
                    }
                },
                "steps": [
                    {
                        "id": "encode",
                        "operation": "video.transcode.v1",
                        "inputs": {"video": {"source": "main"}},
                        "params": {"preset": "web_720p_v1"},
                    }
                ],
                "outputs": {"video": {"step": "encode", "output": "video"}},
            }
            before = sample()
            started = time.monotonic()
            ids = []
            for index in range(jobs):
                status, accepted = client.request("POST", "/v1/jobs", body, f"{owner}-{index}")
                assert status == 202, f"创建任务返回 {status}"
                ids.append(accepted["job_id"])
            pending = set(ids)
            completed = {}
            next_sample = 0
            deadline = started + 900
            while pending and time.monotonic() < deadline:
                for job_id in list(pending):
                    status, job = client.request("GET", f"/v1/jobs/{job_id}")
                    assert status == 200
                    if job["status"] in TERMINAL:
                        assert job["status"] == "succeeded" and job["inputs_released"] is True, job[
                            "status"
                        ]
                        completed[job_id] = job
                        pending.remove(job_id)
                if time.monotonic() >= next_sample:
                    observation = sample()
                    observation["elapsed_seconds"] = time.monotonic() - started
                    samples.append(observation)
                    next_sample = time.monotonic() + 0.5
                if pending:
                    time.sleep(0.1)
            assert not pending, "基准任务未在总预算内完成"
            elapsed = time.monotonic() - started
            after = sample()
            samples.append(after | {"elapsed_seconds": elapsed})
            latencies = list(client.latencies)
            queues = json.loads(docker("exec", name, "python", "-c", QUEUE_CODE, json.dumps(ids)))

            status, cancel_job = client.request("POST", "/v1/jobs", body, f"{owner}-cancel")
            assert status == 202
            cancel_id = cancel_job["job_id"]
            cancellation_deadline = time.monotonic() + 45
            while time.monotonic() < cancellation_deadline:
                if sample()["ffmpeg_count"] > 0:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("取消试验未观察到实际 FFmpeg 进程")
            cancel_started = time.monotonic()
            assert client.request("POST", f"/v1/jobs/{cancel_id}/cancel")[0] == 202
            while time.monotonic() < cancellation_deadline:
                status, canceled = client.request("GET", f"/v1/jobs/{cancel_id}")
                assert status == 200
                if canceled["status"] == "canceled" and canceled["inputs_released"]:
                    stopped = sample()
                    assert stopped["ffmpeg_count"] == 0 and stopped["ffprobe_count"] == 0
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("取消试验未确认进程停止和输入释放")
            cancel_seconds = time.monotonic() - cancel_started
            intervals = list(zip([before, *samples[:-1]], samples, strict=True))
            peak_cpu = max(
                (last["cpu_usage_usec"] - first["cpu_usage_usec"])
                / ((last["sample_monotonic"] - first["sample_monotonic"]) * 10000)
                for first, last in intervals
            )
            assert max(row["ffmpeg_count"] + row["ffprobe_count"] for row in samples) <= workers
            assert hashlib.sha256(fixture.read_bytes()).hexdigest() == digest
            return {
                "status": "passed",
                "workers": workers,
                "image_id": image_id,
                "jobs": jobs,
                "fixture": fixture.name,
                "fixture_sha256": digest,
                "container_limits": {"cpus": 2, "memory_bytes": 2 * 1024**3, "pids": 128},
                "elapsed_seconds": elapsed,
                "throughput_jobs_per_minute": jobs / elapsed * 60,
                "queue_seconds": {
                    "mean": statistics.mean(row["queue_seconds"] for row in queues),
                    "max": max(row["queue_seconds"] for row in queues),
                },
                "api_get_job_ms": {
                    "samples": len(latencies),
                    "p50": percentile(latencies, 50),
                    "p95": percentile(latencies, 95),
                    "max": max(latencies),
                },
                "cancel_seconds_including_stop_confirmation": cancel_seconds,
                "peak_observed_process_rss_bytes": max(row["rss_bytes"] for row in samples),
                "peak_container_memory_bytes": max(row["memory_peak_bytes"] for row in samples),
                "mean_cpu_percent": (after["cpu_usage_usec"] - before["cpu_usage_usec"])
                / ((after["sample_monotonic"] - before["sample_monotonic"]) * 10000),
                "peak_observed_cpu_percent": peak_cpu,
                "peak_work_allocated_bytes": max(row["work_allocated_bytes"] for row in samples),
                "peak_work_logical_bytes": max(row["work_logical_bytes"] for row in samples),
                "peak_observed_media_processes": max(
                    row["ffmpeg_count"] + row["ffprobe_count"] for row in samples
                ),
                "persisted_job_timings": queues,
                "samples": samples,
            }
        finally:
            cleanup(owner, list(volumes.values()))


def main() -> None:
    parser = argparse.ArgumentParser(description="真实视频转码 Worker 1/2/4 资源与取消实测")
    parser.add_argument("--image", default="ffmpeg-agent:v1-local")
    parser.add_argument("--workers", nargs="+", type=int, choices=(1, 2, 4), default=[1, 2, 4])
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not 1 <= args.jobs <= 16:
        parser.error("jobs 必须为 1～16，正式验收用 8")
    reports = []
    for workers in args.workers:
        result = run(args.image, workers, args.jobs)
        reports.append(result)
        if args.report:
            args.report.write_text(json.dumps(reports, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {key: value for key, value in result.items() if key != "samples"},
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
