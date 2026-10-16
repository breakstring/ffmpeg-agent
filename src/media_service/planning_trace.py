import asyncio
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from openai import APIConnectionError, APIError, APITimeoutError
from pydantic import ValidationError
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)

from media_service.telemetry import event

PlanningPhase = Literal[
    "state_read", "model_planning", "plan_validate", "plan_save", "media_accept",
]
ModelPhase = Literal[
    "request", "http_headers", "http_body", "response_parse", "output_validate",
]
_PLANNING_PHASES = frozenset({
    "state_read", "model_planning", "plan_validate", "plan_save", "media_accept",
})
_MODEL_PHASES = frozenset({
    "request", "http_headers", "http_body", "response_parse", "output_validate",
})


def safe_error_type(error: BaseException) -> str:
    # 供应商错误通常经 SDK 包装；仅按已知类型归类，不读取异常正文或动态类名。
    current: BaseException | None = error
    for _ in range(5):
        if isinstance(current, asyncio.CancelledError):
            return "cancelled"
        if isinstance(current, TimeoutError | APITimeoutError):
            return "timeout"
        if isinstance(current, ModelHTTPError):
            return "provider_http"
        if isinstance(current, APIConnectionError):
            return "provider_connection"
        if isinstance(current, ValidationError | UnexpectedModelBehavior | UsageLimitExceeded):
            return "invalid_output"
        current = current.__cause__ if current is not None else None
    if isinstance(error, ModelAPIError | APIError):
        return "provider_error"
    return "internal_error"


@dataclass
class PlanningTrace:
    agent_task_id: str
    request_id: str | None = None
    phase: str = "claimed"
    model_phase: str | None = None
    model_request: int = 0

    def fields(self) -> dict:
        result: dict[str, object] = {"agent_task_id": self.agent_task_id, "phase": self.phase}
        if self.request_id is not None:
            result["request_id"] = self.request_id
        if self.model_phase is not None:
            result["model_phase"] = self.model_phase
            result["model_request"] = self.model_request
        return result

    @contextmanager
    def stage(self, phase: PlanningPhase) -> Iterator[None]:
        if phase not in _PLANNING_PHASES:
            raise ValueError("unknown planning phase")
        self.phase = phase
        started = time.monotonic()
        event("planning_stage", level=logging.DEBUG, **self.fields(), status="started")
        try:
            yield
        except BaseException as error:
            event("planning_stage", level=logging.DEBUG, **self.fields(),
                  status="cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                  duration_ms=round((time.monotonic() - started) * 1000, 3),
                  error_type=safe_error_type(error))
            raise
        else:
            event("planning_stage", level=logging.DEBUG, **self.fields(), status="succeeded",
                  duration_ms=round((time.monotonic() - started) * 1000, 3))

    def model_checkpoint(self, phase: ModelPhase, *, http_status: int | None = None) -> None:
        if phase not in _MODEL_PHASES:
            raise ValueError("unknown model phase")
        self.model_phase = phase
        fields = self.fields()
        if http_status is not None:
            fields["http_status"] = http_status
        event("planning_model_stage", level=logging.DEBUG, **fields, status="started")

    @contextmanager
    def model_call(self) -> Iterator[None]:
        self.model_request += 1
        self.model_phase = "request"
        started = time.monotonic()
        event("planning_model_request", level=logging.DEBUG, **self.fields(), status="started")
        try:
            yield
        except BaseException as error:
            fields = self.fields()
            if isinstance(error, ModelHTTPError):
                fields["http_status"] = error.status_code
            event("planning_model_request", level=logging.WARNING, **fields,
                  status="cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                  duration_ms=round((time.monotonic() - started) * 1000, 3),
                  error_type=safe_error_type(error))
            raise
        else:
            event("planning_model_request", level=logging.DEBUG, **self.fields(), status="succeeded",
                  duration_ms=round((time.monotonic() - started) * 1000, 3))
            self.model_checkpoint("output_validate")


_current: ContextVar[PlanningTrace | None] = ContextVar("planning_trace", default=None)


def current_planning_trace() -> PlanningTrace | None:
    return _current.get()


@contextmanager
def planning_trace(agent_task_id: str, request_id: str | None = None) -> Iterator[PlanningTrace]:
    trace = PlanningTrace(agent_task_id, request_id)
    token = _current.set(trace)
    try:
        yield trace
    finally:
        _current.reset(token)
