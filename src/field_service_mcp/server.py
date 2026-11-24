"""The MCP server: tools for customers, jobs, visits, quotes and the schedule.

Built on the official MCP Python SDK, version 2 (``MCPServer``). The server
never talks to a vendor directly. Every tool goes through the adapter that
``build_server`` was given.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from . import __version__
from .adapters import FieldServiceAdapter, create_adapter
from .adapters.base import MAX_PAGE_SIZE
from .config import ConfigError, Settings
from .errors import AdapterError, InvalidRequestError
from .models import (
    Address,
    Customer,
    CustomerCreate,
    Job,
    JobCreate,
    JobPriority,
    JobStatus,
    OpenSlots,
    Page,
    Quote,
    QuoteCreate,
    QuoteLineItem,
    QuoteStatus,
    Schedule,
    Technician,
    Visit,
    VisitCreate,
)
from .schedule import build_schedule, find_open_slots as compute_open_slots

log = logging.getLogger("field_service_mcp")

INSTRUCTIONS = """\
Tools for a field service business: customers, jobs (work to be done), visits
(a technician on site for a time window), quotes and the schedule.

Working rules:
- Ids are opaque. Find them with a list tool before calling get or create tools.
- Datetimes are local to the business and carry no UTC offset, for example 2030-01-07T09:00:00.
- Before create_visit, call find_open_slots so the technician is not double booked.
- Create tools change the real system. Call them only when the user asked for the change.
- A tool error explains what went wrong and whether a retry can help.
"""

READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)

Limit = Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE, description="Maximum records to return")]
Offset = Annotated[int, Field(ge=0, description="Records to skip, for paging")]

READ_TOOLS = (
    "server_info", "list_customers", "get_customer", "list_jobs", "get_job", "list_visits",
    "list_quotes", "get_quote", "list_technicians", "get_schedule", "find_open_slots",
)
WRITE_TOOLS = ("create_customer", "create_job", "create_visit", "create_quote")


@contextmanager
def tool_errors() -> Iterator[None]:
    """Turn expected failures into tool errors the calling model can read."""
    try:
        yield
    except AdapterError as exc:
        raise ToolError(exc.for_model()) from exc
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or 'input'}: {err['msg']}" for err in exc.errors()
        )
        raise ToolError(f"Invalid arguments: {problems}. Fix the arguments and call the tool again.") from exc


def _day_bounds(date_from: date | None, date_to: date | None) -> tuple[datetime | None, datetime | None]:
    start = datetime.combine(date_from, time.min) if date_from else None
    end = datetime.combine(date_to + timedelta(days=1), time.min) if date_to else None
    return start, end


def build_server(adapter: FieldServiceAdapter, settings: Settings | None = None) -> MCPServer:
    """Create the MCP server around one adapter instance."""
    settings = settings or Settings()
    mcp = MCPServer("field-service", instructions=INSTRUCTIONS, version=__version__)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per vendor call is too noisy

    # ------------------------------------------------------------------ meta

    @mcp.tool(annotations=READ)
    async def server_info() -> dict[str, Any]:
        """Describe this server: which adapter is active, whether writes are enabled, and working hours."""
        return {
            "version": __version__,
            "adapter": adapter.name,
            "read_only": settings.read_only,
            "workday_start": settings.workday_start.strftime("%H:%M"),
            "workday_end": settings.workday_end.strftime("%H:%M"),
            "tools": list(READ_TOOLS) + ([] if settings.read_only else list(WRITE_TOOLS)),
        }

    # ------------------------------------------------------------- customers

    @mcp.tool(annotations=READ)
    async def list_customers(
        search: Annotated[str | None, Field(description="Matches name, email or phone")] = None,
        limit: Limit = 25,
        offset: Offset = 0,
    ) -> Page[Customer]:
        """List customers, optionally filtered by a free text search."""
        with tool_errors():
            return await adapter.list_customers(search=search, limit=limit, offset=offset)

    @mcp.tool(annotations=READ)
    async def get_customer(customer_id: str) -> Customer:
        """Fetch one customer by id."""
        with tool_errors():
            return await adapter.get_customer(customer_id)

    # ------------------------------------------------------------------ jobs

    @mcp.tool(annotations=READ)
    async def list_jobs(
        customer_id: str | None = None,
        status: JobStatus | None = None,
        limit: Limit = 25,
        offset: Offset = 0,
    ) -> Page[Job]:
        """List jobs, newest first. Filter by customer and by status."""
        with tool_errors():
            return await adapter.list_jobs(customer_id=customer_id, status=status, limit=limit, offset=offset)

    @mcp.tool(annotations=READ)
    async def get_job(job_id: str) -> Job:
        """Fetch one job by id."""
        with tool_errors():
            return await adapter.get_job(job_id)

    # ---------------------------------------------------------------- visits

    @mcp.tool(annotations=READ)
    async def list_visits(
        job_id: str | None = None,
        technician_id: str | None = None,
        date_from: Annotated[date | None, Field(description="First day to include (YYYY-MM-DD)")] = None,
        date_to: Annotated[date | None, Field(description="Last day to include (YYYY-MM-DD)")] = None,
        limit: Limit = 25,
        offset: Offset = 0,
    ) -> Page[Visit]:
        """List visits, earliest first. Filter by job, technician and an inclusive date range."""
        with tool_errors():
            if date_from and date_to and date_to < date_from:
                raise InvalidRequestError("date_to must be on or after date_from.")
            start, end = _day_bounds(date_from, date_to)
            return await adapter.list_visits(job_id=job_id, technician_id=technician_id, start=start, end=end,
                                             limit=limit, offset=offset)

    # ---------------------------------------------------------------- quotes

    @mcp.tool(annotations=READ)
    async def list_quotes(
        customer_id: str | None = None,
        status: QuoteStatus | None = None,
        limit: Limit = 25,
        offset: Offset = 0,
    ) -> Page[Quote]:
        """List quotes, newest first. Filter by customer and by status."""
        with tool_errors():
            return await adapter.list_quotes(customer_id=customer_id, status=status, limit=limit, offset=offset)

    @mcp.tool(annotations=READ)
    async def get_quote(quote_id: str) -> Quote:
        """Fetch one quote with its line items and total."""
        with tool_errors():
            return await adapter.get_quote(quote_id)

    # -------------------------------------------------------------- schedule

    @mcp.tool(annotations=READ)
    async def list_technicians() -> list[Technician]:
        """List the technicians who can be assigned to visits."""
        with tool_errors():
            return await adapter.list_technicians()

    @mcp.tool(annotations=READ)
    async def get_schedule(
        date_from: Annotated[date, Field(description="First day (YYYY-MM-DD)")],
        date_to: Annotated[date | None, Field(description="Last day, inclusive. Defaults to date_from.")] = None,
        technician_id: str | None = None,
    ) -> Schedule:
        """Show who is booked where: visits grouped by technician and day, with job and customer names."""
        with tool_errors():
            return await build_schedule(adapter, date_from, date_to or date_from, technician_id)

    @mcp.tool(annotations=READ)
    async def find_open_slots(
        day: Annotated[date, Field(description="Day to search (YYYY-MM-DD)")],
        duration_minutes: Annotated[int, Field(ge=15, le=720, description="Length of the visit to fit")] = 60,
        technician_id: str | None = None,
    ) -> OpenSlots:
        """Find free windows inside working hours that can fit a visit of the given length."""
        with tool_errors():
            return await compute_open_slots(adapter, day, duration_minutes, settings.workday_start,
                                            settings.workday_end, technician_id)

    if settings.read_only:
        log.info("read-only mode: create tools are not registered")
        return mcp

    # ---------------------------------------------------------------- writes

    @mcp.tool(annotations=WRITE)
    async def create_customer(
        name: Annotated[str, Field(min_length=1, max_length=120)],
        phone: str | None = None,
        email: str | None = None,
        address: Address | None = None,
    ) -> Customer:
        """Create a customer. Search with list_customers first to avoid duplicates."""
        with tool_errors():
            data = CustomerCreate(name=name, phone=phone, email=email, address=address)
            created = await adapter.create_customer(data)
            log.info("created customer %s", created.id)
            return created

    @mcp.tool(annotations=WRITE)
    async def create_job(
        customer_id: str,
        title: Annotated[str, Field(min_length=1, max_length=160, description="Short summary of the work")],
        description: str = "",
        priority: JobPriority = JobPriority.NORMAL,
    ) -> Job:
        """Create a job (a piece of work) for an existing customer. It starts in status open."""
        with tool_errors():
            data = JobCreate(customer_id=customer_id, title=title, description=description, priority=priority)
            created = await adapter.create_job(data)
            log.info("created job %s for customer %s", created.id, created.customer_id)
            return created

    @mcp.tool(annotations=WRITE)
    async def create_visit(
        job_id: str,
        technician_id: str,
        start: Annotated[datetime, Field(description="Local start, no UTC offset, e.g. 2030-01-07T09:00:00")],
        end: Annotated[datetime, Field(description="Local end, no UTC offset, after start")],
        notes: str = "",
    ) -> Visit:
        """Book a technician on a job for a time window. Fails if the technician is already booked then."""
        with tool_errors():
            data = VisitCreate(job_id=job_id, technician_id=technician_id, start=start, end=end, notes=notes)
            created = await adapter.create_visit(data)
            log.info("created visit %s on job %s", created.id, created.job_id)
            return created

    @mcp.tool(annotations=WRITE)
    async def create_quote(
        customer_id: str,
        line_items: Annotated[list[QuoteLineItem], Field(min_length=1, max_length=100)],
        job_id: str | None = None,
    ) -> Quote:
        """Create a draft quote for a customer. The total is computed by the field service system."""
        with tool_errors():
            data = QuoteCreate(customer_id=customer_id, job_id=job_id, line_items=line_items)
            created = await adapter.create_quote(data)
            log.info("created quote %s for customer %s", created.id, created.customer_id)
            return created

    return mcp


def main() -> None:
    """Console entry point: read settings from the environment and serve."""
    # stdout belongs to the stdio transport, so all logging goes to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = Settings.from_env()
        adapter = create_adapter(settings)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    server = build_server(adapter, settings)
    log.info("starting field-service MCP server: adapter=%s read_only=%s transport=%s",
             adapter.name, settings.read_only, settings.transport)
    if settings.transport == "streamable-http":
        server.run("streamable-http", host=settings.host, port=settings.port)
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
