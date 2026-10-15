"""在精确生产镜像上验证六类媒体能力的 HTTP/MCP 参数与实际产物。"""

import argparse
import asyncio
import hashlib
import importlib.util
import io
import json
import logging
import re
import subprocess
import uuid
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "upload_mcp_verification", ROOT / "deploy/verify-upload-mcp.py"
)
assert spec and spec.loader
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)

CASES = (
    {
        "tool": "probe_media",
        "operation": "media.probe.v1",
        "source": "video",
        "input": "media",
        "output": "report",
        "params": {
            "sections": ["format", "streams", "tags", "chapters"],
            "stream_indices": [0],
            "count_frames": True,
        },
    },
    {
        "tool": "transcode_video",
        "operation": "video.transcode.v1",
        "source": "video",
        "input": "video",
        "output": "video",
        "params": {
            "codec": "hevc",
            "speed": "fast",
            "resize": {"width": 320},
            "frame_rate": {"mode": "constant", "value": "12/1"},
            "rate_control": {"mode": "crf", "value": 29},
            "audio": {"mode": "drop"},
            "range": {"end_ms": 1000},
        },
    },
    {
        "tool": "transcode_audio",
        "operation": "audio.transcode.v1",
        "source": "audio",
        "input": "audio",
        "output": "audio",
        "params": {
            "codec": "opus",
            "sample_rate": 48000,
            "channels": 1,
            "rate_control": {"mode": "bitrate", "bitrate_bps": 64000},
            "range": {"end_ms": 1000},
        },
    },
    {
        "tool": "extract_audio",
        "operation": "video.extract_audio.v1",
        "source": "video",
        "input": "video",
        "output": "audio",
        "params": {"mode": "copy", "container": "m4a"},
    },
    {
        "tool": "extract_frames",
        "operation": "video.extract_frames.v1",
        "source": "video",
        "input": "video",
        "output": "frames",
        "params": {
            "sampling": {"mode": "count", "count": 3, "end_ms": 900},
            "frame_selection": "nearest",
            "image": {"width": 80, "format": "png", "compression_level": 9},
        },
    },
    {
        "tool": "resize_image",
        "operation": "image.resize.v1",
        "source": "image",
        "input": "image",
        "output": "image",
        "params": {
            "width": 80,
            "height": 80,
            "fit": "cover",
            "crop_y": 0.25,
            "format": "webp",
            "quality": 70,
            "method": 5,
        },
    },
)
FIXTURES = {"video": "ffmpeg_h264_640x360.mkv", "audio": "222.mp3", "image": "haojiao.jpg"}


def safe_parameters(value: Any, secrets: tuple[str, ...]) -> None:
    if isinstance(value, dict):
        assert (
            not {"source", "url", "headers", "file_id", "storage_id", "key", "filename"}
            & value.keys()
        ), "unsafe_parameter_field"
        for child in value.values():
            safe_parameters(child, secrets)
    elif isinstance(value, list):
        for child in value:
            safe_parameters(child, secrets)
    elif isinstance(value, str):
        assert all(secret not in value for secret in secrets), "private_parameter_value"


def load_fixtures() -> dict[str, bytes]:
    manifest = {
        item["name"]: item
        for item in json.loads((ROOT / "tests/fixtures/checksums.json").read_text())
    }
    result = {}
    for kind, name in FIXTURES.items():
        data = (ROOT / "tests/fixtures" / name).read_bytes()
        expected = manifest[name]
        assert len(data) == expected["size_bytes"], "fixture_size_mismatch"
        assert hashlib.sha256(data).hexdigest() == expected["sha256"], "fixture_sha_mismatch"
        result[kind] = data
    return result


async def upload(client: httpx.AsyncClient, data: bytes) -> dict:
    response = await client.post(
        "/v1/files",
        content=data,
        headers={
            "Content-Type": "application/octet-stream",
            "Idempotency-Key": "parameters-" + uuid.uuid4().hex,
            "X-File-Size": str(len(data)),
            "X-File-Sha256": hashlib.sha256(data).hexdigest(),
        },
    )
    assert response.status_code == 201, "upload_failed"
    file = response.json()
    assert file["state"] == "ready" and file["size_bytes"] == len(data), "upload_not_ready"
    assert file["sha256"] == hashlib.sha256(data).hexdigest(), "upload_identity_mismatch"
    return {"kind": "uploaded_file", "file_id": file["file_id"]}


