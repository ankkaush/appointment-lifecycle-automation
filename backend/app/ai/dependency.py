"""Composition root for the AI layer: the one place that decides which
Interpreter implementation the running app uses. Routes depend on
`get_interpreter`, never on a concrete provider — tests override this
FastAPI dependency with FakeInterpreter instead of touching call sites.
"""

from __future__ import annotations

from functools import lru_cache

from app.ai.interpreter import Interpreter
from app.ai.providers.claude import ClaudeInterpreter
from app.core.config import get_settings


@lru_cache
def get_interpreter() -> Interpreter:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Set it in .env to use the AI "
            "interpretation endpoint, or override the get_interpreter "
            "dependency (e.g. with FakeInterpreter) for local work without a key."
        )
    return ClaudeInterpreter(api_key=settings.anthropic_api_key, model=settings.ai_model)
