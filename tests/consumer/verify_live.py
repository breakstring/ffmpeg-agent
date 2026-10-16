"""对已授权的运行服务执行消费者验收；只上传仓库 fixture，不重建或停止服务。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "consumer"))
from ffmpeg_agent_client.envfile import read_env_file  # noqa: E402


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Validation:
    def __init__(self, args):
        previous = json.loads(args.private_record.read_text()) if args.resume else None
        self.root = (Path(previous["root"]) if previous else
                     Path(tempfile.mkdtemp(prefix="ffmpeg-consumer-live-")).resolve())
        os.chmod(self.root, 0o700)
        values = read_env_file(args.env_file)
        self.secrets = [values.get(key, "") for key in ("MEDIA_SERVICE_TOKEN", "MEDIA_LLM_API_KEY")]
        base = json.loads(args.config.read_text())["public_base_url"]
        self.env = self.root / "consumer.env"
        self.env.write_text("\n".join(key + "=" + json.dumps(value).replace("$", "$$")
            for key, value in {"MEDIA_PUBLIC_BASE_URL": base,
                               "MEDIA_SERVICE_TOKEN": values["MEDIA_SERVICE_TOKEN"]}.items()) + "\n")
        os.chmod(self.env, 0o600)
        self.python = str(Path(args.python).resolve())
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith(("PYTHON", "MEDIA_"))}
        self.cases = json.loads(args.report.read_text())["cases"] if previous else []
        self.outputs = previous["outputs"] if previous else []
        self.selected = set(args.case or [])
        self.resume = args.resume
        self.report = args.report
        self.private_record = args.private_record
        self.started = time.time()
        self.manifests = {}
        for skill in ("ffmpeg-agent-connect", "ffmpeg-agent-media"):
            shutil.copytree(ROOT / "skills" / skill, self.root / skill,
                            ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=bool(previous))
            self.manifests[skill] = digest(self.root / skill / "scripts/consumer-manifest.json")
        self.fixtures = json.loads((ROOT / "tests/fixtures/checksums.json").read_text())
        (self.root / "fixtures").mkdir(exist_ok=bool(previous))
        for item in self.fixtures:
            source = ROOT / "tests/fixtures" / item["name"]
            assert source.stat().st_size == item["size_bytes"] and digest(source) == item["sha256"]
            shutil.copyfile(source, self.root / "fixtures" / item["name"])
        self.script = self.root / "ffmpeg-agent-media/scripts/media_client.py"
        self.save()

    def invoke(self, *arguments, expected=(0,), connect=False):
        script = self.root / "ffmpeg-agent-connect/scripts/media_client.py" if connect else self.script
        result = subprocess.run([self.python, "-S", "-B", str(script), "--env-file", str(self.env),
                                 *map(str, arguments)], cwd=self.root, env=self.environment,
                                capture_output=True, text=True, timeout=200)
        text = result.stdout + result.stderr
        assert not any(secret and secret != "null" and secret in text for secret in self.secrets), (
            "CREDENTIAL_IN_CONSUMER_OUTPUT")
        self.outputs.append(text)
        assert not result.stderr, "UNEXPECTED_CONSUMER_STDERR"
        value = json.loads(result.stdout)
        if result.returncode not in expected:
            raise AssertionError("CONSUMER_EXIT_" + str(result.returncode) + "_" + str(value.get("code")))
        return value, result.returncode

    def state(self, name):
        return self.root / (name + ".json")

    def run(self, name, fixture, *, operation=None, params=None, transport="http", intent=None,
            expected=(0,), wait=120):
        arguments = ["run", "--input", "source=" + str(self.root / "fixtures" / fixture),
                     "--state", self.state(name), "--output-dir", self.root / (name + "-output"),
                     "--transport", transport, "--wait-seconds", wait]
        if intent:
            intent_file = self.root / (name + "-intent.txt")
            intent_file.write_text(intent)
            arguments += ["--intent-file", intent_file]
        else:
            arguments += ["--operation", operation]
            if params is not None:
                arguments += ["--params", json.dumps(params)]
        return self.invoke(*arguments, expected=expected)

    def verify_files(self, value):
        count = 0
        for output in value.get("downloads", {}).values():
            for item in output.get("files", []):
                path = Path(item["path"])
                assert path.stat().st_size == item["size_bytes"] and digest(path) == item["sha256"]
                count += 1
        return count

    def probe(self, value):
        streams = []
        for output in value.get("downloads", {}).values():
            for item in output.get("files", []):
                result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json",
                                         item["path"]], capture_output=True, text=True, timeout=30)
                assert result.returncode == 0, "OUTPUT_MEDIA_UNREADABLE"
                for stream in json.loads(result.stdout)["streams"]:
                    streams.append({key: stream[key] for key in (
                        "codec_name", "codec_type", "width", "height", "sample_rate", "channels")
                        if key in stream})
        return streams

    def case(self, name, action):
        existing = next((case for case in self.cases if case["name"] == name), None)
        if self.selected and name not in self.selected:
            return
        if self.resume and not self.selected and existing and existing["passed"]:
            return
        started = time.monotonic()
        try:
            details = action()
            result = {"name": name, "passed": True, **(details or {})}
        except (AssertionError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            # 错误正文可能包含请求或路径，报告只记录异常类别。
            result = {"name": name, "passed": False, "error_type": type(error).__name__}
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["skill_manifest_sha256"] = self.manifests["ffmpeg-agent-media"]
        self.cases = [case for case in self.cases if case["name"] != name] + [result]
        self.save()
        print(json.dumps(result, ensure_ascii=False), flush=True)

    def save(self):
        report = {"schema_version": 1, "scope": "independent-consumer-existing-service",
                  "consumer_site_packages": False, "server_source_imported_by_consumer": False,
                  "skill_manifest_sha256": self.manifests, "fixtures": self.fixtures,
                  "cases": self.cases, "credential_output_scan": "passed",
                  "invocation_elapsed_seconds": round(time.time() - self.started, 3),
                  "case_timing_note": "--resume reuses accepted tasks; case durations measure this CLI invocation, not model planning latency"}
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        self.private_record.parent.mkdir(parents=True, exist_ok=True)
        self.private_record.write_text(json.dumps({"root": str(self.root), "python": self.python,
            "outputs": self.outputs}, ensure_ascii=False, indent=2))
        os.chmod(self.private_record, 0o600)

    def operation(self, name, fixture, operation, params, protocol, expect_stream):
        value, _ = self.run(name, fixture, operation=operation, params=params, transport=protocol)
        assert value["status"] == "succeeded" and value["inputs_released"] and value["uploads_released"]
        count = self.verify_files(value)
        streams = self.probe(value)
        assert any(all(stream.get(key) == expected for key, expected in expect_stream.items())
                   for stream in streams)
        return {"protocol": protocol, "status": value["status"], "verified_files": count,
                "media": streams}

    def all(self):
        self.case("connect_http_mcp_a2a", lambda: {
            "checks": self.invoke("check", connect=True)[0]["checks"]})
        image = "image_jpeg_portrait_odd.jpg"
        audio = "audio_mp3_mono_22050.mp3"
        video = "video_h264_640x360.mkv"
        square = "video_h264_aac_square.mp4"

        def probe_case():
            value, _ = self.run("probe", audio, operation="media.probe.v1")
            assert self.verify_files(value) == 1
            content = json.loads(Path(value["downloads"]["result"]["files"][0]["path"]).read_text())
            assert content["streams"][0]["codec"] == "mp3"
            return {"status": value["status"], "verified_files": 1, "default_params_omitted": True}
        self.case("http_probe_defaults", probe_case)
        self.case("http_image", lambda: self.operation("image", image, "image.resize.v1",
            {"format": "png", "width": 80}, "http", {"codec_name": "png", "width": 80, "height": 107}))
        self.case("mcp_audio", lambda: self.operation("audio", audio, "audio.transcode.v1",
            {"codec": "flac", "sample_rate": 48000}, "mcp",
            {"codec_name": "flac", "sample_rate": "48000", "channels": 1}))
        self.case("http_video", lambda: self.operation("video", video, "video.transcode.v1",
            {"resize": {"width": 160}}, "http", {"codec_name": "h264", "width": 160, "height": 90}))
        self.case("mcp_extract_audio", lambda: self.operation("extract-audio", square,
            "video.extract_audio.v1", {"codec": "mp3", "range": {"end_ms": 1000}}, "mcp",
            {"codec_name": "mp3"}))
        self.case("http_frames", lambda: self.operation("frames", video, "video.extract_frames.v1",
            {"sampling": {"mode": "timestamps", "timestamps_ms": [0, 500]},
             "image": {"format": "png", "width": 80}}, "http",
            {"codec_name": "png", "width": 80, "height": 45}))

        def partial():
            value, code = self.run("partial", video, operation="video.extract_frames.v1",
                params={"sampling": {"mode": "timestamps", "timestamps_ms": [0, 999999]}},
                transport="mcp", expected=(2,))
            assert code == 2 and value["status"] == "partial" and self.verify_files(value) == 1
            assert value["downloads"]["result"]["failures"][0]["code"] == "TIMESTAMP_OUT_OF_RANGE"
            return {"status": value["status"], "exit_code": code, "verified_files": 1,
                    "failure_code": "TIMESTAMP_OUT_OF_RANGE"}
        self.case("partial_collection", partial)

        def resume():
            value, _ = self.run("resume", square, operation="video.transcode.v1", wait=0,
                                 expected=(0, 3))
            accepted = value["job_id"]
            value, _ = self.invoke("resume", "--state", self.state("resume"), "--wait-seconds", 120)
            assert value["job_id"] == accepted and self.verify_files(value) == 1
            replay, _ = self.invoke("resume", "--state", self.state("resume"), "--wait-seconds", 120)
            assert replay["job_id"] == accepted and replay["downloads"]["result"]["files"][0]["reused"]
            return {"same_job": True, "verified_files": 1, "existing_verified_file_reused": True}
        self.case("wait_resume_idempotent_collect", resume)

        def cancel():
            first, _ = self.run("cancel", square, operation="video.transcode.v1", wait=0,
                                expected=(0, 3))
            value, _ = self.invoke("cancel", "--state", self.state("cancel"), "--wait-seconds", 120)
            assert value["job_id"] == first["job_id"] and value["terminal"] and value["inputs_released"]
            self.invoke("release", "--state", self.state("cancel"))
            return {"status": value["status"], "actual_stop_confirmed": True}
        self.case("cancel_wait_release", cancel)

        def failed():
            value, code = self.run("failed-image", image, operation="video.extract_audio.v1", expected=(1,))
            assert value["status"] == "failed" and value["inputs_released"] and not value["ok"]
            self.invoke("release", "--state", self.state("failed-image"))
            return {"status": value["status"], "exit_code": code,
                    "code": value.get("code") or value["outputs"]["result"].get("code")}
        self.case("failed_media_release", failed)

        def native_collect():
            value, _ = self.invoke("upload", "--input", "source=" + str(self.root / "fixtures" / audio),
                                   "--state", self.state("native"))
            helper = self.root / "native-submit.py"
            helper.write_text("import json,sys\nfrom pathlib import Path\n"
                "sys.path.insert(0,str(Path(__file__).parent/'ffmpeg-agent-media/scripts'))\n"
                "from ffmpeg_agent_client.transport import Client\n"
                "from ffmpeg_agent_client.protocols import MCP\n"
                "root=Path(__file__).parent\nstate=json.loads((root/'native.json').read_text())\n"
                "definition={'inputs':{'source':{'kind':'uploaded_file','file_id':state['inputs']['source']['file_id']}},"
                "'steps':[{'id':'probe','operation':'media.probe.v1','inputs':{'media':{'source':'source'}}}],"
                "'outputs':{'report':{'step':'probe','output':'report'}}}\n"
                "with MCP(Client.with_env(root/'consumer.env')) as mcp:\n"
                " result=mcp.call('submit_job',{'definition':definition,'idempotency_key':state['request_id']})\n"
                " print(json.dumps({'job_id':result['job_id']}))\n")
            submitted = subprocess.run([self.python, "-S", "-B", str(helper)], cwd=self.root,
                env=self.environment, capture_output=True, text=True, timeout=30)
            assert submitted.returncode == 0 and not submitted.stderr
            assert not any(secret and secret in submitted.stdout for secret in self.secrets)
            job_id = json.loads(submitted.stdout)["job_id"]
            collected, _ = self.invoke("collect", "--state", self.state("native"), "--job-id", job_id,
                                       "--output-dir", self.root / "native-output")
            assert self.verify_files(collected) == 1
            return {"upload_file_refs": len(value["inputs"]), "mcp_submit_outside_cli_run": True,
                    "collect_status": collected["status"], "verified_files": 1}
        self.case("mcp_upload_external_submit_collect", native_collect)

        def definition():
            definition_file = self.root / "two-input-definition.json"
            definition_file.write_text(json.dumps({"steps": [
                {"id": "audio_probe", "operation": "media.probe.v1",
                 "inputs": {"media": {"source": "audio"}}},
                {"id": "image_resize", "operation": "image.resize.v1",
                 "inputs": {"image": {"source": "image"}}, "params": {"format": "png", "width": 80}},
            ], "outputs": {"report": {"step": "audio_probe", "output": "report"},
                           "picture": {"step": "image_resize", "output": "image"}}}))
            value, _ = self.invoke("run", "--input", "audio=" + str(self.root / "fixtures" / audio),
                "--input", "image=" + str(self.root / "fixtures" / image), "--definition-file",
                definition_file, "--transport", "mcp", "--state", self.state("definition"),
                "--output-dir", self.root / "definition-output")
            assert value["status"] == "succeeded" and set(value["downloads"]) == {"report", "picture"}
            assert self.verify_files(value) == 2
            return {"protocol": "mcp", "status": value["status"], "inputs": 2, "verified_files": 2}
        self.case("two_input_definition", definition)

        def needs_input():
            value, code = self.run("needs-input", audio, transport="http", expected=(1,),
                intent="请把 source 音频转换成另一种格式，具体目标格式还没决定，请先问我。")
            assert value["status"] == "needs_input" and value.get("message") and not value.get("job_id")
            assert value["terminal"] and not value["ok"]
            self.invoke("release", "--state", self.state("needs-input"))
            return {"protocol": "http", "status": value["status"], "exit_code": code,
                    "bounded_message_present": True, "media_job_created": False}
        self.case("natural_needs_input", needs_input)

        def natural(name, fixture, transport, intent, expect):
            value, _ = self.run(name, fixture, transport=transport, intent=intent)
            assert value["status"] == "succeeded" and self.verify_files(value) == 1
            streams = self.probe(value)
            assert any(all(stream.get(key) == expected for key, expected in expect.items())
                       for stream in streams)
            return {"protocol": transport, "status": value["status"], "verified_files": 1,
                    "real_model": True, "media": streams}
        self.case("natural_http_image", lambda: natural("natural-http", image, "http",
            "将 source 图片转为 PNG，宽80像素，保持比例，不放大，只返回一张图片。",
            {"codec_name": "png", "width": 80, "height": 107}))
        self.case("natural_mcp_audio", lambda: natural("natural-mcp", audio, "mcp",
            "将 source 音频转为 FLAC，采样率48000Hz，保持源声道，只返回音频。",
            {"codec_name": "flac", "sample_rate": "48000", "channels": 1}))
        for item in self.fixtures:
            assert digest(self.root / "fixtures" / item["name"]) == item["sha256"]
        assert set(read_env_file(self.env)) == {"MEDIA_PUBLIC_BASE_URL", "MEDIA_SERVICE_TOKEN"}
        self.save()
        return all(case["passed"] for case in self.cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--private-record", type=Path, required=True)
    parser.add_argument("--resume", action="store_true", help="复用原私有记录，仅补验失败项")
    parser.add_argument("--case", action="append", help="只验证指定命名场景，可重复")
    args = parser.parse_args()
    os.umask(0o077)
    return 0 if Validation(args).all() else 1


if __name__ == "__main__":
    raise SystemExit(main())