def definition(case: dict, source: dict) -> dict:
    return {
        "inputs": {"main": source},
        "steps": [
            {
                "id": "process",
                "operation": case["operation"],
                "params": case["params"],
                "inputs": {case["input"]: {"source": "main"}},
            }
        ],
        "outputs": {"result": {"step": "process", "output": case["output"]}},
    }


def probe_bytes(container: str, data: bytes, frames: bool = False) -> dict:
    options = ["-show_frames", "-select_streams", "v:0"] if frames else []
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "ffprobe",
            "-v",
            "error",
            "-threads",
            "1",
            "-show_streams",
            "-show_format",
            *options,
            "-of",
            "json",
            "pipe:0",
        ],
        input=data,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, "downloaded_media_probe_failed"
    return json.loads(result.stdout)


def normalized_pts(report: dict) -> list[Fraction]:
    times = [Fraction(frame["pts_time"]) for frame in report["frames"]]
    assert times, "missing_decoded_frames"
    return [value - times[0] for value in times]


def decode_media(container: str, data: bytes, tool: str, defaults: bool = False) -> dict:
    if tool == "probe_media":
        report = json.loads(data)
        assert report["type"] == "video", "probe_type_mismatch"
        if defaults:
            assert len(report["streams"]) == 2 and "tags" not in report, (
                "default_probe_sections_mismatch"
            )
            return {"type": "json", "stream_count": 2}
        assert len(report["streams"]) == 1, "probe_selection_mismatch"
        assert report["streams"][0]["index"] == 0, "probe_stream_mismatch"
        assert report["streams"][0]["decoded_frame_count"] == 24, "probe_frame_count_mismatch"
        assert "tags" in report and "chapters" in report, "probe_sections_missing"
        return {"type": "json", "decoded_frame_count": 24}
    if tool in {"resize_image", "extract_frames"}:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if defaults:
                expected = ("JPEG", (1279, 1706) if tool == "resize_image" else (640, 360))
                reference = io.BytesIO()
                Image.new("RGB", (1, 1)).save(reference, format="JPEG", quality=90)
                reference.seek(0)
                with Image.open(reference) as sample:
                    assert getattr(image, "quantization", None) == getattr(
                        sample, "quantization", None
                    ), "default_jpeg_quality_mismatch"
            else:
                expected = ("WEBP", (80, 80)) if tool == "resize_image" else ("PNG", (80, 45))
            assert (image.format, image.size) == expected, "image_format_or_size_mismatch"
            assert not image.getexif() and "icc_profile" not in image.info, (
                "image_metadata_not_removed"
            )
            return {
                "format": image.format,
                "width": image.width,
                "height": image.height,
                **({"quality": 90} if defaults else {}),
            }
    report = probe_bytes(container, data)
    streams = report["streams"]
    if tool == "transcode_video":
        video = next(stream for stream in streams if stream["codec_type"] == "video")
        if defaults:
            assert (video["codec_name"], video["width"], video["height"]) == ("h264", 640, 360), (
                "default_video_dimensions_mismatch"
            )
            actual_pts = normalized_pts(probe_bytes(container, data, frames=True))
            source = (ROOT / "tests/fixtures" / FIXTURES["video"]).read_bytes()
            assert actual_pts == normalized_pts(probe_bytes(container, source, frames=True)), (
                "default_video_source_timestamps_changed"
            )
            audio = next(stream for stream in streams if stream["codec_type"] == "audio")
            assert (audio["codec_name"], int(audio["sample_rate"]), audio["channels"]) == (
                "aac",
                44100,
                1,
            ), "default_video_audio_changed"
            return {
                "codec": "h264",
                "width": 640,
                "height": 360,
                "source_timestamps_preserved": True,
                "decoded_frame_count": len(actual_pts),
                "audio_sample_rate": 44100,
                "audio_channels": 1,
            }
        assert len(streams) == 1, "unexpected_output_stream_count"
        assert (video["codec_name"], video["width"], video["height"], video["avg_frame_rate"]) == (
            "hevc",
            320,
            180,
            "12/1",
        ), "video_encoding_mismatch"
        return {"codec": "hevc", "width": 320, "height": 180, "frame_rate": "12/1"}
    assert len(streams) == 1, "unexpected_output_stream_count"
    stream = streams[0]
    expected_audio = (
        (("aac", 22050, 1) if defaults else ("opus", 48000, 1))
        if tool == "transcode_audio"
        else ("aac", 44100, 1)
    )
    actual_audio = (stream["codec_name"], int(stream["sample_rate"]), stream["channels"])
    assert actual_audio == expected_audio, "audio_encoding_mismatch"
    return {"codec": actual_audio[0], "sample_rate": actual_audio[1], "channels": actual_audio[2]}


