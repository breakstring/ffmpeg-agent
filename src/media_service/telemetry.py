import json
import logging
import time
from copy import deepcopy

from uvicorn.config import LOGGING_CONFIG

logger = logging.getLogger("media_service.events")
FIELDS = frozenset({
    "request_id", "job_id", "step_id", "attempt_id", "artifact_id", "operation",
    "status", "code", "duration_ms", "queue_ms", "size_bytes", "active_jobs",
    "active_processes", "created", "method", "route",
    "agent_task_id", "phase", "model_phase", "model_request", "remaining_ms",
    "decision", "http_status", "error_type",
    "content", "content_bytes", "content_truncated", "content_format",
})


def _event_level(name: str, values: dict) -> int:
    status = values.get("status")
    if isinstance(status, int) and status >= 400:
        return logging.ERROR if status >= 500 else logging.WARNING
    if status in {"failed", "partial"} or name == "request_error":
        return logging.ERROR if values.get("code") == "STATE_UNAVAILABLE" else logging.WARNING
    if name == "request_finished":
        return logging.DEBUG
    if name == "mcp_tool_finished" and values.get("operation") in {
        "get_job", "get_artifact", "get_agent_task", "get_file", "list_capabilities",
        "list_operations", "list_presets",
    }:
        return logging.DEBUG
    return logging.INFO


def event(name: str, *, level: int | None = None, **values):
    # 普通事件仅接收语义字段；模型正文只能由专用脱敏器在 DEBUG 下提供。
    level = _event_level(name, values) if level is None else level
    if not logger.isEnabledFor(level):
        return
    record = {"event": name, "timestamp": round(time.time(), 3),
              "level": logging.getLevelName(level)}
    content_allowed = level == logging.DEBUG and name in {
        "planning_model_request_content", "planning_model_response_content",
    }
    record.update({key: value for key, value in values.items() if key in FIELDS
                   and (not key.startswith("content") or content_allowed)})
    logger.log(level, json.dumps(record, separators=(",", ":"), ensure_ascii=False))


def configure(level: str = "INFO"):
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers = [handler]
    logger.setLevel(level)
    logger.propagate = False


class SafeExceptionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            record.msg = '{"event":"runtime_error","code":"STATE_UNAVAILABLE"}'
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def server_logging() -> dict:
    config = deepcopy(LOGGING_CONFIG)
    config["filters"] = {"safe_exception": {"()": SafeExceptionFilter}}
    config["handlers"]["default"]["filters"] = ["safe_exception"]
    return config
