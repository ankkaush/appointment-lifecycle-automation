"""Deterministic post-processing of the AI's raw extraction.

Runs regardless of AI confidence — the architecture baseline's principle
that AI interprets and deterministic code decides, applied at the smallest
possible scale here (Phase 3's workflow engine will apply it end to end).
"""

from __future__ import annotations


def match_known_service(hint: str | None, known_services: list[str]) -> str | None:
    """Case-insensitive exact match first, then a conservative substring
    match. Returns None — never a guess — when nothing lines up clearly,
    so the caller can treat an unmatched service as a signal to ask rather
    than silently proceeding with whatever the model said.
    """
    if not hint:
        return None
    normalized = hint.strip().lower()
    if not normalized:
        return None

    for service in known_services:
        if service.lower() == normalized:
            return service

    candidates = [
        service
        for service in known_services
        if normalized in service.lower() or service.lower() in normalized
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None