async def download_member(harness, client: httpx.AsyncClient, artifact: dict, item: dict) -> bytes:
    key = item["member_key"]
    assert re.fullmatch(r"frame_[0-9]+\.(?:png|jpg)", key), "unexpected_member_key"
    base = urlsplit(artifact["access"]["http"]["files_base"]).path
    assert base == f"/v1/artifacts/{artifact['artifact_id']}/files/", "member_endpoint_mismatch"
    path = base + key
    if harness.authenticated:
        denied = await client.get(path, headers={"Authorization": "Bearer invalid"})
        assert denied.status_code == 401, "member_authentication_bypass"
    head = await client.head(path)
    response = await client.get(path)
    assert head.status_code == response.status_code == 200, "member_download_failed"
    assert not head.content, "member_head_returned_body"
    assert int(head.headers["content-length"]) == len(response.content) == item["size_bytes"], (
        "member_size_mismatch"
    )
    assert hashlib.sha256(response.content).hexdigest() == item["sha256"], "member_sha_mismatch"
    ranged = await client.get(path, headers={"Range": "bytes=0-15"})
    assert ranged.status_code == 206 and ranged.content == response.content[:16], (
        "member_range_failed"
    )
    return response.content


async def inspect_artifact(
    harness,
    client: httpx.AsyncClient,
    session: ClientSession,
    job: dict,
    case: dict,
    defaults: bool = False,
) -> dict:
    artifact_id = job["outputs"]["result"]["artifact_id"]
    artifact = await shared.call(session, "get_artifact", {"artifact_id": artifact_id})
    public = await client.get(f"/v1/artifacts/{artifact_id}")
    assert public.status_code == 200 and public.json() == artifact, "artifact_transport_mismatch"
    safe_parameters(
        artifact["media"]["effective_params"], (str(ROOT), harness.temp.name, harness.token)
    )
    files = []
    if case["tool"] == "extract_frames":
        assert artifact["kind"] == "collection", "frame_result_not_collection"
        members = await shared.call(
            session, "list_artifact_members", {"artifact_id": artifact_id, "limit": 10}
        )
        response = await client.get(f"/v1/artifacts/{artifact_id}/members", params={"limit": 10})
        assert response.status_code == 200 and response.json() == members, (
            "members_transport_mismatch"
        )
        requested = [0] if defaults else [0, 450, 900]
        assert members["next_cursor"] is None and len(members["items"]) == len(requested), (
            "frame_member_count_mismatch"
        )
        assert [item["requested_time_ms"] for item in members["items"]] == requested, (
            "frame_requests_mismatch"
        )
        for item in members["items"]:
            assert item["error"] is None, "frame_member_failed"
            assert abs(item["actual_time_ms"] - item["requested_time_ms"]) < 50, (
                "frame_selection_mismatch"
            )
            data = await download_member(harness, client, artifact, item)
            actual = await asyncio.to_thread(
                decode_media, harness.name, data, case["tool"], defaults
            )
            files.append(
                {
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "actual": actual,
                    "requested_time_ms": item["requested_time_ms"],
                    "actual_time_ms": item["actual_time_ms"],
                }
            )
        assert sum(file["size_bytes"] for file in files) == artifact["size_bytes"], (
            "collection_size_mismatch"
        )
    else:
        assert artifact["kind"] == "file", "unexpected_artifact_kind"
        data = await shared.download(harness, client, artifact)
        assert len(data) == artifact["size_bytes"], "artifact_size_mismatch"
        actual = await asyncio.to_thread(decode_media, harness.name, data, case["tool"], defaults)
        files.append(
            {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "actual": actual}
        )
    return {
        "artifact_id": artifact_id,
        "effective_params": artifact["media"]["effective_params"],
        "files": files,
    }


