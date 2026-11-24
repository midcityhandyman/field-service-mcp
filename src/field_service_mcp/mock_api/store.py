"""In-memory data for the mock vendor API.

Everything here is invented. Names, phone numbers (555-01xx), emails
(example.com) and addresses are placeholders and do not describe anyone.

Records use the mock vendor's own vocabulary on purpose (clients, work
orders, appointments, estimates, camelCase keys, money in cents, upper case
states) so that the adapter has real translation work to do.
"""

from __future__ import annotations

import os
import threading
from datetime import date, datetime, time, timedelta
from typing import Any

DEFAULT_ANCHOR = date(2030, 1, 7)  # a Monday, fixed so demos and tests are reproducible


def resolve_anchor(raw: str | None = None) -> date:
    """Return the date the seed schedule is built around.

    ``MOCK_ANCHOR_DATE`` may be an ISO date or the word ``today``.
    """
    raw = (raw if raw is not None else os.environ.get("MOCK_ANCHOR_DATE", "")).strip().lower()
    if not raw:
        return DEFAULT_ANCHOR
    if raw == "today":
        return date.today()
    return date.fromisoformat(raw)


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


class MockStore:
    """Thread-safe in-memory tables with deterministic ids and timestamps."""

    def __init__(self, anchor: date | None = None, seed: bool = True) -> None:
        self.anchor = anchor or DEFAULT_ANCHOR
        self._lock = threading.RLock()
        self._clock = datetime.combine(self.anchor, time(12, 0))
        self._counters = {"CL": 1000, "WO": 2000, "AP": 3000, "ES": 4000}
        self.techs: list[dict[str, Any]] = []
        self.clients: list[dict[str, Any]] = []
        self.work_orders: list[dict[str, Any]] = []
        self.appointments: list[dict[str, Any]] = []
        self.estimates: list[dict[str, Any]] = []
        if seed:
            self._seed()

    # -- primitives ---------------------------------------------------------- #

    def _next_id(self, prefix: str) -> str:
        self._counters[prefix] += 1
        return f"{prefix}-{self._counters[prefix]}"

    def _tick(self) -> str:
        """Deterministic 'now': one minute later on every write."""
        self._clock += timedelta(minutes=1)
        return _iso(self._clock)

    @staticmethod
    def _find(rows: list[dict[str, Any]], record_id: str) -> dict[str, Any] | None:
        return next((row for row in rows if row["id"] == record_id), None)

    def client(self, record_id: str) -> dict[str, Any] | None:
        return self._find(self.clients, record_id)

    def work_order(self, record_id: str) -> dict[str, Any] | None:
        return self._find(self.work_orders, record_id)

    def estimate(self, record_id: str) -> dict[str, Any] | None:
        return self._find(self.estimates, record_id)

    def tech(self, record_id: str) -> dict[str, Any] | None:
        return self._find(self.techs, record_id)

    # -- writes -------------------------------------------------------------- #

    def add_client(self, display_name: str, phone: str | None, email: str | None,
                   service_address: dict[str, str] | None) -> dict[str, Any]:
        with self._lock:
            row = {
                "id": self._next_id("CL"),
                "displayName": display_name,
                "phone": phone,
                "email": email,
                "serviceAddress": service_address,
                "createdAt": self._tick(),
            }
            self.clients.append(row)
            return row

    def add_work_order(self, client_id: str, summary: str, details: str = "",
                       priority: str = "NORMAL", state: str = "OPEN") -> dict[str, Any]:
        with self._lock:
            row = {
                "id": self._next_id("WO"),
                "clientId": client_id,
                "summary": summary,
                "details": details,
                "state": state,
                "priority": priority,
                "createdAt": self._tick(),
            }
            self.work_orders.append(row)
            return row

    def find_overlap(self, tech_id: str, starts_at: datetime, ends_at: datetime) -> dict[str, Any] | None:
        for row in self.appointments:
            if row["techId"] != tech_id or row["state"] != "BOOKED":
                continue
            if datetime.fromisoformat(row["startsAt"]) < ends_at and datetime.fromisoformat(row["endsAt"]) > starts_at:
                return row
        return None

    def add_appointment(self, work_order_id: str, tech_id: str, starts_at: datetime, ends_at: datetime,
                        notes: str = "", state: str = "BOOKED") -> dict[str, Any]:
        with self._lock:
            row = {
                "id": self._next_id("AP"),
                "workOrderId": work_order_id,
                "techId": tech_id,
                "startsAt": _iso(starts_at),
                "endsAt": _iso(ends_at),
                "state": state,
                "notes": notes,
            }
            self.appointments.append(row)
            order = self.work_order(work_order_id)
            if order is not None and order["state"] == "OPEN" and state == "BOOKED":
                order["state"] = "SCHEDULED"
            return row

    def add_estimate(self, client_id: str, lines: list[dict[str, Any]], work_order_id: str | None = None,
                     state: str = "DRAFT") -> dict[str, Any]:
        with self._lock:
            total = sum(round(line["qty"] * line["unitPriceCents"]) for line in lines)
            row = {
                "id": self._next_id("ES"),
                "clientId": client_id,
                "workOrderId": work_order_id,
                "state": state,
                "lines": [dict(line) for line in lines],
                "totalCents": int(total),
                "createdAt": self._tick(),
            }
            self.estimates.append(row)
            return row

    # -- seed ---------------------------------------------------------------- #

    def _at(self, day_offset: int, hour: int, minute: int = 0) -> datetime:
        return datetime.combine(self.anchor + timedelta(days=day_offset), time(hour, minute))

    def _seed(self) -> None:
        self.techs = [
            {"id": "T-01", "name": "Sam Example", "skills": ["plumbing", "general repair"]},
            {"id": "T-02", "name": "Jordan Placeholder", "skills": ["electrical", "hvac"]},
            {"id": "T-03", "name": "Riley Demo", "skills": ["carpentry", "drywall", "painting"]},
        ]

        def addr(number: int, street: str, postal: str) -> dict[str, str]:
            return {"line1": f"{number} {street}", "city": "Sampletown", "region": "ZZ", "postalCode": postal}

        people = [
            ("Alex Sample", "555-0101", "alex.sample@example.com", addr(100, "Example Street", "00001")),
            ("Casey Testcase", "555-0102", "casey.testcase@example.com", addr(200, "Placeholder Avenue", "00002")),
            ("Morgan Fixture", "555-0103", "morgan.fixture@example.com", addr(300, "Mockingbird Lane", "00003")),
            ("Taylor Mockwell", "555-0104", "taylor.mockwell@example.com", addr(400, "Demo Court", "00004")),
            ("Jamie Synthetic", "555-0105", "jamie.synthetic@example.com", addr(500, "Fictional Road", "00005")),
            ("Sample Property Group", "555-0106", "office@sample-property.example.com", addr(600, "Test Plaza", "00006")),
        ]
        for name, phone, email, address in people:
            self.add_client(name, phone, email, address)
        alex, casey, morgan, taylor, jamie, group = (row["id"] for row in self.clients)

        faucet = self.add_work_order(alex, "Replace kitchen faucet", "Single handle faucet drips at the base.")
        panel = self.add_work_order(casey, "Add two outlets in garage", "Customer wants outlets near the workbench.", "HIGH")
        drywall = self.add_work_order(morgan, "Patch drywall in hallway", "Two holes, roughly palm sized.", "LOW")
        heater = self.add_work_order(taylor, "Water heater not heating", "No hot water since the weekend.", "HIGH")
        deck = self.add_work_order(jamie, "Repair loose deck boards", "Three boards near the steps.")
        turnover = self.add_work_order(group, "Unit 2B turnover punch list", "Paint touch ups, door hardware, caulk.")
        gutter = self.add_work_order(alex, "Clean and reseal gutters", "Finished last week.", "LOW", state="DONE")
        self.add_work_order(casey, "Quote request: bathroom fan", "Customer asked for a price before booking.")

        # Monday (anchor day)
        self.add_appointment(faucet["id"], "T-01", self._at(0, 9), self._at(0, 11), "Bring 3/8 supply lines.")
        self.add_appointment(heater["id"], "T-01", self._at(0, 13), self._at(0, 15), "Check thermostat and element first.")
        self.add_appointment(panel["id"], "T-02", self._at(0, 10), self._at(0, 12, 30))
        # Tuesday
        self.add_appointment(drywall["id"], "T-03", self._at(1, 8), self._at(1, 10))
        self.add_appointment(turnover["id"], "T-03", self._at(1, 11), self._at(1, 16), "Keys are in the lockbox.")
        # Wednesday
        self.add_appointment(deck["id"], "T-03", self._at(2, 9), self._at(2, 12))
        # Last week, already finished
        self.add_appointment(gutter["id"], "T-01", self._at(-5, 9), self._at(-5, 11), state="COMPLETED")
        heater["state"] = "IN_PROGRESS"

        self.add_estimate(
            alex,
            [{"label": "Faucet, single handle", "qty": 1, "unitPriceCents": 14900},
             {"label": "Labor", "qty": 2, "unitPriceCents": 9500}],
            work_order_id=faucet["id"], state="APPROVED",
        )
        self.add_estimate(
            casey,
            [{"label": "20A outlet with box", "qty": 2, "unitPriceCents": 4200},
             {"label": "Wire and conduit", "qty": 1, "unitPriceCents": 6800},
             {"label": "Labor", "qty": 2.5, "unitPriceCents": 11000}],
            work_order_id=panel["id"], state="SENT",
        )
        self.add_estimate(
            group,
            [{"label": "Turnover package, one bedroom", "qty": 1, "unitPriceCents": 85000}],
            work_order_id=turnover["id"], state="DRAFT",
        )
