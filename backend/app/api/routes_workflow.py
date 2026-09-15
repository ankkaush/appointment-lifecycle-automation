"""HTTP boundary for the workflow orchestrator. Same discipline as the
other routers: parse, delegate, translate errors -- no orchestration logic
lives here.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.dependency import get_interpreter
from app.ai.interpreter import Interpreter
from app.api.auth import require_business_api_key
from app.api.rate_limit import enforce_chat_rate_limit
from app.core.db import get_db
from app.domain.exceptions import DomainError
from app.domain.models import Appointment, Customer
from app.domain.schemas import AppointmentOut
from app.workflow.calendar import CalendarProvider, MockCalendarProvider
from app.workflow.exceptions import WorkflowError
from app.workflow.models import (
    AIInvocation,
    EscalationCase,
    EscalationStatus,
    ProcessingRun,
    WorkflowStep,
)
from app.workflow.notification_dependency import get_notification_service
from app.workflow.notifications import NotificationService
from app.workflow.orchestrator import (
    confirm_slot,
    reply_to_clarification,
    retry_calendar_sync,
    start_request,
)
from app.workflow.schemas import (
    AIInvocationOut,
    ConfirmSlotIn,
    EscalationCaseOut,
    EscalationQueueItemOut,
    ProcessingRunOut,
    ProcessingRunTraceOut,
    ReplyIn,
    StartRequestIn,
    WorkflowStepOut,
)

router = APIRouter()


def get_calendar_provider() -> CalendarProvider:
    return MockCalendarProvider()


@router.post(
    "/requests",
    response_model=ProcessingRunOut,
    status_code=201,
    dependencies=[Depends(enforce_chat_rate_limit)],
)
async def create_request(
    payload: StartRequestIn,
    db: AsyncSession = Depends(get_db),
    interpreter: Interpreter = Depends(get_interpreter),
    notification_service: NotificationService = Depends(get_notification_service),
    calendar_provider: CalendarProvider = Depends(get_calendar_provider),
) -> ProcessingRun:
    try:
        return await start_request(
            db,
            payload,
            interpreter=interpreter,
            notification_service=notification_service,
            calendar_provider=calendar_provider,
        )
    except DomainError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/requests/{processing_run_id}/reply",
    response_model=ProcessingRunOut,
    dependencies=[Depends(enforce_chat_rate_limit)],
)
async def reply_request(
    processing_run_id: UUID,
    payload: ReplyIn,
    db: AsyncSession = Depends(get_db),
    interpreter: Interpreter = Depends(get_interpreter),
    notification_service: NotificationService = Depends(get_notification_service),
    calendar_provider: CalendarProvider = Depends(get_calendar_provider),
) -> ProcessingRun:
    try:
        return await reply_to_clarification(
            db,
            processing_run_id,
            payload.message,
            interpreter=interpreter,
            notification_service=notification_service,
            calendar_provider=calendar_provider,
        )
    except WorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/requests/{processing_run_id}/confirm", response_model=ProcessingRunOut)
async def confirm_request(
    processing_run_id: UUID,
    payload: ConfirmSlotIn,
    db: AsyncSession = Depends(get_db),
    notification_service: NotificationService = Depends(get_notification_service),
    calendar_provider: CalendarProvider = Depends(get_calendar_provider),
) -> ProcessingRun:
    try:
        return await confirm_slot(
            db,
            processing_run_id,
            payload,
            notification_service=notification_service,
            calendar_provider=calendar_provider,
        )
    except WorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/appointments/{appointment_id}/retry-calendar-sync", response_model=AppointmentOut)
async def retry_calendar_sync_request(
    appointment_id: UUID,
    db: AsyncSession = Depends(get_db),
    calendar_provider: CalendarProvider = Depends(get_calendar_provider),
    authorization: str | None = Header(None),
) -> Appointment:
    appointment = await db.get(Appointment, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
    await require_business_api_key(db, appointment.business_id, authorization)
    try:
        return await retry_calendar_sync(db, appointment_id, calendar_provider=calendar_provider)
    except DomainError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/requests/{processing_run_id}/trace", response_model=ProcessingRunTraceOut)
async def get_trace(
    processing_run_id: UUID,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
) -> ProcessingRunTraceOut:
    run = await db.get(ProcessingRun, processing_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="ProcessingRun not found")
    await require_business_api_key(db, run.business_id, authorization)

    steps = (
        (
            await db.execute(
                select(WorkflowStep)
                .where(WorkflowStep.processing_run_id == run.id)
                .order_by(WorkflowStep.created_at)
            )
        )
        .scalars()
        .all()
    )
    invocation = (
        await db.execute(select(AIInvocation).where(AIInvocation.processing_run_id == run.id))
    ).scalar_one_or_none()

    return ProcessingRunTraceOut(
        run=ProcessingRunOut.model_validate(run),
        steps=[WorkflowStepOut.model_validate(s) for s in steps],
        ai_invocation=AIInvocationOut.model_validate(invocation) if invocation else None,
    )


@router.get("/escalations", response_model=list[EscalationQueueItemOut])
async def list_escalations(
    business_id: UUID,
    status_filter: EscalationStatus | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
) -> list[EscalationQueueItemOut]:
    # business_id is required and joined-through-filtered here, not
    # optional -- this endpoint used to return every business's
    # escalations to any caller. Phase 9 scoped it by business_id alone;
    # Phase 10 requires that business's API key too.
    await require_business_api_key(db, business_id, authorization)
    stmt = (
        select(EscalationCase, ProcessingRun, Customer)
        .join(ProcessingRun, ProcessingRun.id == EscalationCase.processing_run_id)
        .join(Customer, Customer.id == ProcessingRun.customer_id)
        .where(ProcessingRun.business_id == business_id)
        .order_by(EscalationCase.created_at.desc())
    )
    if status_filter is not None:
        stmt = stmt.where(EscalationCase.status == status_filter)
    rows = (await db.execute(stmt)).all()
    return [
        EscalationQueueItemOut(
            id=case.id,
            processing_run_id=case.processing_run_id,
            reason=case.reason,
            status=case.status.value,
            created_at=case.created_at,
            resolved_at=case.resolved_at,
            customer_name=customer.name,
            customer_contact=customer.contact,
            raw_message=run.raw_message,
        )
        for case, run, customer in rows
    ]


@router.post("/escalations/{escalation_id}/resolve", response_model=EscalationCaseOut)
async def resolve_escalation(
    escalation_id: UUID,
    business_id: UUID,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
) -> EscalationCase:
    await require_business_api_key(db, business_id, authorization)
    case = await db.get(EscalationCase, escalation_id)
    if case is None:
        raise HTTPException(status_code=404, detail="EscalationCase not found")
    run = await db.get(ProcessingRun, case.processing_run_id)
    if run is None or run.business_id != business_id:
        # Same 404 as "doesn't exist" -- a mismatched business_id learns
        # nothing about whether the escalation_id is real for someone
        # else's business.
        raise HTTPException(status_code=404, detail="EscalationCase not found")
    case.status = EscalationStatus.RESOLVED
    case.resolved_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(case)
    return case
