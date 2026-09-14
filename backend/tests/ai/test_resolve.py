from app.ai.resolve import match_known_service

KNOWN = ["Haircut", "Deep Tissue Massage", "Consultation"]


def test_exact_case_insensitive_match() -> None:
    assert match_known_service("haircut", KNOWN) == "Haircut"
    assert match_known_service("HAIRCUT", KNOWN) == "Haircut"


def test_unambiguous_substring_match() -> None:
    assert match_known_service("massage", KNOWN) == "Deep Tissue Massage"


def test_no_hint_returns_none() -> None:
    assert match_known_service(None, KNOWN) is None
    assert match_known_service("   ", KNOWN) is None


def test_unmatched_hint_returns_none_rather_than_guessing() -> None:
    assert match_known_service("teeth whitening", KNOWN) is None


def test_ambiguous_multi_candidate_substring_returns_none() -> None:
    # No exact match, and the substring "mass" matches both candidates —
    # this must never resolve to a guess between them.
    known = ["Massage", "Massage Add-On"]
    assert match_known_service("mass", known) is None
