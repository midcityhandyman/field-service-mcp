"""Offline demo: a scripted agent session against the MCP server.

No model, no network and no keys are involved. A real MCP client connects to
the server in process and makes the tool calls an agent would make for a
typical office request. All data is invented and the dates are fixed, so the
output is the same on every run.

    python demo/run_demo.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp import Client  # noqa: E402

from field_service_mcp.adapters import MockRestAdapter  # noqa: E402
from field_service_mcp.config import Settings  # noqa: E402
from field_service_mcp.server import build_server  # noqa: E402


def say(text: str = "") -> None:
    print(text)


def step(number: int, title: str) -> None:
    say()
    say(f"== Step {number}: {title} ==")


async def call(client: Client, tool: str, **arguments: Any) -> Any:
    """Call a tool, print the call, and return the structured result (or None on a tool error)."""
    say(f"  > {tool}({', '.join(f'{key}={json.dumps(value)}' for key, value in arguments.items())})")
    result = await client.call_tool(tool, arguments)
    if result.is_error:
        message = " ".join(block.text for block in result.content if getattr(block, "text", None))
        say(f"    TOOL ERROR: {message}")
        return None
    return result.structured_content


def clock(stamp: str) -> str:
    return stamp[11:16]


def money(amount: float) -> str:
    return f"${amount:,.2f}"


async def main() -> None:
    logging.disable(logging.CRITICAL)  # keep the transcript clean; errors are shown as tool errors
    adapter = MockRestAdapter()
    settings = Settings()

    say("field-service-mcp demo (in-process mock vendor API, invented data, fixed dates)")
    say("Request from the office: \"Taylor Mockwell called. The water heater still is not fixed.")
    say("Get someone back out there Tuesday morning and send a quote for a new heating element.\"")

    async with Client(build_server(adapter, settings)) as client:
        step(1, "What can this server do?")
        tools = (await client.list_tools()).tools
        for tool in tools:
            kind = "read " if tool.annotations.read_only_hint else "WRITE"
            say(f"    [{kind}] {tool.name}: {tool.description.splitlines()[0]}")
        info = await call(client, "server_info")
        say(f"    adapter={info['adapter']} read_only={info['read_only']} "
            f"hours={info['workday_start']} to {info['workday_end']}")

        step(2, "Find the customer and the job")
        customers = await call(client, "list_customers", search="mockwell")
        customer = customers["items"][0]
        say(f"    {customer['id']}  {customer['name']}  {customer['phone']}  {customer['address']['line1']}")
        jobs = await call(client, "list_jobs", customer_id=customer["id"])
        job = jobs["items"][0]
        say(f"    {job['id']}  {job['title']}  status={job['status']}  priority={job['priority']}")
        visits = await call(client, "list_visits", job_id=job["id"])
        for visit in visits["items"]:
            say(f"    {visit['id']}  {visit['start'][:10]} {clock(visit['start'])} to {clock(visit['end'])}  "
                f"tech={visit['technician_id']}  {visit['status']}")
        technician_id = visits["items"][0]["technician_id"]

        step(3, "Look at Tuesday")
        schedule = await call(client, "get_schedule", date_from="2030-01-08")
        for day in schedule["days"]:
            say(f"    {day['technician_name']} ({day['technician_id']}), {day['booked_minutes']} minutes booked")
            for entry in day["entries"]:
                say(f"      {clock(entry['start'])} to {clock(entry['end'])}  {entry['job_title']}  "
                    f"for {entry['customer_name']}")
        slots = await call(client, "find_open_slots", day="2030-01-08", duration_minutes=120,
                           technician_id=technician_id)
        for slot in slots["slots"]:
            say(f"    open: {slot['technician_name']} {clock(slot['start'])} to {clock(slot['end'])} "
                f"({slot['minutes']} min)")

        step(4, "A careless booking is refused by the vendor and explained to the agent")
        await call(client, "create_visit", job_id=job["id"], technician_id=technician_id,
                   start="2030-01-07T14:00:00", end="2030-01-07T16:00:00")
        await call(client, "create_visit", job_id=job["id"], technician_id=technician_id,
                   start="2030-01-08T11:00:00", end="2030-01-08T09:00:00")

        step(5, "Book the return visit in an open slot")
        visit = await call(client, "create_visit", job_id=job["id"], technician_id=technician_id,
                           start="2030-01-08T09:00:00", end="2030-01-08T11:00:00",
                           notes="Return visit. Bring a replacement heating element.")
        say(f"    booked {visit['id']}: {visit['start'][:10]} {clock(visit['start'])} to {clock(visit['end'])} "
            f"tech={visit['technician_id']} status={visit['status']}")

        step(6, "Create the quote")
        quote = await call(client, "create_quote", customer_id=customer["id"], job_id=job["id"], line_items=[
            {"description": "Heating element, 4500W", "quantity": 1, "unit_price": 38.75},
            {"description": "Thermostat", "quantity": 1, "unit_price": 24.5},
            {"description": "Labor", "quantity": 1.5, "unit_price": 95},
        ])
        for item in quote["line_items"]:
            say(f"    {item['quantity']:>4g} x {item['description']:<24} {money(item['unit_price']):>9}")
        say(f"    {quote['id']}  status={quote['status']}  total={money(quote['total'])}")

        step(7, "Confirm what changed by reading it back")
        schedule = await call(client, "get_schedule", date_from="2030-01-08", technician_id=technician_id)
        for day in schedule["days"]:
            for entry in day["entries"]:
                say(f"    {day['technician_name']}: {clock(entry['start'])} to {clock(entry['end'])}  "
                    f"{entry['job_title']}  for {entry['customer_name']}")
        quotes = await call(client, "list_quotes", customer_id=customer["id"])
        say(f"    quotes on file for {customer['name']}: "
            + ", ".join(f"{q['id']} {q['status']} {money(q['total'])}" for q in quotes["items"]))

        step(8, "A new caller: create the customer and a job")
        new_customer = await call(client, "create_customer", name="Robin Fabricated", phone="555-0142",
                                  email="robin.fabricated@example.com",
                                  address={"line1": "700 Invented Way", "city": "Sampletown", "region": "ZZ",
                                           "postal_code": "00007"})
        say(f"    created {new_customer['id']}  {new_customer['name']}")
        new_job = await call(client, "create_job", customer_id=new_customer["id"], title="Replace storm door",
                             description="Front entry. Customer has the door on site.")
        say(f"    created {new_job['id']}  {new_job['title']}  status={new_job['status']}")
        await call(client, "get_job", job_id="WO-9999")

    step(9, "Same data, read-only mode (FSM_READ_ONLY=true)")
    async with Client(build_server(adapter, Settings(read_only=True))) as client:
        names = [tool.name for tool in (await client.list_tools()).tools]
        say(f"    {len(names)} tools offered, create tools offered: "
            f"{sorted(name for name in names if name.startswith('create_')) or 'none'}")
        jobs = await call(client, "list_jobs", status="open")
        say(f"    open jobs: " + ", ".join(f"{j['id']} {j['title']}" for j in jobs["items"]))

    await adapter.aclose()
    say()
    say("Demo finished. Nothing left this process.")


if __name__ == "__main__":
    asyncio.run(main())
