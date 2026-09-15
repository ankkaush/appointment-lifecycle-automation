"""HTTP boundary for the domain layer. Routers only: they parse requests,
call into app.domain, and translate DomainError -> HTTPException. No
booking, availability, or validation logic lives here — that all stays in
app/domain so it never depends on FastAPI.
"""

from datetime import UTC, datetime, time
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import (
    authenticate_for_rotation,
    generate_api_key,
    hash_api_key,
    require_business_api_key,
)
from app.core.db import get_db
from app.domain import availability, booking
from app.domain.exceptions import (
    DomainError,
    EntityNotFoundError,
    SlotUnavailableError,
    StaffCannotPerformServiceError,
)
from app.domain.models import (
    Business,
    Customer,
    Service,
    StaffResource,
    StaffWorkingHours,
    staff_services,
)
from app.domain.schemas import (
    AppointmentOut,
    BookingRequest,
    BusinessCreate,
    BusinessCreatedOut,
    BusinessOut,
    CustomerCreate,
    CustomerOut,
    ServiceCreate,
    ServiceOut,
    SlotOut,
    StaffCreate,
    StaffOut,
    WorkingHoursCreate,
)

router = APIRouter()


def _created_out(business: Business, api_key: str) -> BusinessCreatedOut:
    return BusinessCreatedOut(**BusinessOut.model_validate(business).model_dump(), api_key=api_key)


_ERROR_STATUS: tuple[tuple[type[DomainError], int], ...] = (
    (EntityNotFoundError, status.HTTP_404_NOT_FOUND),
    (StaffCannotPerformServiceError, status.HTTP_422_UNPROCESSABLE_ENTITY),
    (SlotUnavailableError, status.HTTP_409_CONFLICT),
)


def _as_http_error(exc: DomainError) -> HTTPException:
    for exc_type, code in _ERROR_STATUS:
        if isinstance(exc, exc_type):
            return HTTPException(status_code=code, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# --- Business configuration: services, staff, hours, eligibility --------
# Deliberately plain CRUD. This is config/data, not workflow — the
# baseline's requirement that services/staff/hours/policy stay
# configuration rather than hardcoded logic.


@router.post("/businesses", response_model=BusinessCreatedOut, status_code=201)
async def create_business(
    payload: BusinessCreate, db: AsyncSession = Depends(get_db)
) -> BusinessCreatedOut:
    api_key = generate_api_key()
    obj = Business(**payload.model_dump(), api_key_hash=hash_api_key(api_key))
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return _created_out(obj, api_key)


@router.post("/businesses/{business_id}/rotate-api-key", response_model=BusinessCreatedOut)
async def rotate_api_key(
    business_id: UUID,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
) -> BusinessCreatedOut:
    business = await authenticate_for_rotation(db, business_id, authorization)
    api_key = generate_api_key()
    business.api_key_hash = hash_api_key(api_key)
    await db.commit()
    await db.refresh(business)
    return _created_out(business, api_key)


@router.post("/services", response_model=ServiceOut, status_code=201)
async def create_service(
    payload: ServiceCreate,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
) -> Service:
    await require_business_api_key(db, payload.business_id, authorization)
    obj = Service(**payload.model_dump())
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


@router.post("/staff", response_model=StaffOut, status_code=201)
async def create_staff(
    payload: StaffCreate,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
) -> StaffResource:
    await require_business_api_key(db, payload.business_id, authorization)
    obj = StaffResource(**payload.model_dump())
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


@router.post("/staff/{staff_id}/services/{service_id}", status_code=204)
async def link_staff_service(
    staff_id: UUID,
    service_id: UUID,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
) -> None:
    staff = await db.get(StaffResource, staff_id)
    if staff is None:
        raise HTTPException(status_code=404, detail="StaffResource not found")
    await require_business_api_key(db, staff.business_id, authorization)
    await db.execute(staff_services.insert().values(staff_id=staff_id, service_id=service_id))
    await db.commit()


@router.post("/staff/{staff_id}/working-hours", status_code=204)
async def add_working_hours(
    staff_id: UUID,
    payload: WorkingHoursCreate,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
) -> None:
    staff = await db.get(StaffResource, staff_id)
    if staff is None:
        raise HTTPException(status_code=404, detail="StaffResource not found")
    await require_business_api_key(db, staff.business_id, authorization)
    obj = StaffWorkingHours(
        staff_id=staff_id,
        weekday=payload.weekday,
        start_time=time.fromisoformat(payload.start_time),
        end_time=time.fromisoformat(payload.end_time),
    )
    db.add(obj)
    await db.commit()


@router.post("/customers", response_model=CustomerOut, status_code=201)
async def create_customer(
    payload: CustomerCreate,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
) -> Customer:
    await require_business_api_key(db, payload.business_id, authorization)
    obj = Customer(**payload.model_dump())
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


# --- Availability & booking ----------------------------------------------


@router.get("/availability", response_model=list[SlotOut])
async def get_availability(
    business_id: UUID,
    service_id: UUID,
    staff_id: UUID,
    window_start: datetime = Query(..., description="ISO 8601, must include a UTC offset"),
    window_end: datetime = Query(..., description="ISO 8601, must include a UTC offset"),
    db: AsyncSession = Depends(get_db),
) -> list[SlotOut]:
    business = await db.get(Business, business_id)
    service = await db.get(Service, service_id)
    staff = await db.get(StaffResource, staff_id)
    if business is None or service is None or staff is None:
        raise HTTPException(status_code=404, detail="Business, service, or staff not found")

    slots = await availability.list_available_slots(
        db,
        business=business,
        service=service,
        staff=staff,
        window_start=window_start,
        window_end=window_end,
        now=datetime.now(UTC),
    )
    return [SlotOut(start_at=s.start_at, end_at=s.end_at) for s in slots]


@router.post("/appointments", response_model=AppointmentOut, status_code=201)
async def create_appointment(
    payload: BookingRequest,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
):
    # A direct, manual booking (e.g. a staff member booking a walk-in) --
    # distinct from the customer-facing /v1/requests conversational flow,
    # which never calls this endpoint and stays unauthenticated.
    await require_business_api_key(db, payload.business_id, authorization)
    try:
        return await booking.book_appointment(db, payload)
    except DomainError as exc:
        raise _as_http_error(exc) from exc
