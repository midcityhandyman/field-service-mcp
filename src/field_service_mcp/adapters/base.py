"""The adapter interface every vendor integration implements.

The MCP tools only talk to this interface. An adapter's job is narrow:
call the vendor, translate payloads to and from the domain models in
``models.py``, and raise the errors in ``errors.py``. Anything that can be
computed from these calls (schedule views, open slots) lives above the
adapter so that every vendor gets it for free.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ..models import (
    Customer,
    CustomerCreate,
    Job,
    JobCreate,
    JobStatus,
    Page,
    Quote,
    QuoteCreate,
    QuoteStatus,
    Technician,
    Visit,
    VisitCreate,
)

MAX_PAGE_SIZE = 100


class FieldServiceAdapter(ABC):
    """Vendor-neutral access to a field service management system."""

    #: Short label used in logs and in the server_info tool.
    name: str = "unnamed"

    # -- customers ---------------------------------------------------------- #

    @abstractmethod
    async def list_customers(self, *, search: str | None = None, limit: int = 25, offset: int = 0) -> Page[Customer]:
        """Return customers, optionally filtered by a free text search."""

    @abstractmethod
    async def get_customer(self, customer_id: str) -> Customer:
        """Return one customer or raise NotFoundError."""

    @abstractmethod
    async def create_customer(self, data: CustomerCreate) -> Customer:
        """Create a customer and return the stored record."""

    # -- jobs --------------------------------------------------------------- #

    @abstractmethod
    async def list_jobs(
        self,
        *,
        customer_id: str | None = None,
        status: JobStatus | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Page[Job]:
        """Return jobs, newest first."""

    @abstractmethod
    async def get_job(self, job_id: str) -> Job:
        """Return one job or raise NotFoundError."""

    @abstractmethod
    async def create_job(self, data: JobCreate) -> Job:
        """Create a job for an existing customer."""

    # -- visits ------------------------------------------------------------- #

    @abstractmethod
    async def list_visits(
        self,
        *,
        job_id: str | None = None,
        technician_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Page[Visit]:
        """Return visits overlapping the [start, end) window, earliest first."""

    @abstractmethod
    async def create_visit(self, data: VisitCreate) -> Visit:
        """Book a visit. Raise ConflictError when the technician is already booked."""

    # -- quotes ------------------------------------------------------------- #

    @abstractmethod
    async def list_quotes(
        self,
        *,
        customer_id: str | None = None,
        status: QuoteStatus | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Page[Quote]:
        """Return quotes, newest first."""

    @abstractmethod
    async def get_quote(self, quote_id: str) -> Quote:
        """Return one quote or raise NotFoundError."""

    @abstractmethod
    async def create_quote(self, data: QuoteCreate) -> Quote:
        """Create a draft quote."""

    # -- people ------------------------------------------------------------- #

    @abstractmethod
    async def list_technicians(self) -> list[Technician]:
        """Return everyone who can be assigned to a visit."""

    # -- lifecycle ---------------------------------------------------------- #

    async def aclose(self) -> None:
        """Release network resources. Safe to call more than once."""
        return None

    # -- helpers shared by all adapters ------------------------------------- #

    async def list_all_visits(
        self,
        *,
        technician_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        hard_cap: int = 1000,
    ) -> list[Visit]:
        """Walk every page of list_visits. Used by the schedule views."""
        collected: list[Visit] = []
        offset = 0
        while len(collected) < hard_cap:
            page = await self.list_visits(
                technician_id=technician_id, start=start, end=end, limit=MAX_PAGE_SIZE, offset=offset
            )
            collected.extend(page.items)
            if not page.items or not page.has_more:
                break
            offset += len(page.items)
        return collected[:hard_cap]
