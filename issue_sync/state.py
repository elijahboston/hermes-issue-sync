"""Sync cursors and caches, persisted through ``ctx.state`` (profile-scoped plugin state).

The card body is the source of truth for *which* issue a card is linked to (see ``links``).
This state only remembers what we already did per link so the reconciler avoids repeating
tracker calls: the tracker state last pushed (or observed when the link was made), the
comment cursor, whether the completion summary was posted, and the last error.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Protocol

_LINKS = "links"
_LAST_POLL_AT = "last_poll_at"
_BACKOFF_UNTIL = "backoff_until"
_LAST_ERROR = "last_error"

# Per-link fields; unknown fields are rejected so typos fail loudly in tests.
LINK_FIELDS = frozenset({
    "card_id", "last_pushed_state", "comment_cursor", "done_comment_posted", "last_error",
    "asked_block", "answered_block", "question", "landing", "auto_retried", "review_card_id",
})


class StateStore(Protocol):
    """The ``ctx.state`` surface: ``get(key, default)`` / ``set(key, value)`` of JSON values."""

    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any) -> None: ...


# Hermes' rule for ``ctx.state`` keys (hermes_cli/plugins_state.py), mirrored so tests catch bad keys.
_KEY_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def _check_key(key: str) -> None:
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key) or ".." in key:
        raise ValueError(f"invalid plugin state key {key!r}")


class MemoryStore:
    """In-memory ``StateStore`` with ``ctx.state`` semantics (JSON round-trip, copies out, key rules)."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        _check_key(key)
        return copy.deepcopy(self.data[key]) if key in self.data else default

    def set(self, key: str, value: Any) -> None:
        _check_key(key)
        self.data[key] = json.loads(json.dumps(value))


class _Namespaced:
    """Prefix every key with ``conn.<namespace>.`` so several connections share one ``ctx.state``.

    Hermes allows only letters, digits, ``_ - . :`` in state keys (no ``..``); connection ids are
    already limited to ``[a-z0-9_-]``, so the prefixed key stays valid.
    """

    def __init__(self, store: StateStore, namespace: str) -> None:
        self._store, self._prefix = store, f"conn.{namespace}."

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(self._prefix + key, default)

    def set(self, key: str, value: Any) -> None:
        self._store.set(self._prefix + key, value)


class SyncState:
    def __init__(self, store: StateStore | None = None, namespace: str = "") -> None:
        store = store if store is not None else MemoryStore()
        # "" keeps the original un-prefixed keys, so a single-tracker setup keeps its history.
        self._store: StateStore = _Namespaced(store, namespace) if namespace else store

    # ── per link (keyed by Link.id = "<tracker>:<issue_key>") ─────────────

    def links(self) -> dict[str, dict[str, Any]]:
        value = self._store.get(_LINKS, {})
        return value if isinstance(value, dict) else {}

    def link(self, link_id: str) -> dict[str, Any]:
        value = self.links().get(link_id)
        return dict(value) if isinstance(value, dict) else {}

    def update_link(self, link_id: str, **fields: Any) -> dict[str, Any]:
        unknown = set(fields) - LINK_FIELDS
        if unknown:
            raise ValueError(f"unknown link state field(s): {', '.join(sorted(unknown))}")
        links = self.links()
        entry = dict(links.get(link_id) or {})
        entry.update(fields)
        if entry == links.get(link_id):
            return entry
        links[link_id] = entry
        self._store.set(_LINKS, links)
        return entry

    # ── global ────────────────────────────────────────────────────────────

    @property
    def last_poll_at(self) -> float | None:
        return _as_float(self._store.get(_LAST_POLL_AT))

    @last_poll_at.setter
    def last_poll_at(self, value: float | None) -> None:
        self._store.set(_LAST_POLL_AT, value)

    @property
    def backoff_until(self) -> float | None:
        return _as_float(self._store.get(_BACKOFF_UNTIL))

    @backoff_until.setter
    def backoff_until(self, value: float | None) -> None:
        self._store.set(_BACKOFF_UNTIL, value)

    @property
    def last_error(self) -> str | None:
        """Last error that was not tied to one link (tracker fetch, board listing)."""
        value = self._store.get(_LAST_ERROR)
        return value if isinstance(value, str) else None

    @last_error.setter
    def last_error(self, value: str | None) -> None:
        if self._store.get(_LAST_ERROR) != value:
            self._store.set(_LAST_ERROR, value)


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
