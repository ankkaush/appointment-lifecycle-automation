"""A minimal in-memory rate limiter for public, unauthenticated endpoints.

Deliberately simple: a fixed-window counter per client IP, held in
process memory. Proportional to a single-instance MVP -- it resets on
restart and doesn't coordinate across multiple app instances. Real
production rate limiting (shared store, sliding window, per-business
tuning) is explicitly Phase 9/10 hardening scope; this exists now because
this phase is what first exposes a public endpoint that both creates data
(customers) and spends money (AI calls) with no auth in front of it, and
some protection now is better than none while that's true.
"""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import Depends, HTTPException, Request, status


class FixedWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> None:
        now = time.monotonic()
        window_start = now - self.window_seconds
        hits = [t for t in self._hits[key] if t > window_start]
        if len(hits) >= self.max_requests:
            self._hits[key] = hits
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests -- please slow down and try again shortly.",
            )
        hits.append(now)
        self._hits[key] = hits


# One process-wide instance for the public chat endpoints. 20 requests per
# 5 minutes per IP is generous for a real back-and-forth conversation,
# restrictive enough to blunt trivial scripted abuse of a paid AI call.
chat_rate_limiter = FixedWindowRateLimiter(max_requests=20, window_seconds=300)


def get_chat_rate_limiter() -> FixedWindowRateLimiter:
    return chat_rate_limiter


def enforce_chat_rate_limit(
    request: Request, limiter: FixedWindowRateLimiter = Depends(get_chat_rate_limiter)
) -> None:
    client_ip = request.client.host if request.client else "unknown"
    limiter.check(client_ip)
