import pytest
from fastapi import HTTPException

from app.api.rate_limit import FixedWindowRateLimiter


def test_allows_up_to_the_limit() -> None:
    limiter = FixedWindowRateLimiter(max_requests=3, window_seconds=60)
    for _ in range(3):
        limiter.check("1.2.3.4")  # should not raise


def test_blocks_over_the_limit() -> None:
    limiter = FixedWindowRateLimiter(max_requests=2, window_seconds=60)
    limiter.check("1.2.3.4")
    limiter.check("1.2.3.4")
    with pytest.raises(HTTPException) as exc_info:
        limiter.check("1.2.3.4")
    assert exc_info.value.status_code == 429


def test_limits_are_independent_per_key() -> None:
    limiter = FixedWindowRateLimiter(max_requests=1, window_seconds=60)
    limiter.check("1.2.3.4")
    limiter.check("5.6.7.8")  # different key, should not raise


def test_window_expires() -> None:
    # Uses time.monotonic() internally (the right clock for a rate
    # limiter -- immune to wall-clock adjustments), so we simulate the
    # passage of time by rewriting the recorded hit timestamp directly
    # rather than sleeping or mocking a clock that isn't in play.
    limiter = FixedWindowRateLimiter(max_requests=1, window_seconds=60)
    limiter.check("1.2.3.4")
    limiter._hits["1.2.3.4"] = [limiter._hits["1.2.3.4"][0] - 120]
    limiter.check("1.2.3.4")  # should not raise -- the old hit has aged out
