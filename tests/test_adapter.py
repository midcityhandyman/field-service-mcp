"""The mock adapter: vocabulary mapping, paging, error translation, retries."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from field_service_mcp.adapters import MockRestAdapter
from field_service_mcp.adapters.mock_rest import (
    amount_to_cents,
    cents_to_amount,
    job_from_vendor,
    quote_to_vendor,
)
from field_service_mcp.errors import (
    AuthError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    UpstreamError,
)
from field_service_mcp.models import (
    Address,
    CustomerCreate,
    JobCreate,
    JobPriority,
    JobStatus,
    QuoteCreate,
    QuoteLineItem,
    QuoteStatus,
    VisitCreate,
    VisitStatus,
)

pytestmark = pytest.mark.anyio


def test_money_round_trips_without_float_drift():
    for cents in (0, 1, 1999, 14900, 123456789):
        assert amount_to_cents(cents_to_amount(cents)) == cents
    assert amount_to_cents(19.99) == 1999
    assert amount_to_cents(0.29) == 29


def test_vendor_vocabulary_is_translated():
    job = job_from_vendor({"id": "WO-1", "clientId": "CL-1", "summary": "Fix gate", "details": None,
                           "state": "DONE", "priority": "HIGH", "createdAt": "2030-01-07T12:00:00"})
    assert (job.customer_id, job.title, job.description) == ("CL-1", "Fix gate", "")
    assert job.status is JobStatus.COMPLETED
    assert job.priority is JobPriority.HIGH

    body = quote_to_vendor(QuoteCreate(customer_id="CL-1", line_items=[
        QuoteLineItem(description="Labor", quantity=1.5, unit_price=95.5)]))
    assert body == {"clientId": "CL-1", "workOrderId": None,
                    "lines": [{"label": "Labor", "qty": 1.5, "unitPriceCents": 9550}]}


async def test_list_customers_and_search(adapter):
    page = await adapter.list_customers()
    assert page.total == 6 and len(page.items) == 6
    assert page.items[0].name == "Alex Sample"
    assert page.items[0].address.postal_code == "00001"
    assert page.has_more is False

    hits = await adapter.list_customers(search="555-0104")
    assert [customer.name for customer in hits.items] == ["Taylor Mockwell"]


async def test_offset_paging_is_stitched_across_vendor_pages(adapter):
    everyone = [customer.id for customer in (await adapter.list_customers(limit=100)).items]
    window = await adapter.list_customers(limit=4, offset=3)  # straddles vendor pages 1 and 2
    assert [customer.id for customer in window.items] == everyone[3:6]
    assert (window.total, window.limit, window.offset, window.has_more) == (6, 4, 3, False)

    aligned = await adapter.list_customers(limit=2, offset=2)
    assert [customer.id for customer in aligned.items] == everyone[2:4]
    assert aligned.has_more is True

    odd = await adapter.list_customers(limit=2, offset=1)
    assert [customer.id for customer in odd.items] == everyone[1:3]

    past_the_end = await adapter.list_customers(limit=5, offset=50)
    assert past_the_end.items == [] and past_the_end.total == 6


async def test_job_filters_use_domain_statuses(adapter):
    done = await adapter.list_jobs(status=JobStatus.COMPLETED)
    assert [job.title for job in done.items] == ["Clean and reseal gutters"]
    in_progress = await adapter.list_jobs(status=JobStatus.IN_PROGRESS)
    assert [job.id for job in in_progress.items] == ["WO-2004"]
    newest_first = await adapter.list_jobs()
    assert newest_first.items[0].id == "WO-2008"


async def test_create_customer_job_visit_quote_flow(adapter):
    customer = await adapter.create_customer(CustomerCreate(
        name="Robin Fabricated", phone="555-0142", email="robin.fabricated@example.com",
        address=Address(line1="700 Invented Way", city="Sampletown", region="ZZ", postal_code="00007")))
    assert customer.id == "CL-1007"
    assert (await adapter.get_customer(customer.id)).address.line1 == "700 Invented Way"

    job = await adapter.create_job(JobCreate(customer_id=customer.id, title="Replace storm door"))
    assert job.status is JobStatus.OPEN

    visit = await adapter.create_visit(VisitCreate(
        job_id=job.id, technician_id="T-03",
        start=datetime(2030, 1, 7, 9), end=datetime(2030, 1, 7, 11), notes="Measure the opening first."))
    assert visit.status is VisitStatus.BOOKED
    assert (await adapter.get_job(job.id)).status is JobStatus.SCHEDULED
    assert [v.id for v in (await adapter.list_visits(job_id=job.id)).items] == [visit.id]

    quote = await adapter.create_quote(QuoteCreate(customer_id=customer.id, job_id=job.id, line_items=[
        QuoteLineItem(description="Storm door", quantity=1, unit_price=249.99),
        QuoteLineItem(description="Labor", quantity=2, unit_price=95),
    ]))
    assert quote.status is QuoteStatus.DRAFT
    assert quote.total == 439.99
    assert (await adapter.list_quotes(customer_id=customer.id)).total == 1


async def test_visit_window_filter(adapter):
    monday = await adapter.list_visits(start=datetime(2030, 1, 7), end=datetime(2030, 1, 8))
    assert [visit.id for visit in monday.items] == ["AP-3001", "AP-3003", "AP-3002"]
    assert len(await adapter.list_all_visits()) == 7


async def test_errors_are_translated(adapter):
    with pytest.raises(NotFoundError, match="Job WO-0 does not exist"):
        await adapter.get_job("WO-0")
    with pytest.raises(InvalidRequestError, match="Client CL-0 does not exist"):
        await adapter.create_job(JobCreate(customer_id="CL-0", title="Anything"))
    with pytest.raises(ConflictError, match="already booked") as caught:
        await adapter.create_visit(VisitCreate(job_id="WO-2001", technician_id="T-01",
                                               start=datetime(2030, 1, 7, 10), end=datetime(2030, 1, 7, 12)))
    assert caught.value.status_code == 409
    assert "pick different values" in caught.value.for_model()


async def test_wrong_api_key_is_an_auth_error():
    adapter = MockRestAdapter(api_key="wrong")
    try:
        with pytest.raises(AuthError):
            await adapter.list_technicians()
    finally:
        await adapter.aclose()


def scripted(responses: list) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """A transport that plays back canned responses and records the requests."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        step = responses[min(len(seen), len(responses)) - 1]
        if isinstance(step, Exception):
            raise step
        return step

    return httpx.MockTransport(handler), seen


