from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.workflow.models import ProcessingRunState


class StartRequestIn(BaseModel):
    business_id: UUID
    customer_id: UUID
    message: str


class OfferedSlotOut(BaseModel):
    staff_id: UUID
    start_at: datetime
    end_at: datetime


class ConfirmSlotIn(BaseModel):
    start_at: datetime
    idempotency_key: str


class ProcessingRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    business_id: UUID
    customer_id: UUID
    state: ProcessingRunState
    matched_service_id: UUID | None
    offered_slots: list[OfferedSlotOut] | None
    resulting_appointment_id: UUID | None


class WorkflowStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    step_name: str
    detail: dict | None
    created_at: datetime


class AIInvocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    provider: str
    model: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    intent: str
    is_ambiguous: bool
    confidence: float


class ProcessingRunTraceOut(BaseModel):
    """The observability answer to "what happened to this request?" --
    the ProcessingRun plus its full step-by-step trace and AI call, in
    order. See the architecture baseline, Section N."""

    run: ProcessingRunOut
    steps: list[WorkflowStepOut]
    ai_invocation: AIInvocationOut | None


class EscalationCaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    processing_run_id: UUID
    reason: str
    status: str
    created_at: datetime
    resolved_at: datetime | None
