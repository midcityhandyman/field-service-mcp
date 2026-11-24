"""Contract every adapter must honour, whatever vendor sits behind it.

To check a new adapter, add a factory for it to ADAPTERS. The factory must
return an adapter pointed at a sandbox or fake that starts with at least one
customer and one technician, and it must be safe to write to.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from field_service_mcp.adapters import FieldServiceAdapter, MockRestAdapter
from field_service_mcp.errors import AdapterError, ConflictError, NotFoundError
from field_service_mcp.models import (
    CustomerCreate,
    JobCreate,
    JobStatus,
    Page,
    QuoteCreate,
    QuoteLineItem,
    QuoteStatus,
    VisitCreate,
    VisitStatus,
)
from in_memory_adapter import InMemoryAdapter

pytestmark = pytest.mark.anyio

ADAPTERS = {
    "mock": lambda: MockRestAdapter(backoff_seconds=0),
    "in-memory": InMemoryAdapter,
}

# Far from the seeded schedule so the contract run never collides with it.
SLOT_START = datetime(2031, 6, 2, 9, 0)
SLOT_END = datetime(2031, 6, 2, 11, 0)


@pytest.fixture(params=sorted(ADAPTERS))
async def any_adapter(request):
    instance: FieldServiceAdapter = ADAPTERS[request.param]()
    yield instance
    await instance.aclose()


async def test_lists_return_pages_that_respect_limit_and_offset(any_adapter):
    for call in (any_adapter.list_customers, any_adapter.list_jobs, any_adapter.list_visits, any_adapter.list_quotes):
        page = await call(limit=1, offset=0)
        assert isinstance(page, Page)
        assert len(page.items) <= 1
        assert page.total >= len(page.items)
        assert (page.limit, page.offset) == (1, 0)
        beyond = await call(limit=1, offset=page.total + 5)
        assert beyond.items == []


async def test_unknown_ids_raise_not_found(any_adapter):
    for call in (any_adapter.get_customer, any_adapter.get_job, any_adapter.get_quote):
        with pytest.raises(NotFoundError):
            await call("definitely-not-a-real-id")


async def test_create_then_read_back(any_adapter):
    customer = await any_adapter.create_customer(CustomerCreate(name="Contract Check", phone="555-0199"))
    assert (await any_adapter.get_customer(customer.id)).name == "Contract Check"
    found = await any_adapter.list_customers(search="contract check")
    assert customer.id in [item.id for item in found.items]

    job = await any_adapter.create_job(JobCreate(customer_id=customer.id, title="Contract job"))
    assert job.status is JobStatus.OPEN
    assert job.customer_id == customer.id
    assert job.id in [item.id for item in (await any_adapter.list_jobs(customer_id=customer.id)).items]

    quote = await any_adapter.create_quote(QuoteCreate(customer_id=customer.id, job_id=job.id, line_items=[
        QuoteLineItem(description="Part", quantity=3, unit_price=19.99),
        QuoteLineItem(description="Labor", quantity=0.5, unit_price=90),
    ]))
    assert quote.status is QuoteStatus.DRAFT
    assert quote.total == 104.97
    assert (await any_adapter.get_quote(quote.id)).line_items[0].unit_price == 19.99


async def test_jobs_cannot_be_created_for_unknown_customers(any_adapter):
    with pytest.raises(AdapterError):
        await any_adapter.create_job(JobCreate(customer_id="definitely-not-a-real-id", title="Orphan"))


async def test_visits_double_booking_and_window_semantics(any_adapter):
    customer = (await any_adapter.list_customers(limit=1)).items[0]
    technician = (await any_adapter.list_technicians())[0]
    job = await any_adapter.create_job(JobCreate(customer_id=customer.id, title="Contract visit"))

    visit = await any_adapter.create_visit(VisitCreate(
        job_id=job.id, technician_id=technician.id, start=SLOT_START, end=SLOT_END))
    assert visit.status is VisitStatus.BOOKED
    assert (visit.start, visit.end) == (SLOT_START, SLOT_END)

    with pytest.raises(ConflictError):
        await any_adapter.create_visit(VisitCreate(
            job_id=job.id, technician_id=technician.id,
            start=datetime(2031, 6, 2, 10, 0), end=datetime(2031, 6, 2, 12, 0)))

    # Back to back is allowed: windows are half open, [start, end).
    await any_adapter.create_visit(VisitCreate(
        job_id=job.id, technician_id=technician.id, start=SLOT_END, end=datetime(2031, 6, 2, 12, 0)))

    overlapping = await any_adapter.list_visits(
        technician_id=technician.id, start=datetime(2031, 6, 2, 10, 0), end=datetime(2031, 6, 2, 10, 30))
    assert [item.id for item in overlapping.items] == [visit.id]

    touching = await any_adapter.list_visits(
        technician_id=technician.id, start=datetime(2031, 6, 2, 7, 0), end=SLOT_START)
    assert touching.items == []

    everything = await any_adapter.list_all_visits(technician_id=technician.id, start=SLOT_START,
                                                   end=datetime(2031, 6, 3))
    assert [item.start for item in everything] == sorted(item.start for item in everything)
    assert len(everything) == 2
