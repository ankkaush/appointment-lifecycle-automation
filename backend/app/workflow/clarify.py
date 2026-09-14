"""Deterministic mapping from an ambiguous InterpretedRequest to a
customer-facing clarifying question.

No AI-authored text reaches the customer here -- ambiguity_reason stays
internal/diagnostic only (architecture review, Finding 2). The ambiguity
shapes this domain produces are a small, enumerable set, so this is
closed-set branching, not language generation: pure, synchronous, no DB,
no async -- unit-testable directly.

Priority, when more than one thing is missing/unclear at once: which
*intent* the customer means is the more fundamental uncertainty, so
candidate_intents is checked first. Only once the intent itself is
settled (or was never in question) do missing-detail questions apply --
and among those, the date is asked before the service, since "when"
tends to be the more natural first question in this domain, and asking
one thing at a time keeps each clarifying round bounded and answerable.
"""

from __future__ import annotations

from app.ai.schemas import Intent, InterpretedRequest

GENERIC_QUESTION = "Could you tell me a bit more about what you'd like to do?"

_INTENT_LABELS: dict[Intent, str] = {
    Intent.BOOK: "book an appointment",
    Intent.RESCHEDULE: "reschedule an appointment",
    Intent.CANCEL: "cancel an appointment",
    Intent.QUESTION: "ask a question",
}


def _label(intent: Intent) -> str:
    return _INTENT_LABELS.get(intent, intent.value)


def derive_clarifying_question(req: InterpretedRequest) -> str:
    if req.candidate_intents:
        labels = [_label(i) for i in req.candidate_intents]
        if len(labels) == 2:
            return f"Are you looking to {labels[0]}, or {labels[1]}?"
        return "Are you looking to " + ", ".join(labels[:-1]) + f", or {labels[-1]}?"

    if req.intent == Intent.BOOK and not req.date_hint:
        return "What day would you like to come in?"

    if req.intent == Intent.BOOK and not req.service_hint:
        return "What service would you like to book?"

    return GENERIC_QUESTION
