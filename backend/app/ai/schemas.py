"""Provider-agnostic shapes for the AI interpretation layer. Nothing here
imports Anthropic, OpenAI, or any vendor SDK — see app/ai/providers/ for
the one module per vendor that does.
"""

from __future__ import annotations

import enum
from datetime import date

from pydantic import BaseModel, Field, model_validator


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
    # Internal/diagnostic only -- never shown to the customer directly.
    # See app.workflow.clarify.derive_clarifying_question for the
    # deterministic, customer-facing counterpart (architecture review,
    # Finding 2).
    ambiguity_reason: str | None = None
    # Populated only for a genuine, small fork between specific intents
    # (e.g. "Can I come Friday?" could be book or question) -- null for a
    # message with no coherent signal at all (zero candidates, not a
    # fork) and null for a message whose intent is already clear but
    # missing a detail. See architecture review, Finding 1.
    candidate_intents: list[Intent] | None = Field(default=None)
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _normalize_candidate_intents(self) -> InterpretedRequest:
        """Defensive normalization, not rejection -- a malformed model
        response here shouldn't blow up the whole interpretation. Only a
        real, ambiguous, >=2-distinct-candidate fork is meaningful; any
        other shape collapses to null."""
        if self.is_ambiguous and self.candidate_intents:
            deduped = list(dict.fromkeys(self.candidate_intents))
            self.candidate_intents = deduped if len(deduped) >= 2 else None
        else:
            self.candidate_intents = None
        return self


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
