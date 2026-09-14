"""The deterministic ProcessingRun state machine. Enforced code, not just
a diagram -- an out-of-table transition raises rather than silently
mutating state, so a bug in the orchestrator can't quietly corrupt the
audit trail's story of what happened.
"""

from __future__ import annotations

from app.workflow.exceptions import InvalidTransitionError
from app.workflow.models import ProcessingRunState as S

TRANSITIONS: dict[S, frozenset[S]] = {
    S.RECEIVED: frozenset({S.INTERPRETING}),
    S.INTERPRETING: frozenset({S.SLOTS_OFFERED, S.ESCALATED, S.FAILED}),
    S.SLOTS_OFFERED: frozenset({S.AWAITING_CONFIRMATION, S.ESCALATED}),
    S.AWAITING_CONFIRMATION: frozenset({S.BOOKING, S.EXPIRED}),
    # BOOKING -> SLOTS_OFFERED is the re-offer path: the chosen slot was
    # taken by a concurrent request between offer and confirm (baseline,
    # Section C) -- re-run availability and offer again rather than fail.
    S.BOOKING: frozenset({S.SUCCEEDED, S.SLOTS_OFFERED, S.ESCALATED, S.FAILED}),
    S.SUCCEEDED: frozenset(),
    S.FAILED: frozenset(),
    S.ESCALATED: frozenset(),
    S.EXPIRED: frozenset(),
}


def validate_transition(current: S, target: S) -> None:
    if target not in TRANSITIONS.get(current, frozenset()):
        raise InvalidTransitionError(f"cannot transition ProcessingRun from {current} to {target}")
