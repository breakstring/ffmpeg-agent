import json
import logging
import re
from typing import Literal

from media_service.telemetry import event, logger

MAX_CONTENT_BYTES = 128 * 1024
_PRIVATE_KEYS = re.compile(
    r"authorization|cookie|password|passwd|secret|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|service[_-]?token|signature|^token$|(?:^|[_-])(?:path|filename)$",
    re.IGNORECASE,
)
_PRIVATE_ASSIGNMENT = re.compile(
    r"\b(?:authorization|cookie|set[_-]?cookie|password|passwd|secret|api[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|service[_-]?token|signature|token)"
    r"[\"'\\]*\s*[:=]", re.IGNORECASE,
)
_URL = re.compile(r"(?:https?|file)://[^\s\"'<>\\]+", re.IGNORECASE)
_PATH = re.compile(r"(?<![\w:/])/(?!/)[^\s\"'<>\\]+|[A-Za-z]:\\[^\s\"'<>]+")
_CREDENTIAL = re.compile(
    r"\bBearer\s+[^\s\"',;]+|\bsk-[A-Za-z0-9_-]+|"
    r"\b(?:api[_-]?key|access[_-]?token|password|secret)[\"']?\s*[:=]\s*[\"']?[^\s\"',;}]+",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _redact_text(value: str, secrets: tuple[str, ...]) -> str:
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
        escaped = secret
        for _ in range(3):
            escaped = json.dumps(escaped, ensure_ascii=True)[1:-1]
            value = value.replace(escaped, "[REDACTED]")
    for pattern in (_URL, _PATH, _EMAIL):
        value = pattern.sub("[REDACTED]", value)
    # 非 JSON 或代码围栏无法可靠划分敏感值边界，整段省略而不猜测值的终点。
    if _PRIVATE_ASSIGNMENT.search(value):
        return "[REDACTED_SENSITIVE_TEXT]"
    return _CREDENTIAL.sub("[REDACTED]", value)


def _redact(value: object, secrets: tuple[str, ...], depth: int = 0) -> object:
    if depth > 30:
        return "[DEPTH_LIMIT]"
    if isinstance(value, dict):
        return {
            _redact_text(str(key), secrets): (
                "[REDACTED]" if _PRIVATE_KEYS.search(str(key))
                else _redact(item, secrets, depth + 1)
            ) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, secrets, depth + 1) for item in value]
    if isinstance(value, str):
        # OpenAI message.content 常把计划 JSON 再编码为字符串。
        if value.lstrip().startswith(("{", "[")):
            try:
                nested = json.loads(value)
            except (ValueError, RecursionError):
                pass
            else:
                return json.dumps(_redact(nested, secrets, depth + 1), ensure_ascii=False)
        return _redact_text(value, secrets)
    return value


def log_model_content(
    direction: Literal["request", "response"], content: bytes, *,
    secrets: tuple[str, ...], agent_task_id: str | None, model_request: int,
    http_status: int | None = None,
) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return
    if direction not in {"request", "response"}:
        raise ValueError("unknown model content direction")
    truncated = len(content) > MAX_CONTENT_BYTES
    text = content[:MAX_CONTENT_BYTES].decode("utf-8", errors="replace")
    content_format = "text"
    if not truncated:
        try:
            parsed = json.loads(text)
        except (ValueError, RecursionError):
            text = _redact_text(text, secrets)
        else:
            text = json.dumps(_redact(parsed, secrets), ensure_ascii=False, separators=(",", ":"))
            content_format = "json"
    else:
        # 截断会破坏 JSON 与凭据转义边界，超限正文只保留大小，禁止输出不完整片段。
        text = "[REDACTED_OVERSIZED_CONTENT]"
    encoded = text.encode("utf-8", errors="replace")
    truncated |= len(encoded) > MAX_CONTENT_BYTES
    text = encoded[:MAX_CONTENT_BYTES].decode("utf-8", errors="ignore")
    event(f"planning_model_{direction}_content", level=logging.DEBUG,
          agent_task_id=agent_task_id, model_request=model_request,
          http_status=http_status, content=text, content_bytes=len(content),
          content_truncated=truncated, content_format=content_format)
