"""Shared fixtures. Everything runs in process: no ports, no network, no keys."""

from __future__ import annotations

import pytest
from mcp import Client

from field_service_mcp.adapters import MockRestAdapter
from field_service_mcp.config import Settings
from field_service_mcp.server import build_server


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def adapter():
    instance = MockRestAdapter(backoff_seconds=0)
    yield instance
    await instance.aclose()


@pytest.fixture
async def client(adapter):
    async with Client(build_server(adapter, Settings())) as session:
        yield session


def text_of(result) -> str:
    """Join the text blocks of a tool result."""
    return "\n".join(block.text for block in result.content if getattr(block, "text", None))
