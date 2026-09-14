"""The one seam between the domain/workflow layer and any AI vendor.

Nothing outside app/ai/providers/ should import a vendor SDK — everything
else (routes, and later the Phase 3 workflow engine) depends on this
Protocol instead, so swapping providers means writing one new module under
app/ai/providers/, not touching call sites.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from app.ai.schemas import InterpretationOutcome


class Interpreter(Protocol):
    async def interpret(
        self, message: str, *, today: date, known_services: list[str]
    ) -> InterpretationOutcome: ...
