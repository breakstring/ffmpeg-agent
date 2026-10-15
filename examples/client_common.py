"""完整调用示例共用的 HTTP 文件传输与参数解析；只依赖 Python 标准库。"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

TERMINAL = {"succeeded", "partial", "failed", "canceled"}
OPERATIONS = {
    "probe_media": ("media.probe.v1", "media", "report"),
    "transcode_video": ("video.transcode.v1", "video", "video"),
    "transcode_audio": ("audio.transcode.v1", "audio", "audio"),
    "extract_audio": ("video.extract_audio.v1", "video", "audio"),
    "extract_frames": ("video.extract_frames.v1", "video", "frames"),
    "resize_image": ("image.resize.v1", "image", "image"),
}
ROOT = Path(__file__).resolve().parents[1]


class ExampleError(Exception):
    pass


def parse_args(description: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--config", type=Path, default=ROOT / "deploy/local/media-config.json")
    parser.add_argument("--base-url", help="Override the service URL; never include a token")
    parser.add_argument("--operation", choices=OPERATIONS, default="transcode_video")
    parser.add_argument("--params", type=Path, help="Optional JSON object of media parameters")
    parser.add_argument("--request-id", default=uuid.uuid4().hex,
                        help="Reuse only for an identical request; defaults to a new run")
    parser.add_argument("--job-id", help="Resume an accepted job without uploading or submitting")
    parser.add_argument("--wait-seconds", type=float, default=600)
    parser.add_argument("--upload-first", action="store_true",
                        help="HTTP example: upload a reusable file_id before submitting")
    return parser.parse_args()


def prepare(args: argparse.Namespace) -> dict:
    if args.destination.exists() or args.destination.is_symlink():
        raise ExampleError("Destination already exists; choose a new file or directory")
    if not args.destination.parent.is_dir():
        raise ExampleError("Destination parent directory does not exist")
    if not math.isfinite(args.wait_seconds) or args.wait_seconds <= 0:
        raise ExampleError("--wait-seconds must be positive and finite")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.request_id):
        raise ExampleError("--request-id must contain 1-80 letters, digits, underscores or hyphens")
    if args.job_id:
        if not re.fullmatch(r"job_[a-f0-9]{32}", args.job_id):
            raise ExampleError("Invalid --job-id")
        return {}
    if not args.source.is_file():
        raise ExampleError("Source must be a readable regular file")
    params = json.loads(args.params.read_text()) if args.params else {}
    if not isinstance(params, dict):
        raise ExampleError("--params must contain a JSON object")
    return params


def definition(operation: str, source: dict, params: dict) -> dict:
    name, port, output = OPERATIONS[operation]
    step = {"id": "process", "operation": name, "inputs": {port: {"source": "source"}}}
    if params:
        step["params"] = params
    return {"inputs": {"source": source}, "steps": [step],
            "outputs": {"result": {"step": "process", "output": output}}}


def print_job(job: dict) -> None:
    print("job_id:", job["job_id"], flush=True)


def check_job(job: dict) -> str:
    if job["status"] not in {"succeeded", "partial"}:
        code = (job.get("error") or {}).get("code", "TASK_NOT_SUCCEEDED")
        raise ExampleError("Task did not succeed: " + safe_code(code))
    output = job.get("outputs", {}).get("result", {})
    artifact_id = output.get("artifact_id", "")
    if not re.fullmatch(r"art_[a-f0-9]{32}", artifact_id):
        raise ExampleError("Task has no downloadable result")
    return artifact_id


def safe_code(value: object) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z_]{1,80}", value) else "REQUEST_FAILED"


class Client:
    def __init__(self, config: Path, base_url: str | None = None):
        settings = json.loads(config.read_text())
        self.base = (base_url or settings["public_base_url"]).rstrip("/")
        self.url = urlsplit(self.base)
        if (self.url.scheme not in {"http", "https"} or not self.url.hostname
                or self.url.username or self.url.password or self.url.query or self.url.fragment):
            raise ExampleError("Service URL must be HTTP(S) without credentials, query or fragment")
        token = settings["service_token"]
        if token is not None and (not isinstance(token, str) or not token or "\n" in token
                                  or "\r" in token):
            raise ExampleError("service_token must be a nonempty string or null")
        self.headers = {"Authorization": "Bearer " + token} if token else {}

    @contextmanager
    def response(self, method: str, path: str, body=None, headers=None):
        connection_type = (http.client.HTTPSConnection if self.url.scheme == "https"
                           else http.client.HTTPConnection)
        connection = connection_type(self.url.hostname, self.url.port, timeout=300)
        try:
            # 固定服务地址且不跟随重定向，认证头不会被传给下载链接指向的其他主机。
            connection.request(method, self.url.path.rstrip("/") + path, body=body,
                               headers={**self.headers, **(headers or {})})
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                code = "REQUEST_FAILED"
                try:
                    code = safe_code(json.loads(response.read(65536)).get("error", {}).get("code"))
                except (ValueError, AttributeError):
                    pass
                raise ExampleError(f"HTTP {response.status}: {code}")
            yield response
        finally:
            connection.close()

    def api(self, method: str, path: str, body=None, headers=None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        extra = {"Content-Type": "application/json"} if data is not None else {}
        with self.response(method, path, data, {**extra, **(headers or {})}) as response:
            return json.load(response)

    def upload(self, source: Path, request_id: str) -> dict:
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            stream.seek(0)
            with self.response("POST", "/v1/files", stream, {
                "Content-Type": "application/octet-stream",
                "Content-Length": str(os.fstat(stream.fileno()).st_size),
                "X-File-Sha256": digest.hexdigest(),
                "Idempotency-Key": "example-file-" + request_id,
            }) as response:
                return json.load(response)

    def submit_multipart(self, source: Path, request_id: str, job: dict) -> dict:
        boundary = uuid.uuid4().hex
        prefix = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"job\"\r\n"
                  "Content-Type: application/json\r\n\r\n").encode() + json.dumps(job).encode()
        prefix += (f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"source\"; "
                   "filename=\"input.bin\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
        suffix = f"\r\n--{boundary}--\r\n".encode()
        with source.open("rb") as stream:
            def chunks():
                yield prefix
                yield from iter(lambda: stream.read(1024 * 1024), b"")
                yield suffix

            with self.response("POST", "/v1/jobs/upload", chunks(), {
                "Content-Type": "multipart/form-data; boundary=" + boundary,
                "Content-Length": str(len(prefix) + os.fstat(stream.fileno()).st_size + len(suffix)),
                "Idempotency-Key": "example-job-" + request_id,
            }) as response:
                return json.load(response)

    def download(self, path: str, destination: Path, size_bytes: int, sha256: str) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".media-",
                                             delete=False) as output:
                temporary = Path(output.name)
                digest, size = hashlib.sha256(), 0
                with self.response("GET", path) as response:
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    if int(response.headers.get("Content-Length", -1)) != size:
                        raise ExampleError("Incomplete HTTP download")
                if size != size_bytes or digest.hexdigest() != sha256:
                    raise ExampleError("Downloaded size or SHA-256 does not match")
            # 同目录链接保证校验成功后才发布，且永远不覆盖调用方已有文件。
            os.link(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def save_artifact(self, artifact: dict, destination: Path) -> None:
        artifact_id = artifact["artifact_id"]
        if not re.fullmatch(r"art_[a-f0-9]{32}", artifact_id):
            raise ExampleError("Invalid artifact ID")
        path = "/v1/artifacts/" + artifact_id
        if artifact["kind"] == "file":
            self.download(path + "/content", destination, artifact["size_bytes"], artifact["sha256"])
            return
        items, cursor = [], 0
        while True:
            page = self.api("GET", path + f"/members?cursor={cursor}&limit=100")
            items.extend(page["items"])
            following = page["next_cursor"]
            if following is None:
                break
            if type(following) is not int or following <= cursor:
                raise ExampleError("Invalid collection pagination")
            cursor = following
        destination.mkdir()
        try:
            for item in items:
                if item.get("error"):
                    continue
                key = item["member_key"]
                # 对服务返回的成员名仍作本地路径校验。
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", key) or key in {".", "..", "manifest.json"}:
                    raise ExampleError("Invalid collection member name")
                self.download(path + "/files/" + quote(key, safe=""), destination / key,
                              item["size_bytes"], item["sha256"])
            (destination / "manifest.json").write_text(json.dumps({"items": items}, indent=2) + "\n")
        except BaseException:
            # 此目录由本次调用独占创建；下载失败时只清理它。
            shutil.rmtree(destination)
            raise
