"""Connections: which tracker projects sync to which kanban board.

A connection is one tracker login (a Linear workspace or a Jira site) plus the projects it
pulls from and the board its cards go to. Settings hold a ``connections`` list; the older
single-tracker settings (``tracker``, ``filter``, ...) still work and become one connection
with an empty id, so cards and state made before connections existed keep resolving.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

# Settings each connection may override; anything missing falls back to the top-level value.
SHARED_KEYS = ("state_map", "assignee", "board", "poll_interval_seconds",
               "api_key_env", "base_url_env", "email_env", "token_env",
               "admit", "max_active", "order", "leaf_only", "skip_blocked", "exclude_label",
               "prepare_command", "prepare_timeout_seconds",
               "done_gate", "land_command", "land_timeout_seconds", "land_retry_seconds",
               "merged_base")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class Connection:
    id: str  # "" only for the legacy single-tracker setup
    tracker: str
    settings: dict[str, Any] = field(default_factory=dict)  # adapter + engine settings
    error: str | None = None  # set when this entry is unusable; the others still run

    @property
    def board(self) -> str:
        return str(self.settings.get("board") or "")

    @property
    def projects(self) -> list[str]:
        return list((self.settings.get("filter") or {}).get("projects") or [])

    @property
    def label(self) -> str:
        return str((self.settings.get("filter") or {}).get("label") or "agent")

    @property
    def name(self) -> str:
        return self.id or self.tracker or "default"


def _projects(raw: Mapping[str, Any]) -> list[str]:
    """``projects`` (list or comma string) plus a single ``project``, de-duplicated in order."""
    value = raw.get("projects")
    items: list[Any] = value if isinstance(value, list) else str(value or "").split(",")
    if raw.get("project"):
        items = [raw["project"], *items]
    seen: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def _filter(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise a connection's filter: ``projects`` always a list, ``project`` folded in."""
    raw_filter = raw.get("filter")
    base: dict[str, Any] = dict(raw_filter) if isinstance(raw_filter, Mapping) else {}
    merged = {**base, **{k: raw[k] for k in ("project", "projects", "label", "team", "jql") if raw.get(k)}}
    out = {k: v for k, v in merged.items() if k not in ("project", "projects")}
    projects = _projects(merged)
    if projects:
        out["projects"] = projects
    return out


def _done_gate(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in ("immediate", "merged", "command") else "immediate"


def _has_land_command(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple)):
        return any(str(part).strip() for part in value)
    return False


def _build(raw: Mapping[str, Any], defaults: Mapping[str, Any], conn_id: str) -> Connection:
    tracker = str(raw.get("tracker") or "").strip().lower()
    settings = {key: raw[key] if raw.get(key) not in (None, "") else defaults.get(key) for key in SHARED_KEYS}
    settings.update(tracker=tracker, filter=_filter(raw))
    for key in ("assignee", "board", "api_key_env", "base_url_env", "email_env", "token_env"):
        settings[key] = str(settings.get(key) or "").strip()
    if not tracker:
        return Connection(conn_id, "", settings, "no tracker set")
    # ``admit: all`` with no projects would mean the whole workspace; refuse that.
    admit = str(settings.get("admit") or "label").strip().lower()
    if admit == "all" and not Connection(conn_id, tracker, settings).projects:
        return Connection(conn_id, tracker, settings, "admit: all requires at least one project")
    gate = _done_gate(settings.get("done_gate"))
    settings["done_gate"] = gate
    settings["merged_base"] = str(settings.get("merged_base") or "").strip()
    if gate == "command" and not _has_land_command(settings.get("land_command")):
        return Connection(conn_id, tracker, settings, "done_gate: command requires land_command")
    if gate == "merged" and not settings["merged_base"]:
        return Connection(conn_id, tracker, settings, "done_gate: merged requires merged_base")
    return Connection(conn_id, tracker, settings)


def parse_connections(settings: Mapping[str, Any]) -> list[Connection]:
    """Every configured connection. Bad entries come back with ``error`` set, never raise."""
    raw_list = settings.get("connections")
    if not raw_list:
        if not str(settings.get("tracker") or "").strip():
            return []
        return [_build(settings, settings, "")]
    if not isinstance(raw_list, list):
        return [Connection("connections", "", {}, "`connections` must be a list")]
    out: list[Connection] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_list, 1):
        if not isinstance(raw, Mapping):
            out.append(Connection(f"#{index}", "", {}, "each connection must be a mapping"))
            continue
        conn_id = str(raw.get("id") or "").strip().lower()
        if not _ID_RE.match(conn_id):
            out.append(Connection(conn_id or f"#{index}", str(raw.get("tracker") or ""), {},
                                  "`id` must be lowercase letters, digits, '-' or '_'"))
            continue
        if conn_id in seen:
            out.append(Connection(conn_id, str(raw.get("tracker") or ""), {}, "duplicate connection id"))
            continue
        seen.add(conn_id)
        out.append(_build(raw, settings, conn_id))
    return out