async def test_reads_are_retried_after_a_server_error():
    ok = httpx.Response(200, json={"data": [{"id": "T-01", "name": "Sam Example", "skills": []}]})
    transport, seen = scripted([httpx.Response(503), httpx.ConnectError("down"), ok])
    adapter = MockRestAdapter(base_url="http://vendor.invalid", transport=transport, max_retries=2, backoff_seconds=0)
    assert [tech.id for tech in await adapter.list_technicians()] == ["T-01"]
    assert len(seen) == 3
    assert seen[0].headers["x-api-key"] == "mock-key"
    assert seen[0].url.path == "/api/v1/techs"
    await adapter.aclose()


async def test_reads_give_up_after_max_retries():
    transport, seen = scripted([httpx.Response(502)])
    adapter = MockRestAdapter(base_url="http://vendor.invalid", transport=transport, max_retries=2, backoff_seconds=0)
    with pytest.raises(UpstreamError) as caught:
        await adapter.list_technicians()
    assert len(seen) == 3
    assert caught.value.retryable is True
    await adapter.aclose()


async def test_writes_are_never_retried():
    transport, seen = scripted([httpx.Response(503)])
    adapter = MockRestAdapter(base_url="http://vendor.invalid", transport=transport, max_retries=3, backoff_seconds=0)
    with pytest.raises(UpstreamError):
        await adapter.create_job(JobCreate(customer_id="CL-1001", title="Do not duplicate me"))
    assert len(seen) == 1
    await adapter.aclose()


async def test_timeouts_and_client_errors_are_not_confused():
    transport, seen = scripted([httpx.ReadTimeout("slow")])
    adapter = MockRestAdapter(base_url="http://vendor.invalid", transport=transport, max_retries=1, backoff_seconds=0)
    with pytest.raises(UpstreamError, match="did not answer in time"):
        await adapter.list_technicians()
    assert len(seen) == 2
    await adapter.aclose()

    transport, seen = scripted([httpx.Response(404, json={"error": {"code": "not_found", "message": "gone"}})])
    adapter = MockRestAdapter(base_url="http://vendor.invalid", transport=transport, max_retries=3, backoff_seconds=0)
    with pytest.raises(NotFoundError):
        await adapter.get_quote("ES-1")
    assert len(seen) == 1  # a 404 will not get better, so no retry
    await adapter.aclose()


async def test_validation_errors_from_the_vendor_are_readable():
    detail = [{"loc": ["body", "summary"], "msg": "Field required", "type": "missing"}]
    transport, _ = scripted([httpx.Response(422, json={"detail": detail})])
    adapter = MockRestAdapter(base_url="http://vendor.invalid", transport=transport, backoff_seconds=0)
    with pytest.raises(InvalidRequestError, match="summary: Field required"):
        await adapter.create_job(JobCreate(customer_id="CL-1001", title="x"))
    await adapter.aclose()
