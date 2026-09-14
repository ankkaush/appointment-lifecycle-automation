"""Provider-agnostic shapes for the AI interpretation layer. Nothing here
imports Anthropic, OpenAI, or any vendor SDK — see app/ai/providers/ for
the one module per vendor that does.
"""

from __future__ import annotations

import enum
from datetime import date

from pydantic import BaseModel, Field


class Intent(str, enum.Enum):
    BOOK = "book"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    QUESTION = "question"
    UNKNOWN = "unknown"


class InterpretedRequest(BaseModel):
    """The AI's structured read of one customer message. Nothing here is
    authoritative — see app/ai/resolve.py and the architecture baseline,
    Section G: this is interpretation, not a decision. `confidence` is
    advisory only and never gates a booking on its own.
    """

    intent: Intent
    service_hint: str | None = Field(default=None, description="verbatim service wording")
    date_hint: str | None = Field(default=None, description="verbatim date/time phrase")
    resolved_date: date | None = Field(
        default=None,
        description="best-effort concrete date; still re-validated deterministically downstream",
    )
    time_preference: str | None = None
    is_ambiguous: bool
    ambiguity_reason: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)


class AIInvocationMetadata(BaseModel):
    """Technical record of one AI call — latency/token/cost. The shape the
    architecture baseline's AIInvocation entity will persist once the
    workflow/audit trail exists (Phase 3); not persisted yet here."""

    provider: str
    model: str
    latency_ms: float
    input_tokens: int
    output_tokens: int


class InterpretationOutcome(BaseModel):
    request: InterpretedRequest
    metadata: AIInvocationMetadata
