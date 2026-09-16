"""Phase 11: the operator dashboard's read-only endpoints. These assemble
views over data the existing workflow/jobs code already writes -- built
directly via the ORM here (not through the chat/AI flow) so these tests
stay fast and make zero live Anthropic calls, while still proving the
endpoints correctly join, scope, and sort real persisted rows.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import generate_api_key, hash_api_key
from app.domain.models import (
    Appointment,
    AppointmentStatus,
    Business,
    CalendarSyncStatus,
    Customer,
    Service,
    StaffResource,
)
from app.main import app
from app.workflow.models import (
    AuditEvent,
    EscalationCase,
    EscalationStatus,
    Notification,
    NotificationStatus,
    ProcessingRun,
    ProcessingRunKind,
    ProcessingRunState,
    ScheduledJob,
    ScheduledJobStatus,
    ScheduledJobType,
    WorkflowStep,
)


def _auth(business: Business) -> dict[str, str]:
    return {"Authorization": f"Bearer {business.api_key}"}


async def _make_appointment(
    db: AsyncSession,
    business: Business,
    service: Service,
    staff: StaffResource,
    customer: Customer,
    *,
    status: AppointmentStatus = AppointmentStatus.BOOKED,
    start_at: datetime | None = None,
) -> Appointment:
    start_at = start_at or (datetime.now(UTC) + timedelta(days=1))
    appointment = Appointment(
        business_id=business.id,
        service_id=service.id,
        staff_id=staff.id,
        customer_id=customer.id,
        start_at=start_at,
        end_at=start_at + timedelta(minutes=service.duration_minutes),
        status=status,
        idempotency_key=str(uuid4()),
        calendar_sync_status=CalendarSyncStatus.SYNCED,
    )
    db.add(appointment)
    await db.flush()
    return appointment


@pytest.mark.asyncio
async def test_list_appointments_requires_api_key(business: Business) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/appointments", params={"business_id": str(business.id)})
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_appointments_returns_joined_human_readable_fields(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _make_appointment(db, business, service, staff, customer)
    await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/appointments", params={"business_id": str(business.id)}, headers=_auth(business)
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    row = body[0]
    assert row["id"] == str(appointment.id)
    assert row["customer_name"] == customer.name
    assert row["service_name"] == service.name
    assert row["staff_name"] == staff.name
    assert row["status"] == "BOOKED"


@pytest.mark.asyncio
async def test_list_appointments_is_scoped_to_the_requested_business(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    other_key = generate_api_key()
    other_business = Business(
        name="Other Salon", timezone="America/New_York", api_key_hash=hash_api_key(other_key)
    )
    db.add(other_business)
    await db.commit()

    await _make_appointment(db, business, service, staff, customer)
    await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/appointments",
            params={"business_id": str(other_business.id)},
            headers={"Authorization": f"Bearer {other_key}"},
        )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_appointment_timeline_assembles_across_sources_in_order(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _make_appointment(db, business, service, staff, customer)
    run = ProcessingRun(
        business_id=business.id,
        customer_id=customer.id,
        raw_message="Book me a haircut",
        state=ProcessingRunState.SUCCEEDED,
        kind=ProcessingRunKind.CUSTOMER_INITIATED,
        resulting_appointment_id=appointment.id,
        messages=[],
    )
    db.add(run)
    await db.flush()

    t0 = datetime.now(UTC)
    db.add(
        WorkflowStep(
            processing_run_id=run.id,
            step_name="received",
            created_at=t0,
        )
    )
    db.add(
        AuditEvent(
            processing_run_id=run.id,
            entity_type="Appointment",
            entity_id=appointment.id,
            to_state="BOOKED",
            reason="booked via workflow",
            created_at=t0 + timedelta(seconds=1),
        )
    )
    db.add(
        Notification(
            processing_run_id=run.id,
            appointment_id=appointment.id,
            channel="console",
            to_contact=customer.contact,
            subject="Appointment confirmed",
            body="...",
            status=NotificationStatus.SENT,
            created_at=t0 + timedelta(seconds=2),
        )
    )
    db.add(
        ScheduledJob(
            job_type=ScheduledJobType.SEND_REMINDER,
            run_at=appointment.start_at - timedelta(hours=24),
            payload={"appointment_id": str(appointment.id)},
            status=ScheduledJobStatus.PENDING,
            created_at=t0 + timedelta(seconds=3),
        )
    )
    await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/v1/appointments/{appointment.id}/timeline",
            params={"business_id": str(business.id)},
            headers=_auth(business),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["appointment"]["id"] == str(appointment.id)

    sources = [e["source"] for e in body["entries"]]
    assert sources == ["workflow_step", "audit_event", "notification", "scheduled_job"]

    labels = [e["label"] for e in body["entries"]]
    assert labels == [
        "Customer request received",
        "booked via workflow",
        "Appointment confirmed → SENT",
        f"Reminder job scheduled for {(appointment.start_at - timedelta(hours=24)).isoformat()}",
    ]

    # Entries are in chronological order regardless of which table they
    # came from.
    timestamps = [e["at"] for e in body["entries"]]
    assert timestamps == sorted(timestamps)


@pytest.mark.asyncio
async def test_appointment_timeline_404_for_wrong_business(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _make_appointment(db, business, service, staff, customer)
    await db.commit()

    other_key = generate_api_key()
    other_business = Business(
        name="Other Salon", timezone="America/New_York", api_key_hash=hash_api_key(other_key)
    )
    db.add(other_business)
    await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/v1/appointments/{appointment.id}/timeline",
            params={"business_id": str(other_business.id)},
            headers={"Authorization": f"Bearer {other_key}"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_customers_shows_upcoming_and_latest_status(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    past = await _make_appointment(
        db,
        business,
        service,
        staff,
        customer,
        status=AppointmentStatus.NO_SHOW,
        start_at=datetime.now(UTC) - timedelta(days=1),
    )
    upcoming = await _make_appointment(
        db,
        business,
        service,
        staff,
        customer,
        status=AppointmentStatus.BOOKED,
        start_at=datetime.now(UTC) + timedelta(days=2),
    )
    await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/customers", params={"business_id": str(business.id)}, headers=_auth(business)
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    row = body[0]
    assert row["total_appointments"] == 2
    assert row["upcoming_appointment_at"] == upcoming.start_at.isoformat().replace("+00:00", "Z")
    # latest by start_at is the upcoming one, not necessarily insertion order
    assert row["latest_status"] == "BOOKED"
    assert past.id  # sanity: fixture created successfully


@pytest.mark.asyncio
async def test_list_notifications_scoped_to_business_and_ordered_desc(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _make_appointment(db, business, service, staff, customer)
    t0 = datetime.now(UTC)
    db.add(
        Notification(
            appointment_id=appointment.id,
            channel="email",
            to_contact=customer.contact,
            subject="Appointment confirmed",
            body="...",
            status=NotificationStatus.SENT,
            created_at=t0,
        )
    )
    db.add(
        Notification(
            appointment_id=appointment.id,
            channel="email",
            to_contact=customer.contact,
            subject="Appointment reminder",
            body="...",
            status=NotificationStatus.FAILED,
            created_at=t0 + timedelta(minutes=5),
        )
    )
    await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/notifications", params={"business_id": str(business.id)}, headers=_auth(business)
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    # Most recent first.
    assert body[0]["subject"] == "Appointment reminder"
    assert body[0]["status"] == "FAILED"
    assert body[1]["subject"] == "Appointment confirmed"
    assert all(row["customer_name"] == customer.name for row in body)


@pytest.mark.asyncio
async def test_list_jobs_scoped_to_business(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    appointment = await _make_appointment(db, business, service, staff, customer)
    db.add(
        ScheduledJob(
            job_type=ScheduledJobType.SEND_REMINDER,
            run_at=appointment.start_at - timedelta(hours=24),
            payload={"appointment_id": str(appointment.id)},
            status=ScheduledJobStatus.PENDING,
        )
    )
    await db.commit()

    other_key = generate_api_key()
    other_business = Business(
        name="Other Salon", timezone="America/New_York", api_key_hash=hash_api_key(other_key)
    )
    db.add(other_business)
    await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        mine = await client.get(
            "/v1/jobs", params={"business_id": str(business.id)}, headers=_auth(business)
        )
        theirs = await client.get(
            "/v1/jobs",
            params={"business_id": str(other_business.id)},
            headers={"Authorization": f"Bearer {other_key}"},
        )
    assert mine.status_code == 200
    assert len(mine.json()) == 1
    assert mine.json()[0]["customer_name"] == customer.name

    assert theirs.status_code == 200
    assert theirs.json() == []


@pytest.mark.asyncio
async def test_overview_counts_are_accurate_and_business_scoped(
    db: AsyncSession, business: Business, service: Service, staff: StaffResource, customer: Customer
) -> None:
    now = datetime.now(UTC)
    await _make_appointment(
        db,
        business,
        service,
        staff,
        customer,
        status=AppointmentStatus.BOOKED,
        start_at=now + timedelta(hours=2),
    )
    await _make_appointment(
        db,
        business,
        service,
        staff,
        customer,
        status=AppointmentStatus.BOOKED,
        start_at=now + timedelta(days=5),
    )
    no_show = await _make_appointment(
        db,
        business,
        service,
        staff,
        customer,
        status=AppointmentStatus.NO_SHOW,
        start_at=now - timedelta(days=1),
    )
    db.add(
        Notification(
            appointment_id=no_show.id,
            channel="email",
            to_contact=customer.contact,
            subject="We missed you",
            body="...",
            status=NotificationStatus.SENT,
        )
    )
    db.add(
        Notification(
            appointment_id=no_show.id,
            channel="email",
            to_contact=customer.contact,
            subject="Appointment confirmed",
            body="...",
            status=NotificationStatus.FAILED,
        )
    )
    db.add(
        ScheduledJob(
            job_type=ScheduledJobType.SEND_REMINDER,
            run_at=now + timedelta(hours=1),
            payload={"appointment_id": str(no_show.id)},
            status=ScheduledJobStatus.PENDING,
        )
    )
    run = ProcessingRun(
        business_id=business.id,
        customer_id=customer.id,
        raw_message="???",
        state=ProcessingRunState.ESCALATED,
        messages=[],
    )
    db.add(run)
    await db.flush()
    db.add(EscalationCase(processing_run_id=run.id, reason="unclear", status=EscalationStatus.OPEN))
    await db.commit()

    # A second business's data must never leak into the first business's counts.
    other_key = generate_api_key()
    other_business = Business(
        name="Other Salon", timezone="America/New_York", api_key_hash=hash_api_key(other_key)
    )
    db.add(other_business)
    await db.commit()
    other_customer = Customer(business_id=other_business.id, name="Other", contact="o@example.com")
    db.add(other_customer)
    await db.flush()
    await _make_appointment(db, other_business, service, staff, other_customer)
    await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/overview", params={"business_id": str(business.id)}, headers=_auth(business)
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["upcoming_appointments"] == 2
    assert body["no_shows"] == 1
    assert body["open_escalations"] == 1
    assert body["notifications_sent"] == 1
    assert body["notifications_failed"] == 1
    assert body["pending_reminders"] == 1
