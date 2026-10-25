from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator

ACTIVE = ("queued", "planning", "executing", "awaiting_approval")
TERMINAL = ("succeeded", "failed", "cancelled", "timeout")
STATUS = ACTIVE + TERMINAL


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Actor(Strict):
    user_id: int = Field(gt=0, strict=True)
    role: Literal["operator", "admin"]


class Constraints(Strict):
    max_risk_class: Literal["R0", "R2"] = "R0"
    timeout_sec: int = Field(default=300, gt=0, le=300, strict=True)


class Target(Strict):
    asset_id: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=2000)


class Task(Strict):
    protocol: Literal["ma/v1", "ma-run/v1"]
    task_id: UUID
    trace_id: UUID
    idempotency_key: UUID
    session_id: UUID
    parent_turn_id: int | None = Field(default=None, gt=0, strict=True)
    run_id: UUID | None = None
    actor: Actor
    intent: str = Field(min_length=1, max_length=4000)
    user_request: str = Field(min_length=1, max_length=32000)
    targets: list[Target] = Field(default_factory=list, max_length=20)
    constraints: Constraints = Field(default_factory=Constraints)
    created_at: datetime
    task_kind: Literal['ops','security_inspection'] = 'ops'
    operation: Literal['read_only','restart_drill_container'] = 'read_only'

    @model_validator(mode='after')
    def operation_risk(self):
        if (self.protocol=='ma-run/v1') != (self.run_id is not None):raise ValueError('run_binding_protocol_required')
        if self.protocol=='ma-run/v1' and self.operation!='read_only':raise ValueError('run_tasks_read_only_required')
        if self.run_id is not None and self.parent_turn_id is None:raise ValueError('run_requires_parent_turn')
        if self.task_kind=='security_inspection' and (self.operation!='read_only' or self.constraints.max_risk_class!='R0'):
            raise ValueError('inspection_read_only_required')
        if (self.operation=='restart_drill_container') != (self.constraints.max_risk_class=='R2'):
            raise ValueError('operation_risk_mismatch')
        if self.operation=='restart_drill_container' and self.actor.role!='admin':
            raise ValueError('admin_required')
        return self


class Bind(Strict):
    session_id: UUID
    parent_turn_id: int = Field(gt=0, strict=True)


class Guard(Strict):
    operation_id: UUID
    action: Literal["reserve", "release", "commit"]


class Verification(Strict):
    session_id: UUID
    parent_turn_id: int = Field(gt=0, strict=True)
    device_id: int = Field(gt=0, strict=True)
    checks: list[Literal["latest_telemetry", "current_alarms", "prediction"]] = Field(min_length=1, max_length=3)
    verification_id: UUID


class Diagnosis(Strict):
    summary: str | None = None
    hypotheses: list[str] = Field(default_factory=list, max_length=30)
    recommendations: list[str] = Field(default_factory=list, max_length=30)
    clarification: str | None = None


class TaskError(Strict):
    code: str
    message: str
    retryable: bool


class Step(Strict):
    task_id: UUID
    step_no: int
    tool: str
    version: str
    args: dict
    args_hash: str
    risk_class: Literal['R0','R2']
    required: bool
    status: Literal['pending','running','succeeded','failed','skipped','cancelled','timeout']
    execution_epoch: int
    result: object | None
    error: TaskError | None
    started_at: datetime
    finished_at: datetime | None
    asset_id: str | None = None
    asset_version: int | None = None


class ToolCall(Strict):
    call_id: UUID
    task_id: UUID
    step_no: int
    trace_id: UUID
    execution_epoch: int
    args_hash: str
    status: Literal['running','succeeded','failed','cancelled','timeout']
    data: object | None
    error: TaskError | None
    duration_ms: int | None
    collected_at: datetime
    finished_at: datetime | None
    asset_id: str | None = None
    asset_version: int | None = None


class ResultEnvelope(Strict):
    run_id: UUID | None = None
    business_quality: dict | None = None
    task_kind: Literal['ops','security_inspection'] = 'ops'
    inspection_report: dict | None = None
    protocol: Literal['ma/v1','ma-run/v1']
    task_id: UUID
    trace_id: UUID
    session_id: UUID
    parent_turn_id: int | None
    intent: str
    status: Literal['queued','planning','executing','awaiting_approval','succeeded','failed','cancelled','timeout']
    steps: list[Step]
    calls: list[ToolCall]
    evidence_refs: list[str]
    diagnosis: Diagnosis
    warnings: list[str]
    error: TaskError | None
    tool_calls: list[dict]
    sources: list[dict]
    verification_results: dict | None
    device: dict | None
    model_calls: int
    link_pending: bool
    evidence: dict
    created_at: datetime
    finished_at: datetime | None
    infrastructure_evidence: list[dict] = Field(default_factory=list)
    approvals: list[dict] = Field(default_factory=list)
    execution_attempts: list[dict] = Field(default_factory=list)
    operation_evidence: list[dict] = Field(default_factory=list)
    security_reviews: list[dict] = Field(default_factory=list)
    security_events: list[dict] = Field(default_factory=list)


class AssetImport(Strict):
    registry_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


class ExecutionClaim(Strict):
    grant: str = Field(min_length=1,max_length=12000)


class ApprovalDecision(Strict):
    decision: Literal['approve','reject']
