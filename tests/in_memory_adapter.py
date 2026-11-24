"""A second adapter with no HTTP at all.

It exists to prove the MCP layer depends only on the adapter interface, and
it is the smallest complete example of what a new adapter has to provide.
"""

from __future__ import annotations

from datetime import datetime

from field_service_mcp.adapters import FieldServiceAdapter
from field_service_mcp.config import Settings
from field_service_mcp.errors import ConflictError, NotFoundError
from field_service_mcp.models import (
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
    VisitStatus,
)


def _page(rows: list, limit: int, offset: int) -> Page:
    return Page(items=rows[offset:offset + limit], total=len(rows), limit=limit, offset=offset)


class InMemoryAdapter(FieldServiceAdapter):
    name = "in-memory"

    def __init__(self) -> None:
        self.customers: dict[str, Customer] = {"C1": Customer(id="C1", name="Pat Imaginary")}
        self.jobs: dict[str, Job] = {}
        self.visits: dict[str, Visit] = {}
        self.quotes: dict[str, Quote] = {}
        self.technicians = [Technician(id="X1", name="Drew Stand-In", skills=["general repair"])]

    @classmethod
    def from_settings(cls, settings: Settings) -> "InMemoryAdapter":
        return cls()

    async def list_customers(self, *, search=None, limit=25, offset=0):
        rows = [c for c in self.customers.values() if not search or search.lower() in c.name.lower()]
        return _page(rows, limit, offset)

    async def get_customer(self, customer_id: str) -> Customer:
        if customer_id not in self.customers:
            raise NotFoundError(f"Customer {customer_id} does not exist.")
        return self.customers[customer_id]

    async def create_customer(self, data: CustomerCreate) -> Customer:
        customer = Customer(id=f"C{len(self.customers) + 1}", **data.model_dump())
        self.customers[customer.id] = customer
        return customer

    async def list_jobs(self, *, customer_id=None, status=None, limit=25, offset=0):
        rows = [j for j in self.jobs.values()
                if (customer_id is None or j.customer_id == customer_id) and (status is None or j.status == status)]
        return _page(rows, limit, offset)

    async def get_job(self, job_id: str) -> Job:
        if job_id not in self.jobs:
            raise NotFoundError(f"Job {job_id} does not exist.")
        return self.jobs[job_id]

    async def create_job(self, data: JobCreate) -> Job:
        await self.get_customer(data.customer_id)
        job = Job(id=f"J{len(self.jobs) + 1}", status=JobStatus.OPEN, **data.model_dump())
        self.jobs[job.id] = job
        return job

    async def list_visits(self, *, job_id=None, technician_id=None, start: datetime | None = None,
                          end: datetime | None = None, limit=25, offset=0):
        rows = [v for v in self.visits.values()
                if (job_id is None or v.job_id == job_id)
                and (technician_id is None or v.technician_id == technician_id)
                and (start is None or v.end > start) and (end is None or v.start < end)]
        return _page(sorted(rows, key=lambda v: v.start), limit, offset)

    async def create_visit(self, data: VisitCreate) -> Visit:
        await self.get_job(data.job_id)
        for other in self.visits.values():
            if other.technician_id == data.technician_id and other.start < data.end and other.end > data.start:
                raise ConflictError(f"Technician {data.technician_id} is already booked then.")
        visit = Visit(id=f"V{len(self.visits) + 1}", status=VisitStatus.BOOKED, **data.model_dump())
        self.visits[visit.id] = visit
        return visit

    async def list_quotes(self, *, customer_id=None, status=None, limit=25, offset=0):
        rows = [q for q in self.quotes.values() if customer_id is None or q.customer_id == customer_id]
        return _page(rows, limit, offset)

    async def get_quote(self, quote_id: str) -> Quote:
        if quote_id not in self.quotes:
            raise NotFoundError(f"Quote {quote_id} does not exist.")
        return self.quotes[quote_id]

    async def create_quote(self, data: QuoteCreate) -> Quote:
        total = round(sum(item.line_total for item in data.line_items), 2)
        quote = Quote(id=f"Q{len(self.quotes) + 1}", status=QuoteStatus.DRAFT, total=total, **data.model_dump())
        self.quotes[quote.id] = quote
        return quote

    async def list_technicians(self) -> list[Technician]:
        return self.technicians


class NotAnAdapter:
    """Used to check that the registry refuses classes with the wrong base."""
