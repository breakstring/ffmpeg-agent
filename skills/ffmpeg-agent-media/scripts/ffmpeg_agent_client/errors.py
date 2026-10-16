import re


class ConsumerError(Exception):
    def __init__(self, code: str, status: int | None = None):
        self.code = code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) else "CLIENT_ERROR"
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        super().__init__(self.code)


_SERVICE_CODES = frozenset({
    "AGENT_DISABLED", "PLANNING_FAILED", "PLANNING_INVALID_OUTPUT", "PLANNING_TIMEOUT",
    "PLANNING_INTERRUPTED", "PLANNING_CLOSED", "UNAUTHORIZED", "FORBIDDEN", "NOT_FOUND",
    "ARTIFACT_GONE", "FILE_GONE", "UPLOAD_TIMEOUT", "INVALID_REQUEST", "UNSUPPORTED_OPERATION",
    "INVALID_GRAPH", "IDEMPOTENCY_CONFLICT", "QUOTA_EXCEEDED", "DISK_FULL", "SOURCE_UNREACHABLE",
    "SOURCE_FORBIDDEN", "INPUT_CHANGED", "INPUT_TOO_LARGE", "INVALID_MEDIA", "UNSUPPORTED_MEDIA",
    "NO_AUDIO_TRACK", "TIMESTAMP_OUT_OF_RANGE", "INVALID_TIMELINE", "TASK_TIMEOUT",
    "EXECUTION_INTERRUPTED", "ENGINE_INCOMPATIBLE", "DEPENDENCY_FAILED", "CANCELED",
    "OUTPUT_LIMIT", "STATE_UNAVAILABLE", "STATE_CONFLICT", "INVALID_RANGE", "REQUEST_TOO_LARGE",
})


def service_code(value: object, fallback: str = "REMOTE_ERROR") -> str:
    # 正则不足以脱敏：外部服务器可能把凭据放进形似错误码的字符串。
    return value if isinstance(value, str) and value in _SERVICE_CODES else fallback
