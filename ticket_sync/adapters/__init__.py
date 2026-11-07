"""Adapter registry: tracker name -> factory(settings) -> TrackerAdapter."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .base import AdapterError, Comment, Issue, RateLimited, TrackerAdapter, TransitionNotAllowed
from .fake import FakeAdapter

AdapterFactory = Callable[[Mapping[str, Any]], TrackerAdapter]

_REGISTRY: dict[str, AdapterFactory] = {}


def register_adapter(name: str, factory: AdapterFactory) -> None:
    if not name or not name.strip():
        raise ValueError("adapter name must be non-empty")
    _REGISTRY[name.strip().lower()] = factory


def get_adapter(name: str, settings: Mapping[str, Any]) -> TrackerAdapter:
    factory = _REGISTRY.get((name or "").strip().lower())
    if factory is None:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise AdapterError(f"unknown tracker {name!r} (available: {known})")
    return factory(settings)


def available_adapters() -> list[str]:
    return sorted(_REGISTRY)


def _linear(settings: Mapping[str, Any]) -> TrackerAdapter:
    # Imported on use: the HTTP adapters import ``ticket_sync.http``, which imports ``.base``.
    from .linear import LinearAdapter
    return LinearAdapter(settings)


def _jira(settings: Mapping[str, Any]) -> TrackerAdapter:
    from .jira import JiraAdapter
    return JiraAdapter(settings)


register_adapter("fake", lambda settings: FakeAdapter())
register_adapter("linear", _linear)
register_adapter("jira", _jira)

__all__ = [
    "AdapterError", "AdapterFactory", "Comment", "FakeAdapter", "Issue", "RateLimited",
    "TrackerAdapter", "TransitionNotAllowed", "available_adapters", "get_adapter", "register_adapter",
]
