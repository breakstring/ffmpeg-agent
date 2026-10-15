"""在隔离生产容器上直接执行 README 使用的 HTTP/MCP 客户端示例。"""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("example_harness", ROOT / "deploy/verify-upload-mcp.py")
assert spec and spec.loader
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)

CASES = {
    "probe_media": ("ffmpeg_h264_640x360.mkv", "json"),
    "transcode_video": ("ffmpeg_h264_640x360.mkv", "mp4"),
    "transcode_audio": ("222.mp3", "m4a"),
    "extract_audio": ("ffmpeg_h264_640x360.mkv", "m4a"),
    "extract_frames": ("ffmpeg_h264_640x360.mkv", "frames"),
    "resize_image": ("haojiao.jpg", "jpg"),
}


async def execute(arguments: list[str], expected: int = 0) -> str:
    result = await asyncio.to_thread(subprocess.run, arguments, cwd=ROOT, capture_output=True,
                                    text=True, timeout=180)
    assert result.returncode == expected, "example_exit_status"
    assert "Download verified" in result.stdout, "missing_download_verification"
    match = re.search(r"job_id: (job_[a-f0-9]{32})", result.stdout)
    assert match, "missing_job_id"
    return match[1]


def inspect_output(operation: str, target: Path) -> dict:
    if operation == "probe_media":
        report = json.loads(target.read_text())
        assert report["streams"], "empty_probe"
    elif operation == "extract_frames":
        report = json.loads((target / "manifest.json").read_text())
        succeeded = [item for item in report["items"] if not item.get("error")]
        assert succeeded, "empty_frame_collection"
        for item in succeeded:
            file = target / item["member_key"]
            assert file.stat().st_size == item["size_bytes"], "frame_size_mismatch"
            assert hashlib.sha256(file.read_bytes()).hexdigest() == item["sha256"], "frame_sha_mismatch"
            with Image.open(file) as image:
                image.load()
        return {"saved_members": len(succeeded), "failed_members": len(report["items"]) - len(succeeded)}
    elif operation == "resize_image":
        with Image.open(target) as image:
            image.load()
            assert image.format == "JPEG", "unexpected_image_format"
    assert target.stat().st_size > 0, "empty_download"
    return {"size_bytes": target.stat().st_size, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


async def verify_mode(image: str, authenticated: bool, http_python: str, record: dict) -> None:
    async with harness.running(image, authenticated, record) as server:
        work = Path(server.temp.name)
        client_config = work / "client.json"
        client_config.write_text(json.dumps({"public_base_url": server.base,
                                             "service_token": server.token if authenticated else None}))
        client_config.chmod(0o600)
        record["cases"] = []
        for route in ("multipart", "upload_first", "mcp"):
            for operation, (fixture, extension) in CASES.items():
                target = work / f"{route}-{operation}.{extension}"
                script = "mcp-workflow.py" if route == "mcp" else "http-workflow.py"
                arguments = [sys.executable if route == "mcp" else http_python,
                             str(ROOT / "examples" / script),
                             str(ROOT / "tests/fixtures" / fixture), str(target),
                             "--config", str(client_config), "--operation", operation]
                if route == "upload_first":
                    arguments.append("--upload-first")
                job_id = await execute(arguments)
                output = await asyncio.to_thread(inspect_output, operation, target)
                record["cases"].append({"route": route, "operation": operation,
                                         "job_id": job_id, "passed": True, "output": output})
                if operation == "transcode_video":
                    resumed = arguments.copy()
                    resumed[2] = str(work / "source-no-longer-present")
                    resumed[3] = str(work / f"{route}-resumed.mp4")
                    resumed += ["--job-id", job_id]
                    assert await execute(resumed) == job_id, "resume_created_new_job"
                    resumed_output = await asyncio.to_thread(inspect_output, operation, Path(resumed[3]))
                    assert resumed_output == output, "resume_changed_output"

        record["resume_routes"] = ["multipart", "upload_first", "mcp"]
        for route in ("multipart", "upload_first", "mcp"):
            script = "mcp-workflow.py" if route == "mcp" else "http-workflow.py"
            request_id = "example-retry-" + route
            arguments = [sys.executable if route == "mcp" else http_python,
                         str(ROOT / "examples" / script), str(ROOT / "tests/fixtures/haojiao.jpg"),
                         str(work / f"{route}-retry-first.jpg"), "--config", str(client_config),
                         "--operation", "resize_image", "--request-id", request_id]
            if route == "upload_first":
                arguments.append("--upload-first")
            job_id = await execute(arguments)
            arguments[3] = str(work / f"{route}-retry-second.jpg")
            assert await execute(arguments) == job_id, "retry_created_new_job"
        record["retry_routes"] = ["multipart", "upload_first", "mcp"]

        partial_params = work / "partial.json"
        partial_params.write_text(json.dumps({"sampling": {"mode": "timestamps", "timestamps_ms": [0, 999999]}}))
        for route in ("multipart", "mcp"):
            script = "mcp-workflow.py" if route == "mcp" else "http-workflow.py"
            target = work / f"{route}-partial"
            await execute([sys.executable if route == "mcp" else http_python,
                           str(ROOT / "examples" / script),
                           str(ROOT / "tests/fixtures/ffmpeg_h264_640x360.mkv"), str(target),
                           "--config", str(client_config), "--operation", "extract_frames",
                           "--params", str(partial_params)], expected=2)
            assert inspect_output("extract_frames", target) == {"saved_members": 1, "failed_members": 1}
        record["partial_routes"] = ["multipart", "mcp"]
        record["passed"] = True
    remaining = json.loads(harness.docker("ps", "-a", "--filter", f"label={harness.LABEL}={server.owner}",
                                         "--format", "json") or "[]")
    assert not remaining, "owned_container_remaining"
    volumes = harness.docker("volume", "ls", "--filter", f"label={harness.LABEL}={server.owner}", "-q")
    assert not volumes.strip(), "owned_volume_remaining"
    record["cleanup"] = {"containers_remaining": 0, "volumes_remaining": 0}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--http-python", default=sys.executable)
    args = parser.parse_args()
    image = json.loads(harness.docker("image", "inspect", args.image))[0]["Id"]
    manifest = json.loads((ROOT / "tests/fixtures/checksums.json").read_text())
    for item in manifest:
        data = (ROOT / "tests/fixtures" / item["name"]).read_bytes()
        assert len(data) == item["size_bytes"] and hashlib.sha256(data).hexdigest() == item["sha256"]
    report = {"image_id": image, "recorded_at": datetime.now(UTC).isoformat(),
              "example_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in sorted((ROOT / "examples").glob("*.py"))}, "modes": {}}
    try:
        for authenticated in (True, False):
            mode = "token" if authenticated else "open"
            report["modes"][mode] = {}
            await verify_mode(image, authenticated, args.http_python, report["modes"][mode])
        report["status"] = "passed"
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "default_cases": 36,
                      "resume_runs": 6, "retry_runs": 12, "partial_cases": 4}))


if __name__ == "__main__":
    asyncio.run(main())
