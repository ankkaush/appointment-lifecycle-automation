"""Request/response shapes for the domain API. Plain pydantic models —
no vendor SDK types leak in here, and nothing here leaks out into the AI
or channel layers that arrive in later phases.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class BusinessCreate(BaseModel):
    name: str
    timezone: str
    min_booking_notice_minutes: int = 60
    max_booking_horizon_days: int = 60


class BusinessOut(BusinessCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID


class ServiceCreate(BaseModel):
    business_id: UUID
    name: str
    duration_minutes: int = Field(gt=0)
    buffer_minutes: int = Field(ge=0, default=0)


class ServiceOut(ServiceCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    active: bool


class StaffCreate(BaseModel):
    business_id: UUID
    name: str


class StaffOut(StaffCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    active: bool


class CustomerCreate(BaseModel):
    business_id: UUID
    name: str
    contact: str


class CustomerOut(CustomerCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID


class WorkingHoursCreate(BaseModel):
    weekday: int = Field(ge=0, le=6)
    start_time: str  # "HH:MM", local to the business's timezone
    end_time: str


class BookingRequest(BaseModel):
    idempotency_key: str
    business_id: UUID
    service_id: UUID
    staff_id: UUID
    customer_id: UUID
    start_at: datetime


class AppointmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    business_id: UUID
    service_id: UUID
    staff_id: UUID
    customer_id: UUID
    start_at: datetime
    end_at: datetime
    status: str
    idempotency_key: str


class SlotOut(BaseModel):
    start_at: datetime
    end_at: datetime
