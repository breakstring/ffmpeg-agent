import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Name = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SharedFile(StrictModel):
    kind: Literal["shared_file"]
    storage_id: Name
    key: str = Field(min_length=1, max_length=1024)
    expected_size_bytes: int | None = Field(default=None, ge=0)
    expected_sha256: Sha256 | None = None

    @field_validator("key")
    @classmethod
    def safe_key(cls, value: str) -> str:
        if value.startswith("/") or "\\" in value or "\x00" in value:
            raise ValueError("unsafe key")
        if any(p in ("", ".", "..") for p in value.split("/")):
            raise ValueError("unsafe key")
        return value


class HttpFile(StrictModel):
    kind: Literal["http_file"]
    url: str = Field(min_length=1, max_length=8192)
    headers: dict[str, str] = Field(default_factory=dict, max_length=8)
    expires_at: str | None = None
    expected_size_bytes: int | None = Field(default=None, ge=0)
    expected_sha256: Sha256 | None = None

    @field_validator("expires_at")
    @classmethod
    def utc_expiry(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.endswith("Z") or "T" not in value:
            raise ValueError("expires_at must be UTC RFC3339")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("expires_at must be UTC RFC3339") from None
        if parsed.tzinfo is None:
            raise ValueError("expires_at requires timezone")
        return parsed.isoformat().replace("+00:00", "Z")

    @field_validator("headers")
    @classmethod
    def safe_headers(cls, value: dict[str, str]) -> dict[str, str]:
        result = {}
        for key, val in value.items():
            key = key.lower()
            if key not in {"authorization", "x-api-key"} or "\r" in val or "\n" in val:
                raise ValueError("unsupported source header")
            if len(val) > 4096 or key in result:
                raise ValueError("invalid source header")
            result[key] = val
        return result


class ArtifactInput(StrictModel):
    kind: Literal["artifact"]
    artifact_id: str = Field(pattern=r"^art_[a-f0-9]{32}$")
    member_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,128}$")


Input = Annotated[SharedFile | HttpFile | ArtifactInput, Field(discriminator="kind")]


class SourceBinding(StrictModel):
    source: Name


class StepBinding(StrictModel):
    step: Name
    output: Name


class ProbeParams(StrictModel):
    pass


class VideoParams(StrictModel):
    preset: Literal["web_720p_v1"] = "web_720p_v1"


class AudioParams(StrictModel):
    preset: Literal["aac_stereo_128k_v1", "wav_mono_16k_v1"] = "aac_stereo_128k_v1"
    audio_stream: int = Field(default=0, ge=0, le=15)


class FramesParams(StrictModel):
    timestamps_ms: list[Annotated[int, Field(ge=0, le=86400000)]] = Field(
        min_length=1, max_length=100
    )
    format: Literal["jpeg", "png"] = "jpeg"


class ImageParams(StrictModel):
    width: int = Field(ge=1, le=8192)
    height: int = Field(ge=1, le=8192)
    fit: Literal["contain", "cover"] = "contain"
    allow_upscale: bool = False
    format: Literal["jpeg", "png", "webp"] = "jpeg"
    background: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")


# 操作版本固定端口与参数，不接受引擎命令透传。
OPERATIONS: dict[str, tuple[str, str, type[StrictModel], str]] = {
    "media.probe.v1": ("media", "report", ProbeParams, "json"),
    "video.transcode.v1": ("video", "video", VideoParams, "video"),
    "audio.transcode.v1": ("audio", "audio", AudioParams, "audio"),
    "video.extract_audio.v1": ("video", "audio", AudioParams, "audio"),
    "video.extract_frames.v1": ("video", "frames", FramesParams, "collection"),
    "image.resize.v1": ("image", "image", ImageParams, "image"),
}


class Step(StrictModel):
    id: Name
    operation: str
    inputs: dict[Name, SourceBinding | StepBinding] = Field(min_length=1, max_length=1)
    params: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def operation_contract(self):
        if self.operation not in OPERATIONS:
            raise ValueError("unknown operation version")
        port, _, params, _ = OPERATIONS[self.operation]
        if set(self.inputs) != {port}:
            raise ValueError("invalid input port")
        self.params = params.model_validate(self.params).model_dump()
        return self


class OutputPolicy(StrictModel):
    storage_id: Name | None = None
    retention_seconds: int = Field(default=86400, ge=1, le=604800)


