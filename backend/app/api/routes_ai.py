"""HTTP boundary for the AI interpretation layer. Same discipline as
app/api/routes.py: this router parses requests, calls into app.ai and
app.domain, and translates errors — no interpretation logic lives here.

This endpoint is a Phase 2 proof point (raw text in, validated structured
request out), not the customer-facing intake channel — that arrives with
the workflow engine in Phase 3, which will call the same Interpreter
Protocol this route uses.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.dependency import get_interpreter
from app.ai.exceptions import AIInterpretationError
from app.ai.interpreter import Interpreter
from app.ai.resolve import match_known_service
from app.ai.schemas import AIInvocationMetadata, InterpretedRequest
from app.core.db import get_db
from app.domain.models import Service

router = APIRouter()


class InterpretIn(BaseModel):
    business_id: UUID
    message: str


class InterpretOut(BaseModel):
    request: InterpretedRequest
    matched_service: str | None
    metadata: AIInvocationMetadata


@router.post("/interpret", response_model=InterpretOut)
async def interpret_message(
    payload: InterpretIn,
    db: AsyncSession = Depends(get_db),
    interpreter: Interpreter = Depends(get_interpreter),
) -> InterpretOut:
    known_services = (
        (
            await db.execute(
                select(Service.name).where(
                    Service.business_id == payload.business_id, Service.active.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )

    try:
        outcome = await interpreter.interpret(
            payload.message,
            today=datetime.now(UTC).date(),
            known_services=list(known_services),
        )
    except AIInterpretationError as exc:
        # The AI failed or returned something invalid — this is a failure
        # state, not a default interpretation to quietly proceed with.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    matched_service = match_known_service(outcome.request.service_hint, list(known_services))
    return InterpretOut(
        request=outcome.request, matched_service=matched_service, metadata=outcome.metadata
    )
