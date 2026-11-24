"""Adapter for the bundled mock vendor API.

This is also the reference implementation for a real vendor adapter: an
HTTP client with auth, retries on safe requests, error translation, and a
pair of mapping functions per record type.

With no base URL the adapter mounts the mock FastAPI app in process through
httpx's ASGI transport, so nothing listens on a port and nothing leaves the
machine. With a base URL it speaks plain HTTP to wherever the mock is running.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable, TypeVar

import httpx

from ..config import MOCK_API_KEY, Settings
from ..errors import (
    AdapterError,
    AuthError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    UpstreamError,
)
from ..models import (
    Address,
    Customer,
    CustomerCreate,
    Job,
    JobCreate,
    JobPriority,
    JobStatus,
    Page,
    Quote,
    QuoteCreate,
    QuoteLineItem,
    QuoteStatus,
    Technician,
    Visit,
    VisitCreate,
    VisitStatus,
)
from .base import MAX_PAGE_SIZE, FieldServiceAdapter

log = logging.getLogger(__name__)

T = TypeVar("T")

# --------------------------------------------------------------------------- #
# Vocabulary translation
# --------------------------------------------------------------------------- #

JOB_STATUS_FROM_VENDOR = {
    "OPEN": JobStatus.OPEN,
    "SCHEDULED": JobStatus.SCHEDULED,
    "IN_PROGRESS": JobStatus.IN_PROGRESS,
    "DONE": JobStatus.COMPLETED,
    "CANCELLED": JobStatus.CANCELLED,
}
JOB_STATUS_TO_VENDOR = {status: code for code, status in JOB_STATUS_FROM_VENDOR.items()}

VISIT_STATUS_FROM_VENDOR = {
    "BOOKED": VisitStatus.BOOKED,
    "COMPLETED": VisitStatus.COMPLETED,
    "CANCELLED": VisitStatus.CANCELLED,
}

QUOTE_STATUS_FROM_VENDOR = {
    "DRAFT": QuoteStatus.DRAFT,
    "SENT": QuoteStatus.SENT,
    "APPROVED": QuoteStatus.APPROVED,
    "DECLINED": QuoteStatus.DECLINED,
}
QUOTE_STATUS_TO_VENDOR = {status: code for code, status in QUOTE_STATUS_FROM_VENDOR.items()}


def cents_to_amount(cents: int) -> float:
    return round(cents / 100, 2)


def amount_to_cents(amount: float) -> int:
    return int(round(amount * 100))


def _when(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


def customer_from_vendor(row: dict[str, Any]) -> Customer:
    address = row.get("serviceAddress")
    return Customer(
        id=row["id"],
        name=row["displayName"],
        phone=row.get("phone"),
        email=row.get("email"),
        address=Address(
            line1=address["line1"], city=address["city"], region=address["region"], postal_code=address["postalCode"]
        ) if address else None,
        created_at=_when(row.get("createdAt")),
    )


def customer_to_vendor(data: CustomerCreate) -> dict[str, Any]:
    body: dict[str, Any] = {"displayName": data.name, "phone": data.phone, "email": data.email}
    if data.address is not None:
        body["serviceAddress"] = {
            "line1": data.address.line1,
            "city": data.address.city,
            "region": data.address.region,
            "postalCode": data.address.postal_code,
        }
    return body


def job_from_vendor(row: dict[str, Any]) -> Job:
    return Job(
        id=row["id"],
        customer_id=row["clientId"],
        title=row["summary"],
        description=row.get("details") or "",
        status=JOB_STATUS_FROM_VENDOR[row["state"]],
        priority=JobPriority(row.get("priority", "NORMAL").lower()),
        created_at=_when(row.get("createdAt")),
    )


def job_to_vendor(data: JobCreate) -> dict[str, Any]:
    return {
        "clientId": data.customer_id,
        "summary": data.title,
        "details": data.description,
        "priority": data.priority.value.upper(),
    }


def visit_from_vendor(row: dict[str, Any]) -> Visit:
    return Visit(
        id=row["id"],
        job_id=row["workOrderId"],
        technician_id=row["techId"],
        start=datetime.fromisoformat(row["startsAt"]),
        end=datetime.fromisoformat(row["endsAt"]),
        status=VISIT_STATUS_FROM_VENDOR[row["state"]],
        notes=row.get("notes") or "",
    )


def visit_to_vendor(data: VisitCreate) -> dict[str, Any]:
    return {
        "workOrderId": data.job_id,
        "techId": data.technician_id,
        "startsAt": data.start.isoformat(),
        "endsAt": data.end.isoformat(),
        "notes": data.notes,
    }


def quote_from_vendor(row: dict[str, Any]) -> Quote:
    return Quote(
        id=row["id"],
        customer_id=row["clientId"],
        job_id=row.get("workOrderId"),
        status=QUOTE_STATUS_FROM_VENDOR[row["state"]],
        line_items=[
            QuoteLineItem(description=line["label"], quantity=line["qty"],
                          unit_price=cents_to_amount(line["unitPriceCents"]))
            for line in row["lines"]
        ],
        total=cents_to_amount(row["totalCents"]),
        created_at=_when(row.get("createdAt")),
    )


def quote_to_vendor(data: QuoteCreate) -> dict[str, Any]:
    return {
        "clientId": data.customer_id,
        "workOrderId": data.job_id,
        "lines": [
            {"label": item.description, "qty": item.quantity, "unitPriceCents": amount_to_cents(item.unit_price)}
            for item in data.line_items
        ],
    }


def technician_from_vendor(row: dict[str, Any]) -> Technician:
    return Technician(id=row["id"], name=row["name"], skills=list(row.get("skills") or []))


# --------------------------------------------------------------------------- #
# Error translation
# --------------------------------------------------------------------------- #


def _error_message(response: httpx.Response) -> str:
    """Pull a human readable message out of the vendor's error body."""
    try:
        body = response.json()
    except ValueError:
        return f"Vendor returned HTTP {response.status_code}."
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        detail = body.get("detail")
        if isinstance(detail, list):  # FastAPI style validation errors
            parts = []
            for item in detail:
                where = ".".join(str(part) for part in item.get("loc", []) if part != "body")
                parts.append(f"{where}: {item.get('msg', 'invalid')}" if where else str(item.get("msg", "invalid")))
            return "Vendor rejected the request: " + "; ".join(parts) + "."
        if isinstance(detail, str):
            return detail
    return f"Vendor returned HTTP {response.status_code}."


