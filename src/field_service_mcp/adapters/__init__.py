"""Adapter registry.

``FSM_ADAPTER`` selects the adapter. It is either a registered short name
(``mock``) or a dotted path ``package.module:ClassName`` to a class that
subclasses FieldServiceAdapter and offers ``from_settings(settings)``.
"""

from __future__ import annotations

import importlib
from typing import Callable

from ..config import ConfigError, Settings
from .base import MAX_PAGE_SIZE, FieldServiceAdapter
from .mock_rest import MockRestAdapter

AdapterFactory = Callable[[Settings], FieldServiceAdapter]

_REGISTRY: dict[str, AdapterFactory] = {}


def register_adapter(name: str, factory: AdapterFactory) -> None:
    """Make an adapter selectable by short name."""
    _REGISTRY[name.strip().lower()] = factory


def available_adapters() -> list[str]:
    return sorted(_REGISTRY)


def _load_dotted(path: str) -> AdapterFactory:
    module_name, _, attr = path.partition(":")
    try:
        target = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise ConfigError(f"FSM_ADAPTER={path!r} could not be imported: {exc}") from exc
    if not (isinstance(target, type) and issubclass(target, FieldServiceAdapter)):
        raise ConfigError(f"FSM_ADAPTER={path!r} is not a FieldServiceAdapter subclass")
    factory = getattr(target, "from_settings", None)
    if factory is None:
        raise ConfigError(f"{path} must define a from_settings(settings) classmethod")
    return factory


def create_adapter(settings: Settings) -> FieldServiceAdapter:
    """Build the adapter named by the settings."""
    key = settings.adapter.strip()
    if ":" in key:
        return _load_dotted(key)(settings)
    factory = _REGISTRY.get(key.lower())
    if factory is None:
        known = ", ".join(available_adapters())
        raise ConfigError(f"Unknown FSM_ADAPTER {key!r}. Known adapters: {known}. "
                          "Use package.module:ClassName for an adapter that is not registered.")
    return factory(settings)


register_adapter("mock", MockRestAdapter.from_settings)

__all__ = [
    "MAX_PAGE_SIZE",
    "FieldServiceAdapter",
    "MockRestAdapter",
    "available_adapters",
    "create_adapter",
    "register_adapter",
]
