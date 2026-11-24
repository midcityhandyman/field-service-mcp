"""Settings parsing and adapter selection."""

from __future__ import annotations

from datetime import time

import pytest

from field_service_mcp.adapters import MockRestAdapter, available_adapters, create_adapter, register_adapter
from field_service_mcp.config import ConfigError, Settings

pytestmark = pytest.mark.anyio


def test_defaults_run_offline_with_no_environment():
    settings = Settings.from_env({})
    assert settings.adapter == "mock"
    assert settings.base_url == ""
    assert settings.read_only is False
    assert (settings.workday_start, settings.workday_end) == (time(8), time(17))
    assert settings.transport == "stdio"


def test_environment_overrides():
    settings = Settings.from_env({
        "FSM_BASE_URL": "http://127.0.0.1:8765/", "FSM_READ_ONLY": "Yes", "FSM_MAX_RETRIES": "0",
        "FSM_TIMEOUT_SECONDS": "2.5", "FSM_WORKDAY_START": "07:30", "FSM_WORKDAY_END": "15:30",
        "MCP_TRANSPORT": "streamable-http", "MCP_PORT": "9001",
    })
    assert settings.base_url == "http://127.0.0.1:8765"
    assert settings.read_only is True
    assert settings.max_retries == 0
    assert settings.timeout_seconds == 2.5
    assert settings.workday_start == time(7, 30)
    assert (settings.transport, settings.port) == ("streamable-http", 9001)


@pytest.mark.parametrize("env", [
    {"FSM_READ_ONLY": "maybe"},
    {"FSM_MAX_RETRIES": "-1"},
    {"FSM_TIMEOUT_SECONDS": "soon"},
    {"FSM_WORKDAY_START": "8am"},
    {"FSM_WORKDAY_START": "18:00", "FSM_WORKDAY_END": "09:00"},
    {"MCP_TRANSPORT": "carrier-pigeon"},
])
def test_bad_values_are_rejected_with_the_variable_name(env):
    with pytest.raises(ConfigError) as caught:
        Settings.from_env(env)
    assert any(name in str(caught.value) for name in env)


async def test_mock_adapter_is_the_default():
    adapter = create_adapter(Settings())
    assert isinstance(adapter, MockRestAdapter)
    assert adapter.in_process is True
    await adapter.aclose()

    remote = create_adapter(Settings(base_url="http://127.0.0.1:8765"))
    assert remote.in_process is False
    await remote.aclose()


def test_adapter_by_dotted_path():
    adapter = create_adapter(Settings(adapter="in_memory_adapter:InMemoryAdapter"))
    assert adapter.name == "in-memory"


def test_adapter_by_registered_name():
    from in_memory_adapter import InMemoryAdapter

    register_adapter("Memory", InMemoryAdapter.from_settings)
    assert "memory" in available_adapters()
    assert isinstance(create_adapter(Settings(adapter="memory")), InMemoryAdapter)


@pytest.mark.parametrize("name", ["nope", "no_such_module:Thing", "in_memory_adapter:Missing",
                                  "in_memory_adapter:NotAnAdapter"])
def test_unknown_adapters_fail_clearly(name):
    with pytest.raises(ConfigError, match="FSM_ADAPTER"):
        create_adapter(Settings(adapter=name))
