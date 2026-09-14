"""Customer resolution: deterministic, no account system. A first-time
caller (any channel, not just chat) supplies a name and a contact method;
returning callers are matched by exact contact within the business. No
password, no login, no session -- identity here is "we can reach you at
this contact," which is all the appointment workflow actually needs.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Customer


async def find_or_create_customer(
    db: AsyncSession, *, business_id: UUID, name: str, contact: str
) -> Customer:
    """Exact-match on contact within the business. Deliberately simple for
    MVP: no normalization of phone/email formatting, so "555-1234" and
    "5551234" are treated as different contacts. Acceptable now; revisit
    if it causes real duplicate-customer friction."""
    existing = await db.scalar(
        select(Customer).where(Customer.business_id == business_id, Customer.contact == contact)
    )
    if existing is not None:
        return existing

    customer = Customer(business_id=business_id, name=name, contact=contact)
    db.add(customer)
    await db.flush()
    return customer
