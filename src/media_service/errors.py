from uuid import uuid4


class ServiceError(Exception):
    def __init__(
        self, code: str, status: int = 422, retryable: bool = False, details: dict | None = None
    ):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.details = details

    def public(self, request_id: str | None = None) -> dict:
        return {
            "code": self.code,
            "message": MESSAGES.get(self.code, "请求无法完成"),
            "retryable": self.retryable,
            "request_id": request_id or uuid4().hex,
            "details": self.details,
        }


MESSAGES = {
    "UNAUTHORIZED": "需要有效的调用凭据",
    "FORBIDDEN": "当前身份无权执行此操作",
    "NOT_FOUND": "资源不存在",
    "ARTIFACT_GONE": "产物已结束保留",
    "FILE_GONE": "上传文件已结束保留",
    "UPLOAD_TIMEOUT": "上传超过允许时限",
    "INVALID_REQUEST": "请求格式不符合接口约定",
    "UNSUPPORTED_OPERATION": "不支持该操作或参数",
    "INVALID_GRAPH": "步骤依赖或端口不符合约定",
    "IDEMPOTENCY_CONFLICT": "幂等键已用于不同请求",
    "QUOTA_EXCEEDED": "已达到调用方或队列配额",
    "DISK_FULL": "服务可用存储不足",
    "SOURCE_UNREACHABLE": "无法获取输入文件",
    "SOURCE_FORBIDDEN": "来源不符合访问策略",
    "INPUT_CHANGED": "输入完整性校验失败",
    "INPUT_TOO_LARGE": "输入超过部署上限",
    "INVALID_MEDIA": "无法解析媒体或媒体已损坏",
    "UNSUPPORTED_MEDIA": "媒体类型或特性不受支持",
    "NO_AUDIO_TRACK": "不存在所选音轨",
    "TIMESTAMP_OUT_OF_RANGE": "请求时间超出可处理的媒体范围",
    "INVALID_TIMELINE": "媒体时间戳缺失或非单调",
    "TASK_TIMEOUT": "已超过任务总时限",
    "EXECUTION_INTERRUPTED": "执行已中断且无法安全恢复",
    "ENGINE_INCOMPATIBLE": "执行环境与已接受任务不兼容",
    "DEPENDENCY_FAILED": "上游步骤未完整成功",
    "CANCELED": "任务已取消",
    "OUTPUT_LIMIT": "产物或工作文件超过部署上限",
    "STATE_UNAVAILABLE": "持久状态暂不可用",
    "STATE_CONFLICT": "资源状态不允许此操作",
    "INVALID_RANGE": "文件范围无效",
    "REQUEST_TOO_LARGE": "请求体超过部署上限",
}
