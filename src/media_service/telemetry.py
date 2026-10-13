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
})


def event(name: str, **values):
    # 仅接收已知语义字段；异常正文、请求体、URL、认证头及文件路径不进入日志。
    record = {"event": name, "timestamp": round(time.time(), 3)}
    record.update({key: value for key, value in values.items() if key in FIELDS})
    logger.info(json.dumps(record, separators=(",", ":"), ensure_ascii=False))


def configure():
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
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
