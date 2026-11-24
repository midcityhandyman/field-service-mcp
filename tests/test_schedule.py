"""Schedule views are computed above the adapter."""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from field_service_mcp.errors import InvalidRequestError, NotFoundError
from field_service_mcp.schedule import build_schedule, find_open_slots, free_windows

pytestmark = pytest.mark.anyio

DAY = date(2030, 1, 7)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(DAY, time(hour, minute))


def test_free_windows_with_no_bookings_is_the_whole_day():
    assert free_windows([], at(8), at(17), 60) == [(at(8), at(17))]


def test_free_windows_splits_around_bookings():
    busy = [(at(13), at(15)), (at(9), at(11))]  # unsorted on purpose
    assert free_windows(busy, at(8), at(17), 60) == [(at(8), at(9)), (at(11), at(13)), (at(15), at(17))]


def test_free_windows_drops_gaps_that_are_too_short():
    busy = [(at(9), at(11)), (at(11, 30), at(16, 30))]
    assert free_windows(busy, at(8), at(17), 60) == [(at(8), at(9))]
    assert free_windows(busy, at(8), at(17), 30) == [(at(8), at(9)), (at(11), at(11, 30)), (at(16, 30), at(17))]


def test_free_windows_handles_overlap_and_spill():
    busy = [(at(7), at(9)), (at(8, 30), at(10)), (at(16), at(19))]
    assert free_windows(busy, at(8), at(17), 60) == [(at(10), at(16))]


def test_free_windows_fully_booked():
    assert free_windows([(at(7), at(18))], at(8), at(17), 15) == []


async def test_schedule_groups_by_technician_and_day(adapter):
    schedule = await build_schedule(adapter, DAY, date(2030, 1, 8))
    keys = [(day.technician_id, day.day.isoformat()) for day in schedule.days]
    assert keys == [("T-01", "2030-01-07"), ("T-02", "2030-01-07"), ("T-03", "2030-01-08")]

    sam = schedule.days[0]
    assert sam.booked_minutes == 240
    assert [entry.job_title for entry in sam.entries] == ["Replace kitchen faucet", "Water heater not heating"]
    assert [entry.customer_name for entry in sam.entries] == ["Alex Sample", "Taylor Mockwell"]


async def test_schedule_for_one_technician(adapter):
    schedule = await build_schedule(adapter, DAY, date(2030, 1, 9), technician_id="T-03")
    assert {day.technician_id for day in schedule.days} == {"T-03"}
    assert len(schedule.days) == 2


async def test_schedule_rejects_bad_ranges(adapter):
    with pytest.raises(InvalidRequestError):
        await build_schedule(adapter, date(2030, 1, 8), DAY)
    with pytest.raises(InvalidRequestError):
        await build_schedule(adapter, DAY, date(2030, 3, 1))
    with pytest.raises(NotFoundError):
        await build_schedule(adapter, DAY, DAY, technician_id="T-99")


async def test_open_slots_respect_bookings_and_working_hours(adapter):
    result = await find_open_slots(adapter, DAY, 120, time(8), time(17))
    by_tech: dict[str, list[tuple[str, str]]] = {}
    for slot in result.slots:
        by_tech.setdefault(slot.technician_id, []).append((slot.start.strftime("%H:%M"), slot.end.strftime("%H:%M")))
    assert by_tech == {
        "T-01": [("11:00", "13:00"), ("15:00", "17:00")],
        "T-02": [("08:00", "10:00"), ("12:30", "17:00")],
        "T-03": [("08:00", "17:00")],
    }


async def test_open_slots_shrink_with_shorter_working_hours(adapter):
    result = await find_open_slots(adapter, DAY, 60, time(9), time(12), technician_id="T-01")
    assert [(slot.start.hour, slot.end.hour) for slot in result.slots] == [(11, 12)]


async def test_open_slots_validate_duration(adapter):
    with pytest.raises(InvalidRequestError):
        await find_open_slots(adapter, DAY, 5, time(8), time(17))
