"""The one module in this codebase allowed to import the Anthropic SDK.
Everything else depends on app.ai.interpreter.Interpreter — swapping this
for a different vendor means writing one new file here, not touching the
domain, workflow, or API layers.
"""

from __future__ import annotations

import time
from datetime import date

import anthropic

from app.ai.exceptions import AIInterpretationError
from app.ai.schemas import AIInvocationMetadata, InterpretationOutcome, InterpretedRequest

_TOOL_NAME = "record_interpretation"

_TOOL_SCHEMA = {
    "name": _TOOL_NAME,
    "description": (
        "Record the structured interpretation of a customer's appointment-related message."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["book", "reschedule", "cancel", "question", "unknown"],
                "description": "What the customer is trying to do.",
            },
            "service_hint": {
                "type": ["string", "null"],
                "description": (
                    "The service the customer mentioned, in their own words. "
                    "Null if none was mentioned."
                ),
            },
            "date_hint": {
                "type": ["string", "null"],
                "description": (
                    "The date/time phrase the customer used, verbatim "
                    "(e.g. 'next Tuesday', 'tomorrow afternoon'). Null if none given."
                ),
            },
            "resolved_date": {
                "type": ["string", "null"],
                "description": (
                    "Your best-effort resolution of date_hint to a concrete ISO 8601 "
                    "date (YYYY-MM-DD), given today's date. Null if it cannot be "
                    "pinned down to one specific date."
                ),
            },
            "time_preference": {
                "type": ["string", "null"],
                "description": (
                    "Any time-of-day preference, in the customer's own words "
                    "(e.g. 'after 3pm', 'morning'). Null if none given."
                ),
            },
            "is_ambiguous": {
                "type": "boolean",
                "description": (
                    "True if the message does not give enough information to proceed "
                    "safely — e.g. a booking/reschedule request with no usable date, "
                    "contradictory information, or genuinely unclear intent. False for "
                    "a clear cancellation, a clear question, or a booking request with "
                    "a usable (even if approximate) date."
                ),
            },
            "ambiguity_reason": {
                "type": ["string", "null"],
                "description": (
                    "If is_ambiguous is true, a short explanation of what's missing " "or unclear."
                ),
            },
            "confidence": {
                "type": "number",
                "description": (
                    "Your own rough self-assessment of confidence in this "
                    "interpretation, from 0 to 1. Informational only — never used to "
                    "authorize a booking on its own."
                ),
            },
        },
        "required": ["intent", "is_ambiguous", "confidence"],
    },
}


def _system_prompt(today: date, known_services: list[str]) -> str:
    services = ", ".join(known_services) if known_services else "(no services configured)"
    return (
        "You interpret short customer messages sent to an appointment-based "
        "business. You never decide whether a booking happens — a separate "
        "deterministic system checks real availability and business rules "
        "afterwards. Your only job is to extract structure from the message.\n\n"
        f"Today's date is {today.isoformat()}. "
        f"This business's configured services are: {services}.\n\n"
        "Only set resolved_date when date_hint maps to one specific calendar day. "
        "A vague preference like 'sometime next week' should leave resolved_date "
        "null but does not by itself make the message ambiguous for a booking "
        "intent — leave is_ambiguous false and let date_hint carry the vague "
        "preference forward. Only set is_ambiguous true when there truly isn't "
        "enough in the message to act on at all."
    )


class ClaudeInterpreter:
    """Interpreter backed by the Anthropic Messages API, using tool use to
    force schema-conformant structured output."""

    def __init__(self, api_key: str, model: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model

    async def interpret(
        self, message: str, *, today: date, known_services: list[str]
    ) -> InterpretationOutcome:
        started = time.monotonic()
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=512,
                system=_system_prompt(today, known_services),
                messages=[{"role": "user", "content": message}],
                tools=[_TOOL_SCHEMA],
                tool_choice={"type": "tool", "name": _TOOL_NAME},
            )
        except anthropic.APIError as exc:
            raise AIInterpretationError(f"Anthropic API error: {exc}") from exc
        latency_ms = (time.monotonic() - started) * 1000

        tool_use = next((block for block in response.content if block.type == "tool_use"), None)
        if tool_use is None:
            raise AIInterpretationError("Model did not return a structured tool call")

        try:
            request = InterpretedRequest.model_validate(tool_use.input)
        except Exception as exc:  # pydantic.ValidationError, or a malformed shape
            raise AIInterpretationError(f"Model output failed schema validation: {exc}") from exc

        metadata = AIInvocationMetadata(
            provider="anthropic",
            model=self._model,
            latency_ms=latency_ms,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return InterpretationOutcome(request=request, metadata=metadata)