async def verify_case(
    harness,
    client: httpx.AsyncClient,
    session: ClientSession,
    source: dict,
    case: dict,
    defaults: bool = False,
) -> dict:
    prefix = case["tool"] + ("_defaults" if defaults else "_custom")
    harness.stage = prefix + "_submission"
    accepted = await client.post(
        "/v1/jobs",
        json=definition(case | {"params": {}} if defaults else case, source),
        headers={
            "Idempotency-Key": "parameters-http-" + uuid.uuid4().hex,
        },
    )
    assert accepted.status_code == 202, "http_submission_failed"
    arguments = {
        "source": source,
        "idempotency_key": "parameters-mcp-" + uuid.uuid4().hex,
    }
    if not defaults:
        arguments["params"] = case["params"]
    mcp_accepted = await shared.call(session, case["tool"], arguments)
    replay = await shared.call(session, case["tool"], arguments)
    assert replay["job_id"] == mcp_accepted["job_id"], "mcp_idempotency_mismatch"
    harness.stage = prefix + "_execution_and_download"
    results, parameters = [], []
    for transport, job_id in (("http", accepted.json()["job_id"]), ("mcp", mcp_accepted["job_id"])):
        job = await shared.finished(session, job_id)
        public = await client.get(f"/v1/jobs/{job_id}")
        assert public.status_code == 200 and public.json() == job, "job_transport_mismatch"
        assert len(job["steps"]) == 1, "unexpected_job_step_count"
        step = job["steps"][0]
        output = job["outputs"]["result"]
        assert step["status"] == "succeeded" and step["error"] is None, "media_step_failed"
        assert output["step"] == step["id"] and output["output"] == case["output"], (
            "output_binding_mismatch"
        )
        assert output["artifact_id"] == step["artifact_id"], "output_artifact_mismatch"
        effective = step["effective_params"]
        safe_parameters(effective, (str(ROOT), harness.temp.name, harness.token))
        parameters.append(effective)
        artifact = await inspect_artifact(harness, client, session, job, case, defaults)
        results.append({"transport": transport, "job_id": job_id, **artifact})
    assert parameters[0] == parameters[1], "job_effective_parameters_mismatch"
    if defaults:
        effective = parameters[0]
        if case["tool"] == "transcode_video":
            assert effective["preset"] is None, "default_video_used_named_preset"
            assert effective["resize"]["width"] is None and effective["resize"]["height"] is None, (
                "default_video_resize_mismatch"
            )
            assert effective["frame_rate"]["mode"] == "source", "default_video_frame_rate_mismatch"
        elif case["tool"] in {"transcode_audio", "extract_audio"}:
            assert effective["sample_rate"] == effective["channels"] == "auto", (
                "default_audio_not_adaptive"
            )
        elif case["tool"] == "extract_frames":
            assert effective["sampling"] == {"mode": "timestamps", "timestamps_ms": [0]}, (
                "default_frame_sampling_mismatch"
            )
            assert effective["image"]["quality"] == 90, "default_frame_quality_mismatch"
        elif case["tool"] == "resize_image":
            assert effective["width"] is None and effective["height"] is None, (
                "default_image_resize_mismatch"
            )
            assert effective["format"] == "jpeg" and effective["quality"] == 90, (
                "default_image_quality_mismatch"
            )
    assert results[0]["effective_params"] == results[1]["effective_params"], (
        "artifact_effective_parameters_mismatch"
    )
    return {
        "tool": case["tool"],
        "operation": case["operation"],
        "default_parameters": defaults,
        "effective_params": parameters[0],
        "results": results,
    }


