"""Domain-level errors. These carry no HTTP knowledge — the API layer
translates them into status codes; nothing in domain/ imports FastAPI.
"""


class DomainError(Exception):
    """Base class for every error the domain layer raises deliberately."""


class EntityNotFoundError(DomainError):
    def __init__(self, entity: str, entity_id: object) -> None:
        self.entity = entity
        self.entity_id = entity_id
        super().__init__(f"{entity} {entity_id} not found")


class StaffCannotPerformServiceError(DomainError):
    """Raised when a booking is attempted for a (staff, service) pair that
    isn't in the staff_services eligibility table."""


class SlotUnavailableError(DomainError):
    """The requested time is not available — outside working hours, inside
    a blocked period, already booked, or outside the booking-notice /
    booking-horizon window. Also the error a concurrent booking race
    resolves to for the losing request."""


class AppointmentNotModifiableError(DomainError):
    """Raised by cancel_appointment / reschedule_appointment when the
    appointment isn't BOOKED, or is inside Business.min_reschedule_notice_hours
    of its start_at."""
