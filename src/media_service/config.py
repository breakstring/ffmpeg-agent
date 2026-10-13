import ipaddress
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Storage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: Path
    role: Literal["input", "output"]
    callers: list[str] = Field(min_length=1)
    quota_bytes: int = Field(default=20 * 1024**3, gt=0)


class SourceRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str
    ports: list[int] = Field(default_factory=lambda: [443])
    schemes: list[Literal["http", "https"]] = Field(default_factory=lambda: ["https"])
    networks: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_rule(self):
        if (
            not self.host
            or any(c in self.host for c in "/@?#*")
            or any(ord(c) <= 32 for c in self.host)
        ):
            raise ValueError("source host must be an exact hostname or IP address")
        self.host = self.host.encode("idna").decode("ascii").lower().rstrip(".")
        if not self.ports or len(self.ports) > 16 or any(not 1 <= p <= 65535 for p in self.ports):
            raise ValueError("invalid source ports")
        if not self.schemes:
            raise ValueError("source schemes cannot be empty")
        self.networks = [str(ipaddress.ip_network(value, strict=True)) for value in self.networks]
        return self


class Caller(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tokens: list[SecretStr] = Field(default_factory=list, max_length=2)
    default_output_storage: str
    max_queued_jobs: int = Field(default=16, ge=1, le=1024)
    max_running_jobs: int = Field(default=4, ge=1, le=4)
    source_rules: list[SourceRule] = Field(default_factory=list)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="forbid", env_prefix="MEDIA_", env_nested_delimiter="__"
    )
    state_dir: Path = Path("/var/lib/ffmpeg-agent/state")
    work_dir: Path = Path("/var/lib/ffmpeg-agent/work")
    storages: dict[str, Storage]
    callers: dict[str, Caller]
    service_token: SecretStr | None = None
    service_caller: str | None = None
    upload_retention_seconds: int = Field(default=86400, ge=1, le=604800)
    max_upload_bytes: int = Field(default=4 * 1024**3, gt=0)
    upload_timeout_seconds: float = Field(default=300, gt=0)
    upload_idle_seconds: float = Field(default=30, gt=0)
    max_uploads: int = Field(default=2, ge=1, le=4)
    worker_concurrency: int = Field(default=1, ge=1, le=4)
    ffmpeg_threads: int = Field(default=2, ge=1, le=16)
    ffmpeg_filter_threads: int = Field(default=1, ge=1, le=4)
    max_queued_jobs: int = Field(default=32, ge=1, le=1024)
    max_input_bytes: int = Field(default=2 * 1024**3, ge=1)
    max_output_bytes: int = Field(default=2 * 1024**3, ge=1)
    max_work_bytes: int = Field(default=4 * 1024**3, ge=1)
    max_pixels: int = Field(default=40_000_000, ge=1)
    max_duration_seconds: int = Field(default=3600, ge=1)
    disk_reserve_bytes: int = Field(default=512 * 1024**2, ge=0)
    task_timeout_seconds: float = Field(default=3600, gt=0)
    http_connect_seconds: float = Field(default=10, gt=0)
    http_idle_seconds: float = Field(default=30, gt=0)
    http_download_seconds: float = Field(default=300, gt=0)
    http_send_seconds: float = Field(default=600, gt=0)
    http_attempts: int = Field(default=3, ge=1, le=3)
    max_redirects: int = Field(default=3, ge=0, le=5)
    stop_grace_seconds: float = Field(default=10, gt=0)
    max_attempts: int = Field(default=2, ge=1, le=2)
    execution_lease_seconds: float = Field(default=30, ge=1)
    max_retention_seconds: int = Field(default=604800, ge=1, le=604800)
    max_lease_seconds: int = Field(default=3600, ge=1, le=3600)
    max_lease_total_seconds: int = Field(default=86400, ge=1, le=86400)
    idempotency_seconds: int = Field(default=86400, ge=86400)
    gc_interval_seconds: float = Field(default=10, gt=0)
    scheduler_interval_seconds: float = Field(default=0.2, gt=0)
    max_request_bytes: int = Field(default=256 * 1024, ge=1024)
    public_base_url: str = "http://localhost:8080"
    runtime_mode: Literal["local", "container"] = "container"
    engine_series: str = "7.1"
    image_version: str = "0.1.0"
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)

    @model_validator(mode="after")
    def consistent(self):
        if self.service_caller is None and len(self.callers) == 1:
            self.service_caller = next(iter(self.callers))
        if "service_token" in self.model_fields_set:
            if self.service_caller not in self.callers:
                raise ValueError("single-token mode requires a configured service caller")
            if self.service_token is not None:
                value = self.service_token.get_secret_value()
                if len(value) < 24 or any(ord(c) < 33 or ord(c) > 126 for c in value):
                    raise ValueError("service token must be printable ASCII with at least 24 characters")
        roots = [self.state_dir, self.work_dir] + [s.root for s in self.storages.values()]
        for root in roots:
            if not root.is_absolute():
                raise ValueError("storage paths must be absolute")
        resolved = [p.resolve() for p in roots]
        for index, path in enumerate(resolved):
            for other in resolved[index + 1 :]:
                if path.is_relative_to(other) or other.is_relative_to(path):
                    raise ValueError("storage roots must not overlap")
        seen_tokens = set()
        for name, caller in self.callers.items():
            if "service_token" not in self.model_fields_set and not caller.tokens:
                raise ValueError("legacy authentication requires caller tokens")
            output = self.storages.get(caller.default_output_storage)
            if output is None or output.role != "output" or name not in output.callers:
                raise ValueError("caller requires an authorized default output storage")
            for token in caller.tokens:
                value = token.get_secret_value()
                if (
                    len(value) < 24
                    or value in seen_tokens
                    or any(ord(c) < 33 or ord(c) > 126 for c in value)
                ):
                    raise ValueError(
                        "credentials must be unique printable ASCII with at least 24 characters"
                    )
                seen_tokens.add(value)
        return self

    @property
    def uploads_dir(self) -> Path:
        return self.work_dir / "_uploads"

    @classmethod
    def from_file(cls) -> Settings:
        path = Path(os.environ.get("MEDIA_CONFIG_FILE", "/run/secrets/media-config.json"))
        return cls.model_validate(json.loads(path.read_text()))

    def public_limits(self) -> dict:
        return {
            key: getattr(self, key)
            for key in (
                "worker_concurrency",
                "max_queued_jobs",
                "max_input_bytes",
                "max_output_bytes",
                "max_work_bytes",
                "max_pixels",
                "max_duration_seconds",
                "task_timeout_seconds",
                "http_download_seconds",
                "http_send_seconds",
                "http_attempts",
                "max_attempts",
                "max_retention_seconds",
                "max_lease_seconds",
                "max_lease_total_seconds",
                "idempotency_seconds",
                "max_request_bytes",
                "upload_retention_seconds",
                "max_upload_bytes",
                "upload_timeout_seconds",
                "upload_idle_seconds",
                "max_uploads",
            )
        } | {
            "max_inputs": 4,
            "max_steps": 16,
            "max_outputs": 16,
            "max_depth": 16,
            "max_frame_requests": 100,
            "api_processes": 1,
        }
