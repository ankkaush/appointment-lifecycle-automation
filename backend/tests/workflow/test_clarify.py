"""Unit tests for the deterministic clarifying-question mapping. Pure,
synchronous, no DB, no AI -- exactly the point of keeping this logic
deterministic (architecture review, Finding 2).
"""

from app.ai.schemas import Intent, InterpretedRequest
from app.workflow.clarify import GENERIC_QUESTION, derive_clarifying_question


def _req(**overrides) -> InterpretedRequest:
    defaults = dict(intent=Intent.BOOK, is_ambiguous=True, confidence=0.5)
    defaults.update(overrides)
    return InterpretedRequest(**defaults)


# --- one thing missing at a time -----------------------------------------


def test_missing_date_only() -> None:
    req = _req(service_hint="Haircut", date_hint=None)
    assert derive_clarifying_question(req) == "What day would you like to come in?"


def test_missing_service_only() -> None:
    req = _req(service_hint=None, date_hint="Friday")
    assert derive_clarifying_question(req) == "What service would you like to book?"


def test_two_candidate_intents() -> None:
    req = _req(candidate_intents=[Intent.BOOK, Intent.QUESTION])
    expected = "Are you looking to book an appointment, or ask a question?"
    assert derive_clarifying_question(req) == expected


def test_no_structured_signal_falls_back_to_generic() -> None:
    req = _req(service_hint="Haircut", date_hint="Friday")  # nothing obviously missing
    assert derive_clarifying_question(req) == GENERIC_QUESTION


# --- priority when more than one thing is unclear at once ----------------


def test_candidate_intents_takes_priority_over_missing_date() -> None:
    # Both a fork between intents AND no date given -- which intent the
    # customer means is the more fundamental uncertainty, so that's what
    # gets asked first, not the date.
    req = _req(candidate_intents=[Intent.BOOK, Intent.QUESTION], date_hint=None, service_hint=None)
    expected = "Are you looking to book an appointment, or ask a question?"
    assert derive_clarifying_question(req) == expected


def test_missing_date_takes_priority_over_missing_service() -> None:
    # Both missing, no candidate_intents fork -- date is asked first
    # (the more natural first question in this domain), not service.
    req = _req(date_hint=None, service_hint=None)
    assert derive_clarifying_question(req) == "What day would you like to come in?"


# --- three-way (and more) candidate lists ---------------------------------


def test_three_candidate_intents_phrased_as_a_list() -> None:
    req = _req(candidate_intents=[Intent.BOOK, Intent.RESCHEDULE, Intent.CANCEL])
    assert derive_clarifying_question(req) == (
        "Are you looking to book an appointment, reschedule an appointment, "
        "or cancel an appointment?"
    )


def test_unrecognized_intent_label_falls_back_to_raw_value() -> None:
    # UNKNOWN has no hand-written label in _INTENT_LABELS -- exercises the
    # fallback-to-raw-value path through the public function rather than
    # reaching into a private helper.
    req = _req(candidate_intents=[Intent.BOOK, Intent.UNKNOWN])
    assert derive_clarifying_question(req) == "Are you looking to book an appointment, or unknown?"
