"""The MCP surface, exercised through a real MCP client connected in process."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from conftest import text_of
from field_service_mcp.config import Settings
from field_service_mcp.server import READ_TOOLS, WRITE_TOOLS, build_server

pytestmark = pytest.mark.anyio


async def test_all_tools_are_listed_with_schemas_and_hints(client):
    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) == set(READ_TOOLS) | set(WRITE_TOOLS)
    for name, tool in tools.items():
        assert tool.description, name
        assert tool.input_schema["type"] == "object", name
        assert tool.annotations.read_only_hint is (name in READ_TOOLS), name
    assert tools["create_visit"].input_schema["required"] == ["job_id", "technician_id", "start", "end"]
    assert tools["list_jobs"].input_schema["properties"]["limit"]["maximum"] == 100
    assert "items" in tools["list_jobs"].output_schema["properties"]


async def test_server_info(client):
    info = (await client.call_tool("server_info", {})).structured_content
    assert info["adapter"] == "mock"
    assert info["read_only"] is False
    assert "create_job" in info["tools"]


async def test_list_and_get_return_structured_content(client):
    result = await client.call_tool("list_customers", {"search": "sample", "limit": 5})
    assert result.is_error is False
    page = result.structured_content
    assert page["total"] == 2
    assert [item["name"] for item in page["items"]] == ["Alex Sample", "Sample Property Group"]
    assert "Alex Sample" in text_of(result)  # models without structured output support still get JSON text

    quote = (await client.call_tool("get_quote", {"quote_id": "ES-4002"})).structured_content
    assert quote["status"] == "sent"
    assert quote["total"] == 427.0
    assert quote["line_items"][0] == {"description": "20A outlet with box", "quantity": 2.0, "unit_price": 42.0}


async def test_list_jobs_filters_by_enum(client):
    result = await client.call_tool("list_jobs", {"status": "in_progress"})
    assert [job["title"] for job in result.structured_content["items"]] == ["Water heater not heating"]

    bad = await client.call_tool("list_jobs", {"status": "sideways"})
    assert bad.is_error is True


async def test_list_visits_by_inclusive_date_range(client):
    result = await client.call_tool("list_visits", {"date_from": "2030-01-08", "date_to": "2030-01-09"})
    visits = result.structured_content["items"]
    assert [visit["id"] for visit in visits] == ["AP-3004", "AP-3005", "AP-3006"]
    assert visits[0]["start"] == "2030-01-08T08:00:00"


async def test_schedule_and_open_slots(client):
    schedule = (await client.call_tool("get_schedule", {"date_from": "2030-01-07"})).structured_content
    assert [(day["technician_name"], day["booked_minutes"]) for day in schedule["days"]] == [
        ("Sam Example", 240), ("Jordan Placeholder", 150)]

    slots = (await client.call_tool(
        "find_open_slots", {"day": "2030-01-07", "duration_minutes": 180, "technician_id": "T-02"}
    )).structured_content
    assert [(slot["start"], slot["minutes"]) for slot in slots["slots"]] == [("2030-01-07T12:30:00", 270)]


async def test_agent_workflow_new_customer_to_booked_visit(client):
    customer = (await client.call_tool("create_customer", {
        "name": "Quinn Hypothetical", "phone": "555-0150", "email": "quinn.hypothetical@example.com",
        "address": {"line1": "800 Pretend Place", "city": "Sampletown", "region": "ZZ", "postal_code": "00008"},
    })).structured_content
    job = (await client.call_tool("create_job", {
        "customer_id": customer["id"], "title": "Install ceiling fan", "priority": "high",
    })).structured_content
    assert (job["status"], job["priority"]) == ("open", "high")

    slots = (await client.call_tool(
        "find_open_slots", {"day": "2030-01-07", "duration_minutes": 120, "technician_id": "T-02"}
    )).structured_content["slots"]
    visit = (await client.call_tool("create_visit", {
        "job_id": job["id"], "technician_id": "T-02", "start": slots[0]["start"], "end": "2030-01-07T10:00:00",
    })).structured_content
    assert visit["status"] == "booked"

    quote = (await client.call_tool("create_quote", {
        "customer_id": customer["id"], "job_id": job["id"],
        "line_items": [{"description": "Ceiling fan install", "quantity": 1, "unit_price": 185.5}],
    })).structured_content
    assert (quote["status"], quote["total"]) == ("draft", 185.5)

    assert (await client.call_tool("get_job", {"job_id": job["id"]})).structured_content["status"] == "scheduled"
    after = (await client.call_tool(
        "find_open_slots", {"day": "2030-01-07", "duration_minutes": 120, "technician_id": "T-02"}
    )).structured_content["slots"]
    assert [slot["start"] for slot in after] == ["2030-01-07T12:30:00"]


async def test_expected_failures_come_back_as_readable_tool_errors(client):
    missing = await client.call_tool("get_customer", {"customer_id": "CL-0"})
    assert missing.is_error is True
    assert "Customer CL-0 does not exist" in text_of(missing)
    assert "list tool" in text_of(missing)

    clash = await client.call_tool("create_visit", {
        "job_id": "WO-2001", "technician_id": "T-01",
        "start": "2030-01-07T10:00:00", "end": "2030-01-07T11:30:00"})
    assert clash.is_error is True
    assert "already booked" in text_of(clash)

    backwards = await client.call_tool("create_visit", {
        "job_id": "WO-2001", "technician_id": "T-02",
        "start": "2030-01-07T15:00:00", "end": "2030-01-07T14:00:00"})
    assert backwards.is_error is True
    assert "end must be after start" in text_of(backwards)

    with_offset = await client.call_tool("create_visit", {
        "job_id": "WO-2001", "technician_id": "T-02",
        "start": "2030-01-07T15:00:00Z", "end": "2030-01-07T16:00:00Z"})
    assert with_offset.is_error is True
    assert "no UTC offset" in text_of(with_offset)

    empty_quote = await client.call_tool("create_quote", {"customer_id": "CL-1001", "line_items": []})
    assert empty_quote.is_error is True

    too_many = await client.call_tool("list_jobs", {"limit": 500})
    assert too_many.is_error is True

    wide = await client.call_tool("get_schedule", {"date_from": "2030-01-01", "date_to": "2030-06-01"})
    assert wide.is_error is True
    assert "at most 31 days" in text_of(wide)


async def test_failed_writes_change_nothing(client):
    before = (await client.call_tool("list_visits", {"limit": 100})).structured_content["total"]
    await client.call_tool("create_visit", {
        "job_id": "WO-2001", "technician_id": "T-01",
        "start": "2030-01-07T10:00:00", "end": "2030-01-07T11:30:00"})
    after = (await client.call_tool("list_visits", {"limit": 100})).structured_content["total"]
    assert before == after == 7


async def test_read_only_mode_hides_every_write_tool(adapter):
    settings = Settings(read_only=True)
    async with Client(build_server(adapter, settings)) as session:
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert names == set(READ_TOOLS)
        info = (await session.call_tool("server_info", {})).structured_content
        assert info["read_only"] is True and "create_job" not in info["tools"]
        try:
            refused = await session.call_tool("create_job", {"customer_id": "CL-1001", "title": "Sneaky"})
            assert refused.is_error is True
        except Exception:
            pass  # a protocol error for an unknown tool is an equally valid refusal
        assert (await session.call_tool("list_jobs", {})).structured_content["total"] == 8


async def test_server_works_with_a_different_adapter():
    from in_memory_adapter import InMemoryAdapter

    async with Client(build_server(InMemoryAdapter())) as session:
        info = (await session.call_tool("server_info", {})).structured_content
        assert info["adapter"] == "in-memory"
        job = (await session.call_tool("create_job", {"customer_id": "C1", "title": "Tighten railing"})).structured_content
        booked = await session.call_tool("create_visit", {
            "job_id": job["id"], "technician_id": "X1", "start": "2030-02-01T09:00:00", "end": "2030-02-01T10:00:00"})
        assert booked.is_error is False
        schedule = (await session.call_tool("get_schedule", {"date_from": "2030-02-01"})).structured_content
        assert schedule["days"][0]["entries"][0]["customer_name"] == "Pat Imaginary"
        slots = (await session.call_tool("find_open_slots", {"day": "2030-02-01", "duration_minutes": 60})).structured_content
        assert [slot["start"] for slot in slots["slots"]] == ["2030-02-01T08:00:00", "2030-02-01T10:00:00"]


async def test_stdio_transport_end_to_end():
    """Launch the real server as a subprocess and talk to it over stdin and stdout."""
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [src, env.get("PYTHONPATH", "")]))
    env["FSM_ADAPTER"] = "mock"
    env.pop("FSM_BASE_URL", None)
    params = StdioServerParameters(command=sys.executable, args=["-m", "field_service_mcp.server"], env=env)
    async with Client(params) as session:
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert "find_open_slots" in names
        result = await session.call_tool("list_technicians", {})
        assert [tech["name"] for tech in result.structured_content["result"]] == [
            "Sam Example", "Jordan Placeholder", "Riley Demo"]