class JobCreate(StrictModel):
    inputs: dict[Name, Input] = Field(min_length=1, max_length=4)
    steps: list[Step] = Field(min_length=1, max_length=16)
    outputs: dict[Name, StepBinding] = Field(min_length=1, max_length=16)
    output_policy: OutputPolicy = Field(default_factory=OutputPolicy)

    @model_validator(mode="after")
    def graph(self):
        by_id = {s.id: s for s in self.steps}
        if len(by_id) != len(self.steps):
            raise ValueError("duplicate step id")
        dependencies: dict[str, set[str]] = {s.id: set() for s in self.steps}
        sources = set()

        def check(binding: StepBinding) -> str:
            step = by_id.get(binding.step)
            if step is None or binding.output != OPERATIONS[step.operation][1]:
                raise ValueError("unknown step output")
            return OPERATIONS[step.operation][3]

        for step in self.steps:
            for binding in step.inputs.values():
                if isinstance(binding, SourceBinding):
                    if binding.source not in self.inputs:
                        raise ValueError("unknown input")
                    sources.add(binding.source)
                else:
                    kind = check(binding)
                    required = OPERATIONS[step.operation][0]
                    if kind in {"collection", "json"} or required not in {"media", kind}:
                        raise ValueError("incompatible step input type")
                    dependencies[step.id].add(binding.step)
        if sources != set(self.inputs):
            raise ValueError("unused input")
        reachable: set[str] = set()

        def visit(sid: str, path: set[str]):
            if sid in path or len(path) >= 16:
                raise ValueError("dependency cycle or depth limit")
            for dep in dependencies[sid]:
                visit(dep, path | {sid})
            reachable.add(sid)

        for output in self.outputs.values():
            check(output)
            visit(output.step, set())
        if reachable != set(by_id):
            raise ValueError("unused step")
        return self

    def ordered_steps(self) -> list[Step]:
        pending = list(self.steps)
        ordered: list[Step] = []
        done: set[str] = set()
        while pending:
            step = next(
                s
                for s in pending
                if all(isinstance(b, SourceBinding) or b.step in done for b in s.inputs.values())
            )
            ordered.append(step)
            done.add(step.id)
            pending.remove(step)
        return ordered

    def canonical(self) -> str:
        return json.dumps(
            self.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()


class LeaseRequest(StrictModel):
    duration_seconds: int = Field(default=900, ge=1, le=3600)


class RetentionRequest(StrictModel):
    retention_seconds: int = Field(ge=1, le=604800)


class ErrorResult(StrictModel):
    code: str
    message: str
    retryable: bool
    request_id: str
    details: dict | None = None


class ErrorEnvelope(StrictModel):
    error: ErrorResult


class StepResult(StrictModel):
    id: str
    status: Literal["pending", "running", "succeeded", "partial", "failed", "skipped", "canceled"]
    artifact_id: str | None = None
    error: ErrorResult | None = None


class OutputResult(StrictModel):
    step: str
    output: str
    status: str
    artifact_id: str | None = None
    error: ErrorResult | None = None


class JobResult(StrictModel):
    job_id: str
    status: Literal["queued", "running", "canceling", "succeeded", "partial", "failed", "canceled"]
    phase: Literal["acquiring_inputs", "processing", "publishing"] | None = None
    progress: float | None = None
    inputs_released: bool
    created_at: str
    deadline_at: str
    idempotency_expires_at: str | None
    steps: list[StepResult]
    outputs: dict[str, OutputResult]
    error: ErrorResult | None = None
    execution: dict


class ArtifactResult(StrictModel):
    artifact_id: str
    job_id: str
    kind: Literal["file", "collection"]
    state: Literal["ready", "retiring"]
    media: dict
    size_bytes: int
    sha256: str
    retain_until: str
    published_at: str
    member_count: int
    item_count: int
    access: dict


class LeaseResult(StrictModel):
    lease_id: str
    artifact_id: str
    expires_at: str


class MemberResult(StrictModel):
    index: int
    member_key: str | None = None
    requested_time_ms: int
    actual_time_ms: int | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    error: ErrorResult | None = None


class MembersResult(StrictModel):
    items: list[MemberResult]
    next_cursor: int | None


def capabilities() -> dict:
    return {
        name: {
            "input_ports": [spec[0]],
            "input_kind": "file",
            "output_port": spec[1],
            "output_type": spec[3],
            "accepts_partial": False,
            "params_schema": spec[2].model_json_schema(),
        }
        for name, spec in OPERATIONS.items()
    }
