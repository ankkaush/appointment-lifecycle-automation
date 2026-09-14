from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.workflow.models import ProcessingRunState

MAX_MESSAGE_LENGTH = 2000


class StartRequestIn(BaseModel):
    business_id: UUID
    # Either an existing customer_id, or a name+contact for a first-time
    # caller -- see app.domain.customers.find_or_create_customer. No
    # account system: a contact method is the entire identity model.
    customer_id: UUID | None = None
    customer_name: str | None = None
    customer_contact: str | None = None
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)

    @model_validator(mode="after")
    def _customer_identity_provided(self) -> "StartRequestIn":
        if self.customer_id is None and not (self.customer_name and self.customer_contact):
            raise ValueError(
                "provide either customer_id, or both customer_name and customer_contact"
            )
        return self


class ReplyIn(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)


class ConversationMessageOut(BaseModel):
    role: str
    content: str
    at: str


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
    clarification_rounds: int
    messages: list[ConversationMessageOut] | None


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