def error_from_response(response: httpx.Response) -> AdapterError:
    status = response.status_code
    message = _error_message(response)
    if status in (401, 403):
        return AuthError(message, status_code=status)
    if status == 404:
        return NotFoundError(message, status_code=status)
    if status == 409:
        return ConflictError(message, status_code=status)
    if status in (400, 422):
        return InvalidRequestError(message, status_code=status)
    return UpstreamError(message, status_code=status)


RETRY_STATUSES = {429, 500, 502, 503, 504}


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #


class MockRestAdapter(FieldServiceAdapter):
    name = "mock"

    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = MOCK_API_KEY,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.in_process = transport is None and not base_url
        if self.in_process:
            from ..mock_api import create_app

            transport = httpx.ASGITransport(app=create_app())
            base_url = "http://mock-field-service.invalid"
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v1",
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            timeout=timeout_seconds,
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "MockRestAdapter":
        return cls(
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout_seconds=settings.timeout_seconds,
            max_retries=settings.max_retries,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- HTTP plumbing -------------------------------------------------------- #

    async def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                       json: dict[str, Any] | None = None) -> Any:
        """Send one request. Only GETs are retried, because POSTs are not idempotent."""
        params = {key: value for key, value in (params or {}).items() if value is not None}
        attempts = 1 + (self._max_retries if method == "GET" else 0)
        last_error: AdapterError | None = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(self._backoff * (2 ** (attempt - 1)))
            try:
                response = await self._client.request(method, path, params=params, json=json)
            except httpx.TimeoutException:
                last_error = UpstreamError("The field service system did not answer in time.")
                continue
            except httpx.TransportError as exc:
                last_error = UpstreamError(f"Could not reach the field service system ({type(exc).__name__}).")
                continue
            if response.status_code < 400:
                return response.json()
            last_error = error_from_response(response)
            if response.status_code not in RETRY_STATUSES:
                break
            log.warning("vendor %s %s returned %s (attempt %s of %s)", method, path, response.status_code,
                        attempt + 1, attempts)
        assert last_error is not None
        raise last_error

    async def _get_one(self, path: str, label: str, record_id: str) -> Any:
        """Fetch one record, rewording a 404 in domain terms instead of vendor terms."""
        try:
            return await self._request("GET", f"{path}/{record_id}")
        except NotFoundError as exc:
            raise NotFoundError(f"{label} {record_id} does not exist.", status_code=exc.status_code) from exc

    async def _list(self, path: str, params: dict[str, Any], limit: int, offset: int,
                    convert: Callable[[dict[str, Any]], T]) -> Page[T]:
        """Translate limit/offset into the vendor's page/pageSize scheme.

        When the offset is not a multiple of the limit the requested window
        straddles two vendor pages, so a second page is fetched and stitched.
        """
        limit = max(1, min(limit, MAX_PAGE_SIZE))
        offset = max(0, offset)
        page_number, skip = divmod(offset, limit)
        query = {**params, "page": page_number + 1, "pageSize": limit}
        body = await self._request("GET", path, params=query)
        rows = body["data"][skip:]
        total = int(body["meta"]["total"])
        if skip and offset + len(rows) < total:
            query["page"] = page_number + 2
            more = await self._request("GET", path, params=query)
            rows.extend(more["data"][:skip])
        return Page(items=[convert(row) for row in rows], total=total, limit=limit, offset=offset)

    # -- customers ------------------------------------------------------------ #

    async def list_customers(self, *, search: str | None = None, limit: int = 25, offset: int = 0) -> Page[Customer]:
        return await self._list("/clients", {"q": search or None}, limit, offset, customer_from_vendor)

    async def get_customer(self, customer_id: str) -> Customer:
        return customer_from_vendor(await self._get_one("/clients", "Customer", customer_id))

    async def create_customer(self, data: CustomerCreate) -> Customer:
        return customer_from_vendor(await self._request("POST", "/clients", json=customer_to_vendor(data)))

    # -- jobs ----------------------------------------------------------------- #

    async def list_jobs(self, *, customer_id: str | None = None, status: JobStatus | None = None,
                        limit: int = 25, offset: int = 0) -> Page[Job]:
        params = {"clientId": customer_id, "state": JOB_STATUS_TO_VENDOR[status] if status else None}
        return await self._list("/workorders", params, limit, offset, job_from_vendor)

    async def get_job(self, job_id: str) -> Job:
        return job_from_vendor(await self._get_one("/workorders", "Job", job_id))

    async def create_job(self, data: JobCreate) -> Job:
        return job_from_vendor(await self._request("POST", "/workorders", json=job_to_vendor(data)))

    # -- visits --------------------------------------------------------------- #

    async def list_visits(self, *, job_id: str | None = None, technician_id: str | None = None,
                          start: datetime | None = None, end: datetime | None = None,
                          limit: int = 25, offset: int = 0) -> Page[Visit]:
        params = {
            "workOrderId": job_id,
            "techId": technician_id,
            "from": start.isoformat() if start else None,
            "to": end.isoformat() if end else None,
        }
        return await self._list("/appointments", params, limit, offset, visit_from_vendor)

    async def create_visit(self, data: VisitCreate) -> Visit:
        return visit_from_vendor(await self._request("POST", "/appointments", json=visit_to_vendor(data)))

    # -- quotes --------------------------------------------------------------- #

    async def list_quotes(self, *, customer_id: str | None = None, status: QuoteStatus | None = None,
                          limit: int = 25, offset: int = 0) -> Page[Quote]:
        params = {"clientId": customer_id, "state": QUOTE_STATUS_TO_VENDOR[status] if status else None}
        return await self._list("/estimates", params, limit, offset, quote_from_vendor)

    async def get_quote(self, quote_id: str) -> Quote:
        return quote_from_vendor(await self._get_one("/estimates", "Quote", quote_id))

    async def create_quote(self, data: QuoteCreate) -> Quote:
        return quote_from_vendor(await self._request("POST", "/estimates", json=quote_to_vendor(data)))

    # -- people --------------------------------------------------------------- #

    async def list_technicians(self) -> list[Technician]:
        body = await self._request("GET", "/techs")
        return [technician_from_vendor(row) for row in body["data"]]
