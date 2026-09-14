"""HTTP boundary for the workflow orchestrator. Same discipline as the
other routers: parse, delegate, translate errors -- no orchestration logic
lives here.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.dependency import get_interpreter
from app.ai.interpreter import Interpreter
from app.core.db import get_db
from app.domain.exceptions import DomainError
from app.workflow.exceptions import WorkflowError
from app.workflow.models import (
    AIInvocation,
    EscalationCase,
    EscalationStatus,
    ProcessingRun,
    WorkflowStep,
)
from app.workflow.notifications import MockNotificationProvider, NotificationService
from app.workflow.orchestrator import confirm_slot, start_request
from app.workflow.schemas import (
    AIInvocationOut,
    ConfirmSlotIn,
    EscalationCaseOut,
    ProcessingRunOut,
    ProcessingRunTraceOut,
    StartRequestIn,
    WorkflowStepOut,
)

router = APIRouter()


def get_notification_service() -> NotificationService:
    return MockNotificationProvider()


@router.post("/requests", response_model=ProcessingRunOut, status_code=201)
async def create_request(
    payload: StartRequestIn,
    db: AsyncSession = Depends(get_db),
    interpreter: Interpreter = Depends(get_interpreter),
) -> ProcessingRun:
    try:
        return await start_request(db, payload, interpreter=interpreter)
    except DomainError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/requests/{processing_run_id}/confirm", response_model=ProcessingRunOut)
async def confirm_request(
    processing_run_id: UUID,
    payload: ConfirmSlotIn,
    db: AsyncSession = Depends(get_db),
    notification_service: NotificationService = Depends(get_notification_service),
) -> ProcessingRun:
    try:
        return await confirm_slot(
            db, processing_run_id, payload, notification_service=notification_service
        )
    except WorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/requests/{processing_run_id}/trace", response_model=ProcessingRunTraceOut)
async def get_trace(
    processing_run_id: UUID, db: AsyncSession = Depends(get_db)
) -> ProcessingRunTraceOut:
    run = await db.get(ProcessingRun, processing_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="ProcessingRun not found")

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


@router.get("/escalations", response_model=list[EscalationCaseOut])
async def list_escalations(
    status_filter: EscalationStatus | None = None, db: AsyncSession = Depends(get_db)
) -> list[EscalationCase]:
    stmt = select(EscalationCase).order_by(EscalationCase.created_at.desc())
    if status_filter is not None:
        stmt = stmt.where(EscalationCase.status == status_filter)
    return list((await db.execute(stmt)).scalars().all())


@router.post("/escalations/{escalation_id}/resolve", response_model=EscalationCaseOut)
async def resolve_escalation(
    escalation_id: UUID, db: AsyncSession = Depends(get_db)
) -> EscalationCase:
    case = await db.get(EscalationCase, escalation_id)
    if case is None:
        raise HTTPException(status_code=404, detail="EscalationCase not found")
    case.status = EscalationStatus.RESOLVED
    case.resolved_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(case)
    return case
