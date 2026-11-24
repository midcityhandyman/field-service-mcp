"""Schedule views built on top of any adapter.

Vendors differ a lot in how (or whether) they expose a calendar. Instead of
asking every adapter for one, these functions derive it from the three calls
all adapters already have: list_visits, list_technicians and the get_* lookups.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta

from .adapters.base import FieldServiceAdapter
from .errors import InvalidRequestError, NotFoundError
from .models import (
    Customer,
    Job,
    OpenSlot,
    OpenSlots,
    Schedule,
    ScheduleEntry,
    TechnicianDay,
    Technician,
    Visit,
    VisitStatus,
)

MAX_RANGE_DAYS = 31


def free_windows(busy: list[tuple[datetime, datetime]], window_start: datetime, window_end: datetime,
                 min_minutes: int) -> list[tuple[datetime, datetime]]:
    """Return the gaps of at least ``min_minutes`` inside a working window.

    ``busy`` may be unsorted, may overlap and may spill outside the window.
    """
    gaps: list[tuple[datetime, datetime]] = []
    needed = timedelta(minutes=min_minutes)
    cursor = window_start
    for start, end in sorted(busy):
        if end <= cursor:
            continue
        if start >= window_end:
            break
        if start - cursor >= needed:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if window_end - cursor >= needed:
        gaps.append((cursor, window_end))
    return gaps


async def _pick_technicians(adapter: FieldServiceAdapter, technician_id: str | None) -> list[Technician]:
    technicians = await adapter.list_technicians()
    if technician_id is None:
        return technicians
    chosen = [tech for tech in technicians if tech.id == technician_id]
    if not chosen:
        raise NotFoundError(f"Technician {technician_id} does not exist.")
    return chosen


async def _lookup(call, key: str):
    """Fetch context for a schedule entry. A missing record is not fatal."""
    try:
        return key, await call(key)
    except NotFoundError:
        return key, None


async def _context(adapter: FieldServiceAdapter, visits: list[Visit]) -> tuple[dict[str, Job], dict[str, Customer]]:
    job_ids = sorted({visit.job_id for visit in visits})
    jobs = {key: job for key, job in await asyncio.gather(*(_lookup(adapter.get_job, i) for i in job_ids)) if job}
    customer_ids = sorted({job.customer_id for job in jobs.values()})
    customers = {
        key: customer
        for key, customer in await asyncio.gather(*(_lookup(adapter.get_customer, i) for i in customer_ids))
        if customer
    }
    return jobs, customers


async def build_schedule(adapter: FieldServiceAdapter, date_from: date, date_to: date,
                         technician_id: str | None = None) -> Schedule:
    """Group visits by technician and day for an inclusive date range."""
    if date_to < date_from:
        raise InvalidRequestError("date_to must be on or after date_from.")
    if (date_to - date_from).days >= MAX_RANGE_DAYS:
        raise InvalidRequestError(f"A schedule lookup can cover at most {MAX_RANGE_DAYS} days.")

    technicians = await _pick_technicians(adapter, technician_id)
    window_start = datetime.combine(date_from, time.min)
    window_end = datetime.combine(date_to + timedelta(days=1), time.min)
    visits = [
        visit
        for visit in await adapter.list_all_visits(technician_id=technician_id, start=window_start, end=window_end)
        if visit.status != VisitStatus.CANCELLED
    ]
    jobs, customers = await _context(adapter, visits)

    days: list[TechnicianDay] = []
    for offset in range((date_to - date_from).days + 1):
        day = date_from + timedelta(days=offset)
        for tech in technicians:
            entries = []
            for visit in visits:
                if visit.technician_id != tech.id or visit.start.date() != day:
                    continue
                job = jobs.get(visit.job_id)
                customer = customers.get(job.customer_id) if job else None
                entries.append(ScheduleEntry(
                    visit_id=visit.id,
                    job_id=visit.job_id,
                    job_title=job.title if job else None,
                    customer_id=job.customer_id if job else None,
                    customer_name=customer.name if customer else None,
                    start=visit.start,
                    end=visit.end,
                    status=visit.status,
                ))
            if not entries:
                continue
            entries.sort(key=lambda entry: entry.start)
            minutes = sum(int((entry.end - entry.start).total_seconds() // 60) for entry in entries)
            days.append(TechnicianDay(technician_id=tech.id, technician_name=tech.name, day=day,
                                      entries=entries, booked_minutes=minutes))
    return Schedule(date_from=date_from, date_to=date_to, days=days)


async def find_open_slots(adapter: FieldServiceAdapter, day: date, duration_minutes: int,
                          workday_start: time, workday_end: time,
                          technician_id: str | None = None) -> OpenSlots:
    """List free windows of at least ``duration_minutes`` for each technician on one day."""
    if not 15 <= duration_minutes <= 12 * 60:
        raise InvalidRequestError("duration_minutes must be between 15 and 720.")
    technicians = await _pick_technicians(adapter, technician_id)
    window_start = datetime.combine(day, workday_start)
    window_end = datetime.combine(day, workday_end)
    visits = await adapter.list_all_visits(technician_id=technician_id, start=window_start, end=window_end)

    slots: list[OpenSlot] = []
    for tech in technicians:
        busy = [
            (visit.start, visit.end)
            for visit in visits
            if visit.technician_id == tech.id and visit.status == VisitStatus.BOOKED
        ]
        for start, end in free_windows(busy, window_start, window_end, duration_minutes):
            slots.append(OpenSlot(technician_id=tech.id, technician_name=tech.name, start=start, end=end,
                                  minutes=int((end - start).total_seconds() // 60)))
    return OpenSlots(day=day, duration_minutes=duration_minutes,
                     workday_start=workday_start.strftime("%H:%M"), workday_end=workday_end.strftime("%H:%M"),
                     slots=slots)

