import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.customers import find_or_create_customer
from app.domain.models import Business


@pytest.mark.asyncio
async def test_creates_new_customer_when_no_match(db: AsyncSession, business: Business) -> None:
    customer = await find_or_create_customer(
        db, business_id=business.id, name="Jamie Lee", contact="jamie@example.com"
    )
    assert customer.name == "Jamie Lee"
    assert customer.contact == "jamie@example.com"


@pytest.mark.asyncio
async def test_returns_existing_customer_on_repeat_contact(
    db: AsyncSession, business: Business
) -> None:
    first = await find_or_create_customer(
        db, business_id=business.id, name="Jamie Lee", contact="jamie@example.com"
    )
    await db.commit()

    second = await find_or_create_customer(
        db,
        business_id=business.id,
        name="Jamie Lee (typo'd differently)",
        contact="jamie@example.com",
    )

    assert second.id == first.id
    # The existing row wins -- a repeat contact doesn't overwrite the name.
    assert second.name == "Jamie Lee"
