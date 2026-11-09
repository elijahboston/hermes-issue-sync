"""Tracker adapter contract: every issue tracker (Linear, Jira, the fake) implements this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class Issue:
    key: str
    title: str
    body: str
    url: str
    state: str
    labels: tuple[str, ...] = ()
    updated_at: str | None = None  # ISO-8601, as reported by the tracker
    closed: bool = False  # finished or canceled, by the tracker's status type (not its name)


@dataclass(frozen=True)
class Comment:
    id: str
    author: str
    body: str
    created_at: str | None = None  # ISO-8601
    is_bot: bool = False


def filter_projects(filter: Mapping[str, Any] | None) -> list[str]:
    """Projects named by a filter: ``projects`` (list) and/or a single ``project``."""
    filter = filter or {}
    value = filter.get("projects")
    items = list(value) if isinstance(value, (list, tuple)) else ([value] if value else [])
    if filter.get("project"):
        items.insert(0, filter["project"])
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


class AdapterError(Exception):
    """A tracker call failed. ``str(exc)`` is shown to users: keep it human, never include secrets."""


class RateLimited(AdapterError):
    """The tracker asked us to slow down; retry no sooner than ``retry_after`` seconds."""

    def __init__(self, retry_after: float, message: str = "") -> None:
        self.retry_after = max(0.0, float(retry_after or 0.0))
        super().__init__(message or f"rate limited; retry after {self.retry_after:g}s")


class TransitionNotAllowed(AdapterError):
    """The tracker's workflow does not allow moving the issue to the requested state."""


class TrackerAdapter(ABC):
    """One tracker connection. Constructors must not touch the network; ``check()`` probes."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Tracker id used in card markers and idempotency keys (e.g. ``linear``)."""

    @abstractmethod
    def fetch_issues(self, filter: Mapping[str, Any]) -> list[Issue]:
        """Issues matching the user's tracker-specific ``filter`` setting."""

    @abstractmethod
    def move_issue(self, key: str, target_state: str) -> None:
        """Move issue ``key`` to the tracker state NAMED ``target_state`` (adapters translate to ids)."""

    @abstractmethod
    def post_comment(self, key: str, body: str) -> Comment:
        """Post ``body`` as a comment on issue ``key``."""

    @abstractmethod
    def list_comments_since(self, key: str, cursor: str | None) -> tuple[list[Comment], str | None]:
        """Comments newer than ``cursor`` (``None`` = from the start) and the cursor to pass next time."""

    @abstractmethod
    def check(self) -> None:
        """Probe credentials/config; raise ``AdapterError`` with a human-readable message on failure."""
