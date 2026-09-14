"""A deterministic, no-network Interpreter used by tests and local
development without an API key. Never wired in for the real app — see
app/ai/dependency.py for how the real provider is selected.
"""

from __future__ import annotations

from datetime import date

from app.ai.schemas import AIInvocationMetadata, Intent, InterpretationOutcome, InterpretedRequest


class FakeInterpreter:
    """Canned responses keyed on a substring of the message, so tests stay
    deterministic and free. Falls back to an unrecognized/ambiguous result
    for anything it doesn't match, rather than guessing.
    """

    async def interpret(
        self, message: str, *, today: date, known_services: list[str]
    ) -> InterpretationOutcome:
        text = message.lower()

        if "cancel" in text:
            request = InterpretedRequest(intent=Intent.CANCEL, is_ambiguous=False, confidence=0.9)
        elif "reschedule" in text or "move my" in text:
            request = InterpretedRequest(
                intent=Intent.RESCHEDULE, is_ambiguous=False, confidence=0.85
            )
        elif any(word in text for word in ("book", "appointment", "haircut")):
            service_hint = next((s for s in known_services if s.lower() in text), None)
            request = InterpretedRequest(
                intent=Intent.BOOK,
                service_hint=service_hint,
                is_ambiguous=False,
                confidence=0.8,
            )
        else:
            request = InterpretedRequest(
                intent=Intent.UNKNOWN,
                is_ambiguous=True,
                ambiguity_reason="fake interpreter did not recognize this message",
                confidence=0.2,
            )

        metadata = AIInvocationMetadata(
            provider="fake", model="fake-v1", latency_ms=0.0, input_tokens=0, output_tokens=0
        )
        return InterpretationOutcome(request=request, metadata=metadata)
