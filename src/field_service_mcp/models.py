"""Vendor-neutral domain models.

Every adapter translates its vendor's payloads into these shapes, so the MCP
tools (and the agents calling them) never see vendor field names.

Conventions:
- ids are opaque strings owned by the vendor system
- datetimes are naive local times in ISO 8601 (the business's own time zone)
- money is a float in major currency units, rounded to 2 places
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

T = TypeVar("T")


class JobStatus(str, Enum):
    OPEN = "open"
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class JobPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class VisitStatus(str, Enum):
    BOOKED = "booked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class QuoteStatus(str, Enum):
    DRAFT = "draft"
    SENT = "sent"
    APPROVED = "approved"
    DECLINED = "declined"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------- #
# Read models
# --------------------------------------------------------------------------- #


class Address(_Model):
    line1: str = Field(min_length=1, max_length=120)
    city: str = Field(min_length=1, max_length=80)
    region: str = Field(min_length=1, max_length=40, description="State or province code")
    postal_code: str = Field(min_length=1, max_length=20)


class Customer(_Model):
    id: str
    name: str
    phone: str | None = None
    email: str | None = None
    address: Address | None = None
    created_at: datetime | None = None


class Technician(_Model):
    id: str
    name: str
    skills: list[str] = Field(default_factory=list)


class Job(_Model):
    id: str
    customer_id: str
    title: str
    description: str = ""
    status: JobStatus
    priority: JobPriority = JobPriority.NORMAL
    created_at: datetime | None = None


class Visit(_Model):
    id: str
    job_id: str
    technician_id: str
    start: datetime
    end: datetime
    status: VisitStatus
    notes: str = ""


class QuoteLineItem(_Model):
    description: str = Field(min_length=1, max_length=200)
    quantity: float = Field(gt=0, le=10_000)
    unit_price: float = Field(ge=0, le=1_000_000, description="Price per unit in major currency units")

    @property
    def line_total(self) -> float:
        return round(self.quantity * self.unit_price, 2)


class Quote(_Model):
    id: str
    customer_id: str
    job_id: str | None = None
    status: QuoteStatus
    line_items: list[QuoteLineItem]
    total: float
    created_at: datetime | None = None


class Page(_Model, Generic[T]):
    """One page of a list result."""

    items: list[T]
    total: int = Field(description="Total matching records in the vendor system")
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


# --------------------------------------------------------------------------- #
# Write models (inputs to create_* calls)
# --------------------------------------------------------------------------- #


class CustomerCreate(_Model):
    name: str = Field(min_length=1, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    address: Address | None = None

    @field_validator("email")
    @classmethod
    def _email_shape(cls, value: str | None) -> str | None:
        if value is not None and ("@" not in value or " " in value):
            raise ValueError("email must look like name@domain")
        return value


class JobCreate(_Model):
    customer_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    priority: JobPriority = JobPriority.NORMAL


class VisitCreate(_Model):
    job_id: str = Field(min_length=1)
    technician_id: str = Field(min_length=1)
    start: datetime
    end: datetime
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _window(self) -> "VisitCreate":
        if self.start.tzinfo is not None or self.end.tzinfo is not None:
            raise ValueError("start and end must be naive local datetimes (no UTC offset)")
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.end - self.start > timedelta(hours=12):
            raise ValueError("a single visit cannot be longer than 12 hours")
        return self


class QuoteCreate(_Model):
    customer_id: str = Field(min_length=1)
    job_id: str | None = None
    line_items: list[QuoteLineItem] = Field(min_length=1, max_length=100)


# --------------------------------------------------------------------------- #
# Schedule views (computed above the adapter, see schedule.py)
# --------------------------------------------------------------------------- #


class ScheduleEntry(_Model):
    """A visit with the job and customer context an agent needs to talk about it."""

    visit_id: str
    job_id: str
    job_title: str | None = None
    customer_id: str | None = None
    customer_name: str | None = None
    start: datetime
    end: datetime
    status: VisitStatus


class TechnicianDay(_Model):
    technician_id: str
    technician_name: str
    day: date
    entries: list[ScheduleEntry]
    booked_minutes: int


class Schedule(_Model):
    date_from: date
    date_to: date
    days: list[TechnicianDay]


class OpenSlot(_Model):
    technician_id: str
    technician_name: str
    start: datetime
    end: datetime
    minutes: int


class OpenSlots(_Model):
    day: date
    duration_minutes: int
    workday_start: str
    workday_end: str
    slots: list[OpenSlot]