async def verify(image: str, report: dict) -> None:
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", image), "exact_image_id_required"
    image_info = json.loads(await asyncio.to_thread(shared.docker, "image", "inspect", image))[0]
    assert image_info["Id"] == image, "image_identity_mismatch"
    image_version = next(
        value.partition("=")[2]
        for value in image_info["Config"]["Env"]
        if value.startswith("MEDIA_IMAGE_VERSION=")
    )
    contents = load_fixtures()
    report["fixtures"] = {
        kind: {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for kind, data in contents.items()
    }
    for authenticated in (True, False):
        record: dict[str, Any] = {"token_enabled": authenticated, "checks": [], "cases": []}
        report["modes"].append(record)
        async with shared.running(image, authenticated, record) as harness:
            record["resource_owner_id"] = harness.owner
            record["checks"].append("nonroot_readonly_exact_image_without_shared_input")
            async with httpx.AsyncClient(
                base_url=harness.base, headers=harness.headers, timeout=30
            ) as client:
                if authenticated:
                    for method, path in (
                        ("GET", "/v1/capabilities"),
                        ("POST", "/v1/files"),
                        ("POST", "/mcp"),
                    ):
                        denied = await client.request(
                            method,
                            path,
                            content=b"not-read",
                            headers={"Authorization": "Bearer invalid"},
                        )
                        assert denied.status_code == 401, "entry_authentication_bypass"
                sources = {kind: await upload(client, data) for kind, data in contents.items()}
                record["checks"].append("verified_raw_http_uploads")
                async with shared.remote(harness) as session:
                    listed = await session.list_tools()
                    tools = {tool.name: tool for tool in listed.tools}
                    assert len(tools) == 19, "tool_discovery_mismatch"
                    response = await client.get("/v1/capabilities")
                    assert response.status_code == 200, "http_capabilities_failed"
                    capabilities = response.json()
                    assert capabilities == await shared.call(session, "list_capabilities", {}), (
                        "capabilities_transport_mismatch"
                    )
                    assert set(capabilities["operations"]) == {
                        case["operation"] for case in CASES
                    }, "unexpected_operation_versions"
                    assert "media_parameters" in capabilities, "parameter_capabilities_missing"
                    assert capabilities["engine"]["image_version"] == image_version, (
                        "capability_image_version_mismatch"
                    )
                    record["engine"] = {
                        key: capabilities["engine"][key]
                        for key in (
                            "ffmpeg",
                            "ffprobe",
                            "image_version",
                            "implementation_sha256",
                        )
                    }
                    for case in CASES:
                        schema = tools[case["tool"]].input_schema
                        assert "operation_version" not in schema["properties"], (
                            "unexpected_operation_version_argument"
                        )
                        assert "params" not in schema.get("required", []), "params_is_required"
                        assert case["operation"] in capabilities["operations"], (
                            "operation_not_advertised"
                        )
                        record["cases"].append(
                            await verify_case(
                                harness, client, session, sources[case["source"]], case
                            )
                        )
                        record["cases"].append(
                            await verify_case(
                                harness,
                                client,
                                session,
                                sources[case["source"]],
                                case,
                                defaults=True,
                            )
                        )
                    record["checks"].extend(
                        [
                            "remote_mcp_discovery_and_http_capability_parity",
                            "six_media_tools_over_real_tcp_http_and_mcp",
                            "job_and_artifact_effective_parameter_parity",
                            "mcp_idempotency_replay",
                            "all_files_and_collection_members_head_range_size_sha_download",
                            "downloaded_media_decoded_and_properties_verified",
                            "all_six_tools_accept_omitted_mcp_params_and_empty_http_params",
                            "adaptive_defaults_preserve_video_timestamps_audio_properties_and_image_size",
                        ]
                    )
            assert load_fixtures() == contents, "project_fixture_changed"
            record["checks"].append("project_fixtures_remain_unchanged")
        containers = await asyncio.to_thread(
            shared.docker, "ps", "-aq", "--filter", f"label={shared.LABEL}={harness.owner}"
        )
        volumes = await asyncio.to_thread(
            shared.docker, "volume", "ls", "-q", "--filter", f"label={shared.LABEL}={harness.owner}"
        )
        assert not containers and not volumes, "owned_resources_remain"
        record["cleanup"] = {"containers_remaining": 0, "volumes_remaining": 0}
        record["checks"].append("only_owned_resources_removed")
    report["status"] = "passed"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="精确 sha256 镜像 ID")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    report: dict[str, Any] = {
        "image_id": args.image,
        "created_at": datetime.now(UTC).isoformat(),
        "modes": [],
    }
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
