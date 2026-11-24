"""A small fake field service REST API.

It stands in for a real vendor so the MCP server, the tests and the demo run
with no network and no credentials. It behaves like a typical vendor API:
API key header, page based lists, camelCase payloads, money in cents, and
business rule errors (double booking, closed work orders).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import MOCK_API_KEY
from .store import MockStore, resolve_anchor

WorkOrderState = Literal["OPEN", "SCHEDULED", "IN_PROGRESS", "DONE", "CANCELLED"]
EstimateState = Literal["DRAFT", "SENT", "APPROVED", "DECLINED"]
Priority = Literal["LOW", "NORMAL", "HIGH"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AddressIn(_Body):
    line1: str = Field(min_length=1)
    city: str = Field(min_length=1)
    region: str = Field(min_length=1)
    postalCode: str = Field(min_length=1)


class ClientIn(_Body):
    displayName: str = Field(min_length=1, max_length=120)
    phone: str | None = None
    email: str | None = None
    serviceAddress: AddressIn | None = None


class WorkOrderIn(_Body):
    clientId: str
    summary: str = Field(min_length=1, max_length=160)
    details: str = ""
    priority: Priority = "NORMAL"


class AppointmentIn(_Body):
    workOrderId: str
    techId: str
    startsAt: datetime
    endsAt: datetime
    notes: str = ""

    @model_validator(mode="after")
    def _window(self) -> "AppointmentIn":
        if self.endsAt <= self.startsAt:
            raise ValueError("endsAt must be after startsAt")
        return self


class EstimateLineIn(_Body):
    label: str = Field(min_length=1)
    qty: float = Field(gt=0)
    unitPriceCents: int = Field(ge=0)


class EstimateIn(_Body):
    clientId: str
    workOrderId: str | None = None
    lines: list[EstimateLineIn] = Field(min_length=1)


class ApiError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(status_code=status_code, detail={"code": code, "message": message})


def _page(rows: list[dict[str, Any]], page: int, page_size: int) -> dict[str, Any]:
    first = (page - 1) * page_size
    return {
        "data": rows[first:first + page_size],
        "meta": {"page": page, "pageSize": page_size, "total": len(rows)},
    }


def create_app(store: MockStore | None = None, api_key: str = MOCK_API_KEY) -> FastAPI:
    """Build the mock API. Each call gets its own store unless one is passed in."""
    store = store or MockStore(anchor=resolve_anchor())
    app = FastAPI(title="Mock Field Service API", version="1.0.0")
    app.state.store = store

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        if x_api_key != api_key:
            raise ApiError(401, "unauthorized", "Missing or invalid X-Api-Key header.")

    auth = [Depends(require_key)]
    page_q = Query(default=1, ge=1)
    size_q = Query(default=25, ge=1, le=100, alias="pageSize")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "anchorDate": store.anchor.isoformat()}

    # -- techs --------------------------------------------------------------- #

    @app.get("/api/v1/techs", dependencies=auth)
    def list_techs() -> dict[str, Any]:
        return {"data": store.techs, "meta": {"page": 1, "pageSize": len(store.techs), "total": len(store.techs)}}

    # -- clients ------------------------------------------------------------- #

    @app.get("/api/v1/clients", dependencies=auth)
    def list_clients(q: str | None = None, page: int = page_q, page_size: int = size_q) -> dict[str, Any]:
        rows = store.clients
        if q:
            needle = q.lower()
            rows = [
                row for row in rows
                if needle in row["displayName"].lower()
                or needle in (row["email"] or "").lower()
                or needle in (row["phone"] or "")
            ]
        return _page(sorted(rows, key=lambda row: row["displayName"].lower()), page, page_size)

    @app.get("/api/v1/clients/{client_id}", dependencies=auth)
    def get_client(client_id: str) -> dict[str, Any]:
        row = store.client(client_id)
        if row is None:
            raise ApiError(404, "not_found", f"Client {client_id} does not exist.")
        return row

    @app.post("/api/v1/clients", status_code=201, dependencies=auth)
    def create_client(body: ClientIn) -> dict[str, Any]:
        address = body.serviceAddress.model_dump() if body.serviceAddress else None
        return store.add_client(body.displayName, body.phone, body.email, address)

    # -- work orders --------------------------------------------------------- #

    @app.get("/api/v1/workorders", dependencies=auth)
    def list_work_orders(client_id: str | None = Query(default=None, alias="clientId"),
                         state: WorkOrderState | None = None,
                         page: int = page_q, page_size: int = size_q) -> dict[str, Any]:
        rows = [
            row for row in store.work_orders
            if (client_id is None or row["clientId"] == client_id) and (state is None or row["state"] == state)
        ]
        return _page(sorted(rows, key=lambda row: row["createdAt"], reverse=True), page, page_size)

    @app.get("/api/v1/workorders/{work_order_id}", dependencies=auth)
    def get_work_order(work_order_id: str) -> dict[str, Any]:
        row = store.work_order(work_order_id)
        if row is None:
            raise ApiError(404, "not_found", f"Work order {work_order_id} does not exist.")
        return row

    @app.post("/api/v1/workorders", status_code=201, dependencies=auth)
    def create_work_order(body: WorkOrderIn) -> dict[str, Any]:
        if store.client(body.clientId) is None:
            raise ApiError(422, "unknown_client", f"Client {body.clientId} does not exist.")
        return store.add_work_order(body.clientId, body.summary, body.details, body.priority)

    # -- appointments -------------------------------------------------------- #

    @app.get("/api/v1/appointments", dependencies=auth)
    def list_appointments(work_order_id: str | None = Query(default=None, alias="workOrderId"),
                          tech_id: str | None = Query(default=None, alias="techId"),
                          window_from: datetime | None = Query(default=None, alias="from"),
                          window_to: datetime | None = Query(default=None, alias="to"),
                          page: int = page_q, page_size: int = size_q) -> dict[str, Any]:
        rows = []
        for row in store.appointments:
            if work_order_id is not None and row["workOrderId"] != work_order_id:
                continue
            if tech_id is not None and row["techId"] != tech_id:
                continue
            if window_from is not None and datetime.fromisoformat(row["endsAt"]) <= window_from:
                continue
            if window_to is not None and datetime.fromisoformat(row["startsAt"]) >= window_to:
                continue
            rows.append(row)
        return _page(sorted(rows, key=lambda row: (row["startsAt"], row["id"])), page, page_size)

    @app.post("/api/v1/appointments", status_code=201, dependencies=auth)
    def create_appointment(body: AppointmentIn) -> dict[str, Any]:
        order = store.work_order(body.workOrderId)
        if order is None:
            raise ApiError(422, "unknown_work_order", f"Work order {body.workOrderId} does not exist.")
        if store.tech(body.techId) is None:
            raise ApiError(422, "unknown_tech", f"Tech {body.techId} does not exist.")
        if order["state"] in ("DONE", "CANCELLED"):
            raise ApiError(409, "work_order_closed",
                           f"Work order {order['id']} is {order['state']} and cannot take new appointments.")
        starts_at = body.startsAt.replace(tzinfo=None)
        ends_at = body.endsAt.replace(tzinfo=None)
        clash = store.find_overlap(body.techId, starts_at, ends_at)
        if clash is not None:
            raise ApiError(409, "tech_double_booked",
                           f"Tech {body.techId} is already booked {clash['startsAt']} to {clash['endsAt']} "
                           f"(appointment {clash['id']}).")
        return store.add_appointment(body.workOrderId, body.techId, starts_at, ends_at, body.notes)

    # -- estimates ----------------------------------------------------------- #

    @app.get("/api/v1/estimates", dependencies=auth)
    def list_estimates(client_id: str | None = Query(default=None, alias="clientId"),
                       state: EstimateState | None = None,
                       page: int = page_q, page_size: int = size_q) -> dict[str, Any]:
        rows = [
            row for row in store.estimates
            if (client_id is None or row["clientId"] == client_id) and (state is None or row["state"] == state)
        ]
        return _page(sorted(rows, key=lambda row: row["createdAt"], reverse=True), page, page_size)

    @app.get("/api/v1/estimates/{estimate_id}", dependencies=auth)
    def get_estimate(estimate_id: str) -> dict[str, Any]:
        row = store.estimate(estimate_id)
        if row is None:
            raise ApiError(404, "not_found", f"Estimate {estimate_id} does not exist.")
        return row

    @app.post("/api/v1/estimates", status_code=201, dependencies=auth)
    def create_estimate(body: EstimateIn) -> dict[str, Any]:
        if store.client(body.clientId) is None:
            raise ApiError(422, "unknown_client", f"Client {body.clientId} does not exist.")
        if body.workOrderId is not None:
            order = store.work_order(body.workOrderId)
            if order is None:
                raise ApiError(422, "unknown_work_order", f"Work order {body.workOrderId} does not exist.")
            if order["clientId"] != body.clientId:
                raise ApiError(422, "client_mismatch",
                               f"Work order {order['id']} belongs to client {order['clientId']}, not {body.clientId}.")
        return store.add_estimate(body.clientId, [line.model_dump() for line in body.lines], body.workOrderId)

    return app
