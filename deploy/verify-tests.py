import argparse
import json
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

LABEL = "io.ffmpeg-agent.verification"


def main() -> int:
    parser = argparse.ArgumentParser(description="在精确服务镜像内执行完整测试套件")
    parser.add_argument("--image", default="ffmpeg-agent:v1-local")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    owner = uuid4().hex
    name = f"ffmpeg-tests-{owner[:12]}"
    inspected = subprocess.run(
        ["docker", "image", "inspect", args.image], capture_output=True, text=True, check=True
    )
    image = json.loads(inspected.stdout)[0]
    command = [
        "docker", "run", "--rm", "--name", name, "--label", f"{LABEL}={owner}",
        "--init", "--read-only", "--user", "10001:10001",
        # 仅此验证 venv 在 tmpfs；动态扩展需要 exec，服务 Compose 不更改。
        "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=1536m",
    ]
    for part in ("tests", "scripts", "examples", "deploy", "pyproject.toml", ".env.example"):
        command += ["--mount", f"type=bind,src={root / part},dst=/verification/{part},readonly"]
    command += [
        "--env", "MEDIA_TEST_ENGINE_SERIES=7.1",
        "--env", "UV_CACHE_DIR=/tmp/uv-cache",
        "--env", "UV_PROJECT_ENVIRONMENT=/tmp/verification-venv",
        image["Id"], "sh", "-c",
        "uv sync --frozen --no-install-project --python /usr/local/bin/python "
        "&& cd /verification && /tmp/verification-venv/bin/python -m pytest -q "
        "-o cache_dir=/tmp/pytest-cache",
    ]
    started = time.monotonic()
    report = {
        "status": "failed", "image_id": image["Id"], "architecture": image["Architecture"],
        "recorded_at": datetime.now(UTC).isoformat(), "engine_series": "7.1",
        "checks": ["non_root", "readonly_root", "locked_test_dependencies", "full_pytest_suite"],
    }
    try:
        with args.log.open("w") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=900)
        summary = re.findall(r"^.*\b(?:passed|failed|error)\b.*in [0-9.]+s.*$",
                             args.log.read_text(), re.MULTILINE)
        report.update({
            "status": "passed" if result.returncode == 0 else "failed",
            "exit_code": result.returncode,
            "pytest_summary": summary[-1] if summary else "没有 pytest 终态汇总",
        })
        return result.returncode
    finally:
        probe = subprocess.run(["docker", "inspect", name], capture_output=True, text=True)
        if probe.returncode == 0:
            container = json.loads(probe.stdout)[0]
            assert container["Config"]["Labels"][LABEL] == owner
            subprocess.run(["docker", "rm", "--force", name], capture_output=True, check=True)
        report["elapsed_seconds"] = time.monotonic() - started
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
