"""Workflow/observability schema: ProcessingRun (the request-handling
attempt) is deliberately a separate lifecycle from Appointment (the
persistent resource) -- see the architecture baseline's Concern 1
correction. Shares app.domain.models.Base so Alembic sees one metadata and
foreign keys to businesses/services/appointments resolve normally; the
dependency direction is workflow -> domain, never the reverse.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base


class ProcessingRunState(str, enum.Enum):
    """The request/attempt lifecycle -- ephemeral, describes handling one
    inbound message. Terminal states name an outcome, not a resource.
    Deliberately shares no vocabulary with AppointmentStatus."""

    RECEIVED = "RECEIVED"
    INTERPRETING = "INTERPRETING"
    AWAITING_CLARIFICATION = "AWAITING_CLARIFICATION"
    SLOTS_OFFERED = "SLOTS_OFFERED"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    BOOKING = "BOOKING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"
    EXPIRED = "EXPIRED"


class EscalationStatus(str, enum.Enum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class NotificationStatus(str, enum.Enum):
    SENT = "SENT"
    FAILED = "FAILED"


class ProcessingRun(Base):
    """One attempt to fulfill one inbound request. Correlates the
    AIInvocation, WorkflowSteps, and (if successful) the resulting
    Appointment -- it is a correlation record, not a lifecycle of the
    appointment itself."""

    __tablename__ = "processing_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="RESTRICT"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    raw_message: Mapped[str] = mapped_column(sa.Text, nullable=False)
    state: Mapped[ProcessingRunState] = mapped_column(
        sa.Enum(ProcessingRunState, name="processing_run_state"),
        nullable=False,
        default=ProcessingRunState.RECEIVED,
    )
    matched_service_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT"), nullable=True
    )
    # The AI's resolved_date, persisted here so a re-offer (after a
    # concurrent booking took the originally chosen slot) searches the
    # same date context rather than falling back to a generic window.
    resolved_date: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    # Working state for this attempt only -- not authoritative. Each entry
    # is {"staff_id", "start_at", "end_at"}; re-validated deterministically
    # against real availability at confirm time regardless of what's here.
    offered_slots: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # How many clarifying questions have been asked, capped at
    # MAX_CLARIFICATION_ROUNDS in orchestrator.py -- still ambiguous past
    # the cap escalates rather than looping indefinitely.
    clarification_rounds: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    # The bounded conversation transcript: [{"role", "content", "at"}, ...].
    # Working/operational data, not a business record -- same shorter-
    # retention register as WorkflowStep (Concern 6), not AuditEvent. Lives
    # and dies with this run; no copy of it exists anywhere else. No purge
    # job exists yet -- that lands with Phase 6's background-job sweep,
    # which will also own expiring abandoned AWAITING_CLARIFICATION runs.
    messages: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    resulting_appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )


class WorkflowStep(Base):
    """The execution trace: what happened while processing one request,
    including steps that produced no state change. Operational/debug
    register -- shorter retention than AuditEvent is fine here."""

    __tablename__ = "workflow_steps"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    processing_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_name: Mapped[str] = mapped_column(String(100), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class AIInvocation(Base):
    """Technical + structured record of one AI call -- latency/token/cost
    plus the interpreted fields, for AI evaluation and cost/latency
    observability. Referenced by a WorkflowStep; not a step itself."""

    __tablename__ = "ai_invocations"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    processing_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    latency_ms: Mapped[float] = mapped_column(sa.Float, nullable=False)
    input_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    intent: Mapped[str] = mapped_column(String(20), nullable=False)
    service_hint: Mapped[str | None] = mapped_column(String(200), nullable=True)
    date_hint: Mapped[str | None] = mapped_column(String(200), nullable=True)
    resolved_date: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    time_preference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_ambiguous: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    ambiguity_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # List of Intent values (as strings), populated only for a genuine
    # small fork (e.g. ["book", "question"]) -- see architecture review,
    # Finding 1.
    candidate_intents: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class AuditEvent(Base):
    """The business record: what changed on a persistent entity, and why.
    Append-only, long retention -- distinct from WorkflowStep's shorter-
    lived execution trace."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    processing_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="SET NULL"), nullable=True
    )
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(50), nullable=True)
    to_state: Mapped[str] = mapped_column(String(50), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class EscalationCase(Base):
    """Created whenever a ProcessingRun reaches ESCALATED. Minimal for
    Phase 3: creation + a resolve action. The dashboard (Phase 8) gets a
    real queue view over this table -- the data model is what matters now."""

    __tablename__ = "escalation_cases"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    processing_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[EscalationStatus] = mapped_column(
        sa.Enum(EscalationStatus, name="escalation_status"),
        nullable=False,
        default=EscalationStatus.OPEN,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class Notification(Base):
    """A sent (or attempted) customer communication. channel/provider is
    "mock" until Phase 7's real provider lands -- workflow code depends on
    the NotificationService Protocol in notifications.py, never a vendor."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    processing_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="SET NULL"), nullable=True
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True
    )
    channel: Mapped[str] = mapped_column(String(50), nullable=False)
    to_contact: Mapped[str] = mapped_column(String(200), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        sa.Enum(NotificationStatus, name="notification_status"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
