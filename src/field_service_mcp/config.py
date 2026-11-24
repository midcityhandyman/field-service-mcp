"""Settings read from environment variables. No file IO, no secrets on disk."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off", ""}

# The bundled mock API accepts this key. It is a public placeholder, not a secret.
MOCK_API_KEY = "mock-key"


class ConfigError(ValueError):
    """A setting has a value the server cannot use."""


def _bool(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _clock(name: str, raw: str) -> time:
    try:
        return time.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be HH:MM, got {raw!r}") from exc


def _number(name: str, raw: str, kind: type, minimum: float) -> float:
    try:
        value = kind(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {raw!r}")
    return value


@dataclass(frozen=True)
class Settings:
    adapter: str = "mock"
    base_url: str = ""
    api_key: str = MOCK_API_KEY
    read_only: bool = False
    timeout_seconds: float = 10.0
    max_retries: int = 2
    workday_start: time = time(8, 0)
    workday_end: time = time(17, 0)
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        defaults = cls()

        def get(name: str, default: str) -> str:
            return env.get(name, default)

        settings = cls(
            adapter=get("FSM_ADAPTER", defaults.adapter).strip() or defaults.adapter,
            base_url=get("FSM_BASE_URL", "").strip().rstrip("/"),
            api_key=get("FSM_API_KEY", defaults.api_key).strip(),
            read_only=_bool("FSM_READ_ONLY", get("FSM_READ_ONLY", "false")),
            timeout_seconds=float(_number("FSM_TIMEOUT_SECONDS", get("FSM_TIMEOUT_SECONDS", "10"), float, 0.1)),
            max_retries=int(_number("FSM_MAX_RETRIES", get("FSM_MAX_RETRIES", "2"), int, 0)),
            workday_start=_clock("FSM_WORKDAY_START", get("FSM_WORKDAY_START", "08:00")),
            workday_end=_clock("FSM_WORKDAY_END", get("FSM_WORKDAY_END", "17:00")),
            transport=get("MCP_TRANSPORT", defaults.transport).strip().lower(),
            host=get("MCP_HOST", defaults.host).strip(),
            port=int(_number("MCP_PORT", get("MCP_PORT", "8000"), int, 1)),
        )
        if settings.workday_end <= settings.workday_start:
            raise ConfigError("FSM_WORKDAY_END must be after FSM_WORKDAY_START")
        if settings.transport not in {"stdio", "streamable-http"}:
            raise ConfigError("MCP_TRANSPORT must be stdio or streamable-http")
        return settings
