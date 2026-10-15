from typing import Literal

from pydantic import Field, model_validator

from media_service.models import (
    ErrorResult,
    Input,
    JobCreate,
    JobResult,
    Name,
    OutputPolicy,
    Step,
    StepBinding,
    StrictModel,
)


class AgentRequest(StrictModel):
    intent: str = Field(min_length=1, max_length=4000)
    inputs: dict[Name, Input] = Field(min_length=1, max_length=4)
    output_policy: OutputPolicy = Field(default_factory=OutputPolicy)

    @model_validator(mode="after")
    def nonblank(self):
        if not self.intent.strip():
            raise ValueError("intent must not be blank")
        return self


class MediaPlan(StrictModel):
    steps: list[Step] = Field(min_length=1, max_length=16)
    outputs: dict[Name, StepBinding] = Field(min_length=1, max_length=16)

    def bind(self, request: AgentRequest) -> JobCreate:
        return JobCreate.model_validate({
            "inputs": request.model_dump()["inputs"],
            "steps": [step.model_dump() for step in self.steps],
            "outputs": {name: binding.model_dump() for name, binding in self.outputs.items()},
            "output_policy": request.output_policy.model_dump(),
        })


class PlanningOutcome(StrictModel):
    decision: Literal["execute", "needs_input", "unsupported"]
    message: str = Field(default="", max_length=1000)
    plan: MediaPlan | None = None

    @model_validator(mode="after")
    def consistent(self):
        if (self.decision == "execute") != (self.plan is not None):
            raise ValueError("only executable outcomes carry a plan")
        if self.decision != "execute" and not self.message.strip():
            raise ValueError("non-executable outcomes require an explanation")
        return self


AgentStatus = Literal[
    "queued", "planning", "planned", "running", "canceling", "succeeded", "partial",
    "needs_input", "unsupported", "failed", "canceled",
]


class AgentTaskResult(StrictModel):
    agent_task_id: str
    status: AgentStatus
    created_at: str
    planning_deadline_at: str
    decision: Literal["execute", "needs_input", "unsupported"] | None = None
    message: str | None = None
    plan: MediaPlan | None = None
    job_id: str | None = None
    job: JobResult | None = None
    error: ErrorResult | None = None
