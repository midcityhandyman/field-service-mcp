"""The mock vendor API behaves like a believable vendor."""

from __future__ import annotations

import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from field_service_mcp.mock_api import DEFAULT_ANCHOR, MockStore, create_app, resolve_anchor

HEADERS = {"X-Api-Key": "mock-key"}


@pytest.fixture
def api() -> TestClient:
    return TestClient(create_app(MockStore()), headers=HEADERS)


def test_health_needs_no_key():
    response = TestClient(create_app(MockStore())).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "anchorDate": "2030-01-07"}


def test_missing_or_wrong_key_is_rejected():
    bare = TestClient(create_app(MockStore()))
    assert bare.get("/api/v1/clients").status_code == 401
    wrong = bare.get("/api/v1/clients", headers={"X-Api-Key": "nope"})
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "unauthorized"


def test_client_list_is_paged_and_searchable(api):
    first = api.get("/api/v1/clients", params={"pageSize": 4}).json()
    assert first["meta"] == {"page": 1, "pageSize": 4, "total": 6}
    assert len(first["data"]) == 4
    second = api.get("/api/v1/clients", params={"pageSize": 4, "page": 2}).json()
    assert len(second["data"]) == 2
    assert {row["id"] for row in first["data"]}.isdisjoint(row["id"] for row in second["data"])

    hits = api.get("/api/v1/clients", params={"q": "casey"}).json()["data"]
    assert [row["displayName"] for row in hits] == ["Casey Testcase"]


def test_page_size_is_capped(api):
    assert api.get("/api/v1/clients", params={"pageSize": 101}).status_code == 422


def test_work_order_filters(api):
    done = api.get("/api/v1/workorders", params={"state": "DONE"}).json()["data"]
    assert [row["summary"] for row in done] == ["Clean and reseal gutters"]
    for_alex = api.get("/api/v1/workorders", params={"clientId": "CL-1001"}).json()
    assert for_alex["meta"]["total"] == 2
    assert api.get("/api/v1/workorders", params={"state": "BOGUS"}).status_code == 422


def test_unknown_records_return_404(api):
    for path in ("clients/CL-0", "workorders/WO-0", "estimates/ES-0"):
        response = api.get(f"/api/v1/{path}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


def test_create_work_order_requires_known_client(api):
    response = api.post("/api/v1/workorders", json={"clientId": "CL-0", "summary": "Fix gate"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_client"

    created = api.post("/api/v1/workorders", json={"clientId": "CL-1001", "summary": "Fix gate"})
    assert created.status_code == 201
    assert created.json()["state"] == "OPEN"
    assert created.json()["id"] == "WO-2009"


def test_appointment_window_filter_uses_overlap(api):
    rows = api.get("/api/v1/appointments",
                   params={"from": "2030-01-07T10:30:00", "to": "2030-01-07T13:30:00"}).json()["data"]
    assert [row["id"] for row in rows] == ["AP-3001", "AP-3003", "AP-3002"]


def test_double_booking_is_refused(api):
    body = {"workOrderId": "WO-2001", "techId": "T-01",
            "startsAt": "2030-01-07T10:00:00", "endsAt": "2030-01-07T12:00:00"}
    response = api.post("/api/v1/appointments", json=body)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "tech_double_booked"

    # Back to back with the 09:00 to 11:00 visit is fine.
    body.update(startsAt="2030-01-07T11:00:00", endsAt="2030-01-07T12:00:00")
    assert api.post("/api/v1/appointments", json=body).status_code == 201


def test_booking_moves_an_open_work_order_to_scheduled(api):
    order = api.post("/api/v1/workorders", json={"clientId": "CL-1003", "summary": "Hang shelf"}).json()
    body = {"workOrderId": order["id"], "techId": "T-02",
            "startsAt": "2030-01-09T09:00:00", "endsAt": "2030-01-09T10:00:00"}
    assert api.post("/api/v1/appointments", json=body).status_code == 201
    assert api.get(f"/api/v1/workorders/{order['id']}").json()["state"] == "SCHEDULED"


def test_closed_work_order_cannot_be_booked(api):
    body = {"workOrderId": "WO-2007", "techId": "T-02",
            "startsAt": "2030-01-09T09:00:00", "endsAt": "2030-01-09T10:00:00"}
    response = api.post("/api/v1/appointments", json=body)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "work_order_closed"


def test_appointment_end_must_follow_start(api):
    body = {"workOrderId": "WO-2001", "techId": "T-02",
            "startsAt": "2030-01-09T10:00:00", "endsAt": "2030-01-09T09:00:00"}
    assert api.post("/api/v1/appointments", json=body).status_code == 422


def test_estimate_total_is_computed_in_cents(api):
    body = {"clientId": "CL-1005", "lines": [
        {"label": "Deck screws", "qty": 2, "unitPriceCents": 1299},
        {"label": "Labor", "qty": 1.5, "unitPriceCents": 9000},
    ]}
    created = api.post("/api/v1/estimates", json=body).json()
    assert created["totalCents"] == 2 * 1299 + 13500
    assert created["state"] == "DRAFT"


def test_estimate_checks_work_order_owner(api):
    body = {"clientId": "CL-1002", "workOrderId": "WO-2001",
            "lines": [{"label": "Labor", "qty": 1, "unitPriceCents": 100}]}
    response = api.post("/api/v1/estimates", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "client_mismatch"


def test_seed_data_is_obviously_fake():
    store = MockStore()
    for row in store.clients:
        assert re.fullmatch(r"555-01\d\d", row["phone"])
        assert row["email"].endswith("example.com")
        assert row["serviceAddress"]["region"] == "ZZ"


def test_anchor_date_can_be_moved(monkeypatch):
    assert resolve_anchor("") == DEFAULT_ANCHOR
    assert resolve_anchor("2031-03-03") == date(2031, 3, 3)
    assert resolve_anchor("today") == date.today()
    monkeypatch.setenv("MOCK_ANCHOR_DATE", "2032-05-05")
    assert resolve_anchor() == date(2032, 5, 5)
    shifted = MockStore(anchor=date(2031, 3, 3))
    assert shifted.appointments[0]["startsAt"] == "2031-03-03T09:00:00"
