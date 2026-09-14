from datetime import date

import pytest
from pydantic import ValidationError

from app.ai.schemas import Intent, InterpretedRequest


def test_minimal_valid_request() -> None:
    req = InterpretedRequest(intent=Intent.QUESTION, is_ambiguous=False, confidence=0.9)
    assert req.service_hint is None
    assert req.resolved_date is None


def test_invalid_intent_value_rejected() -> None:
    with pytest.raises(ValidationError):
        InterpretedRequest(intent="reboot", is_ambiguous=False, confidence=0.5)


def test_confidence_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        InterpretedRequest(intent=Intent.BOOK, is_ambiguous=False, confidence=1.5)
    with pytest.raises(ValidationError):
        InterpretedRequest(intent=Intent.BOOK, is_ambiguous=False, confidence=-0.1)


def test_missing_required_field_rejected() -> None:
    with pytest.raises(ValidationError):
        InterpretedRequest(intent=Intent.BOOK, confidence=0.5)  # missing is_ambiguous


def test_full_request_round_trips() -> None:
    req = InterpretedRequest(
        intent=Intent.BOOK,
        service_hint="haircut",
        date_hint="next Tuesday",
        resolved_date=date(2026, 9, 22),
        time_preference="after 3pm",
        is_ambiguous=False,
        confidence=0.87,
    )
    dumped = req.model_dump(mode="json")
    assert dumped["intent"] == "book"
    assert dumped["resolved_date"] == "2026-09-22"


# --- candidate_intents normalization (architecture review, Finding 1) ---


def test_candidate_intents_kept_when_ambiguous_with_two_distinct() -> None:
    req = InterpretedRequest(
        intent=Intent.BOOK,
        is_ambiguous=True,
        candidate_intents=[Intent.BOOK, Intent.QUESTION],
        confidence=0.5,
    )
    assert req.candidate_intents == [Intent.BOOK, Intent.QUESTION]


def test_candidate_intents_nulled_when_not_ambiguous() -> None:
    # Even if the model mistakenly supplies candidates on a non-ambiguous
    # read, they're not meaningful -- is_ambiguous is the gate.
    req = InterpretedRequest(
        intent=Intent.BOOK,
        is_ambiguous=False,
        candidate_intents=[Intent.BOOK, Intent.QUESTION],
        confidence=0.9,
    )
    assert req.candidate_intents is None


def test_candidate_intents_nulled_when_single_entry() -> None:
    # A one-item "fork" isn't a fork -- that's just `intent` itself.
    req = InterpretedRequest(
        intent=Intent.BOOK, is_ambiguous=True, candidate_intents=[Intent.BOOK], confidence=0.4
    )
    assert req.candidate_intents is None


def test_candidate_intents_deduped() -> None:
    req = InterpretedRequest(
        intent=Intent.BOOK,
        is_ambiguous=True,
        candidate_intents=[Intent.BOOK, Intent.BOOK, Intent.QUESTION],
        confidence=0.4,
    )
    assert req.candidate_intents == [Intent.BOOK, Intent.QUESTION]


def test_candidate_intents_nulled_when_dedup_leaves_one() -> None:
    req = InterpretedRequest(
        intent=Intent.BOOK,
        is_ambiguous=True,
        candidate_intents=[Intent.BOOK, Intent.BOOK],
        confidence=0.4,
    )
    assert req.candidate_intents is None


def test_candidate_intents_none_by_default() -> None:
    req = InterpretedRequest(intent=Intent.BOOK, is_ambiguous=True, confidence=0.4)
    assert req.candidate_intents is None
