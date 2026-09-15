"""Per-business API key authentication (Phase 10). Not a general identity
or session system -- one shared secret per Business, the same pattern a
B2B API like Stripe or Twilio uses for a caller that's another system,
not a human logging in. Required on every endpoint that reveals or
mutates data scoped to one business and isn't part of the customer-facing
conversational flow (start_request / reply / confirm) -- customers never
hold a business's key, and gating those would break the chat channel
entirely.

Deliberately not building account/session/password infrastructure here:
a business is the caller's identity, and the key is bearer credential
enough for that. Full user accounts are a different, larger feature this
phase doesn't attempt.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Business


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    """SHA-256, not bcrypt/argon2: those defend a low-entropy, human-
    chosen password against offline brute-forcing by being deliberately
    slow. This hashes a 256-bit, cryptographically random token -- there
    is nothing to brute-force, and a slow hash would only cost real
    requests real latency for no security benefit."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    return token or None


async def require_business_api_key(
    db: AsyncSession, business_id: UUID, authorization: str | None
) -> Business:
    """Looks up `business_id` and verifies `authorization` carries its
    current key. Raises 404 for an unknown business (same as any other
    not-found here -- an invalid key on a real business and a made-up
    business_id both just fail), 401 for a missing/wrong/not-yet-issued
    key. Returns the Business on success, for handlers that need it
    anyway."""
    business = await db.get(Business, business_id)
    if business is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Business not found")

    if business.api_key_hash is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No API key configured for this business -- call rotate-api-key to issue one.",
        )

    token = _extract_bearer_token(authorization)
    if token is None or not hmac.compare_digest(hash_api_key(token), business.api_key_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    return business


async def authenticate_for_rotation(
    db: AsyncSession, business_id: UUID, authorization: str | None
) -> Business:
    """Same check as require_business_api_key, except a business with no
    key yet (api_key_hash is None) is let through unauthenticated --
    exactly once, as the bootstrap path for a business created before
    this column existed, or whose key was never issued. Every business
    that already has a key still needs to present it to rotate."""
    business = await db.get(Business, business_id)
    if business is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Business not found")

    if business.api_key_hash is None:
        return business

    token = _extract_bearer_token(authorization)
    if token is None or not hmac.compare_digest(hash_api_key(token), business.api_key_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    return business
