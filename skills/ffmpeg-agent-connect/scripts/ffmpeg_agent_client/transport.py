import hashlib
import http.client
import json
import math
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import Connection, load_connection
from .errors import ConsumerError, service_code

JSON_LIMIT = 8 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024


def _signature(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def json_bytes(value: object) -> bytes:
    try:
        result = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ConsumerError("INVALID_JSON_REQUEST") from None
    if len(result) > JSON_LIMIT:
        raise ConsumerError("REQUEST_TOO_LARGE")
    return result


def read_json(response: http.client.HTTPResponse) -> dict:
    raw = response.read(JSON_LIMIT + 1)
    if len(raw) > JSON_LIMIT:
        raise ConsumerError("RESPONSE_TOO_LARGE")
    if not raw and response.status in {202, 204}:
        return {}
    if response.headers.get_content_type() != "application/json":
        raise ConsumerError("INVALID_JSON_RESPONSE")

    def object_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    def constant(_):
        raise ValueError

    try:
        value = json.loads(raw, object_pairs_hook=object_pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ConsumerError("INVALID_JSON_RESPONSE") from None
    if not isinstance(value, dict):
        raise ConsumerError("INVALID_JSON_RESPONSE")
    return value


class Client:
    def __init__(self, connection: Connection, timeout: float = 30):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ConsumerError("INVALID_TIMEOUT")
        self.base = connection.base
        self.authenticated = connection.token is not None
        self._token = connection.token
        self._url = urlsplit(self.base)
        self.timeout = timeout

    @classmethod
    def with_env(cls, path: Path, timeout: float = 30):
        return cls(load_connection(path), timeout)

    def safe_message(self, value: object) -> str | None:
        if not isinstance(value, str) or len(value) > 1000:
            return None
        if self._token is not None:
            value = value.replace(self._token, "[redacted]")
        value = "".join(char for char in value if char == "\n" or char.isprintable())
        return value if value.strip() else None

    @contextmanager
    def raw_request(self, method: str, path: str, body=None, headers: dict | None = None):
        if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
                or not path.isascii() or "#" in path or "\\" in path
                or any(ord(char) <= 32 or ord(char) == 127 for char in path)
                or any(part in {".", ".."} for part in unquote(path.split("?", 1)[0]).split("/"))
                or "\\" in unquote(path.split("?", 1)[0])):
            raise ConsumerError("INVALID_SERVICE_PATH")
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            raise ConsumerError("INVALID_HTTP_METHOD")
        outgoing = {"Accept-Encoding": "identity"}
        for key, value in (headers or {}).items():
            if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9-]+", key)
                    or not isinstance(value, str) or not value.isascii()
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)
                    or key.lower() in {"authorization", "host", "connection", "transfer-encoding",
                                       "accept-encoding"}):
                raise ConsumerError("INVALID_HTTP_HEADER")
            outgoing[key] = value
        if self._token is not None:
            outgoing["Authorization"] = "Bearer " + self._token
        factory = (http.client.HTTPSConnection if self._url.scheme == "https"
                   else http.client.HTTPConnection)
        connection = factory(self._url.hostname, self._url.port, timeout=self.timeout)
        try:
            # http.client 不使用代理环境变量或重定向；认证只发送到显式配置的服务。
            connection.request(method, self._url.path + path, body=body, headers=outgoing)
            response = connection.getresponse()
            if 300 <= response.status <= 399:
                raise ConsumerError("REDIRECT_REFUSED", response.status)
            if not 200 <= response.status <= 299:
                code = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND"}.get(
                    response.status, "HTTP_ERROR")
                try:
                    error = json.loads(response.read(65537))
                    if isinstance(error, dict) and isinstance(error.get("error"), dict):
                        code = service_code(error["error"].get("code"), code)
                except (ValueError, UnicodeError, RecursionError):
                    pass
                raise ConsumerError(code, response.status)
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ConsumerError("UNSUPPORTED_CONTENT_ENCODING")
            yield response
        except ConsumerError:
            raise
        except (TimeoutError, OSError, http.client.HTTPException):
            raise ConsumerError("CONNECTION_FAILED") from None
        except (ValueError, UnicodeError):
            raise ConsumerError("INVALID_HTTP_RESPONSE") from None
        finally:
            connection.close()

    def api(self, method: str, path: str, body=None, headers: dict | None = None) -> dict:
        data = json_bytes(body) if body is not None else None
        extra = {"Content-Type": "application/json"} if data is not None else {}
        with self.raw_request(method, path, data, {**extra, **(headers or {})}) as response:
            return read_json(response)

    def upload(self, source: Path, idempotency_key: str,
               expected_sha256: str | None = None) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", idempotency_key):
            raise ConsumerError("INVALID_IDEMPOTENCY_KEY")
        if (expected_sha256 is not None
                and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
            raise ConsumerError("INVALID_SHA256")
        try:
            if source.is_symlink():
                raise ConsumerError("INVALID_INPUT_FILE")
            descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ConsumerError("INVALID_INPUT_FILE")
                before = _signature(info)
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
                    digest.update(chunk)
                expected = digest.hexdigest()
                if (_signature(os.fstat(stream.fileno())) != before
                        or (expected_sha256 is not None and expected_sha256 != expected)):
                    raise ConsumerError("INPUT_CHANGED")
                stream.seek(0)
                sent_digest, sent_size = hashlib.sha256(), 0

                def chunks():
                    nonlocal sent_size
                    for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
                        sent_size += len(chunk)
                        if sent_size > info.st_size:
                            raise ConsumerError("INPUT_CHANGED")
                        sent_digest.update(chunk)
                        yield chunk
                    if sent_size != info.st_size or sent_digest.hexdigest() != expected:
                        raise ConsumerError("INPUT_CHANGED")

                with self.raw_request("POST", "/v1/files", chunks(), {
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(info.st_size), "X-File-Size": str(info.st_size),
                    "X-File-Sha256": expected, "Idempotency-Key": idempotency_key,
                }) as response:
                    result = read_json(response)
                if (sent_size != info.st_size or sent_digest.hexdigest() != expected
                        or _signature(os.fstat(stream.fileno())) != before
                        or _signature(source.stat(follow_symlinks=False)) != before):
                    raise ConsumerError("INPUT_CHANGED")
        except ConsumerError:
            raise
        except (OSError, ValueError):
            raise ConsumerError("INPUT_UNREADABLE") from None
        if (not isinstance(result.get("file_id"), str)
                or not re.fullmatch(r"file_[a-f0-9]{32}", result["file_id"])
                or result.get("state") != "ready"
                or type(result.get("size_bytes")) is not int
                or result["size_bytes"] != info.st_size or result.get("sha256") != expected):
            raise ConsumerError("INVALID_UPLOAD_RESPONSE")
        return result
