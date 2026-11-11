"""The sync pane's data: what each connection syncs, its health, and its linked issues.

The gateway side builds a snapshot after every sync pass and writes it to the plugin's data
folder; the Desktop pane reads it through ``dashboard/plugin_api.py``. The snapshot holds no
secrets: only setting names that are safe to show (tracker, projects, label, board) and issue
keys, titles, URLs and states.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from .links import link_from_card

SNAPSHOT_FILE = "pane.json"
DATA_NAME = "hermes-issue-sync"
_MAX_ITEMS = 200


def _card_title(card: Mapping[str, Any]) -> str:
    title = str(card.get("title") or "")
    # Cards are titled "[KEY] title"; the pane shows the key separately.
    return title.split("] ", 1)[1] if title.startswith("[") and "] " in title else title


def connection_snapshot(conn: Any, state: Any, cards: Iterable[Mapping[str, Any]], error: str | None,
                        running: bool) -> dict[str, Any]:
    """One connection's block: settings safe to show, health, and its linked issues."""
    links = state.links() if state is not None else {}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in cards:
        link = link_from_card(card)
        if link is None:
            continue
        seen.add(link.id)
        # Archived cards are cleared off the board, so they leave the panel too.
        if card.get("status") == "archived":
            continue
        entry = links.get(link.id) or {}
        waiting = bool(entry.get("asked_block")) and entry.get("asked_block") != entry.get("answered_block") \
            and card.get("status") == "blocked"
        items.append({
            "key": link.key,
            "url": link.url,
            "title": _card_title(card),
            "card_id": card.get("id"),
            "card_status": card.get("status"),
            "tracker_state": entry.get("last_pushed_state"),
            "waiting": waiting,
            "question": entry.get("question") if waiting else None,
            "error": entry.get("last_error"),
        })
    # Links with an error but no card yet (a failed card create) still need to be visible.
    for link_id, entry in sorted(links.items()):
        if link_id not in seen and isinstance(entry, dict) and entry.get("last_error") and not entry.get("card_id"):
            items.append({"key": link_id.rsplit(":", 1)[-1], "url": "", "title": "", "card_id": None,
                          "card_status": None, "tracker_state": None, "waiting": False,
                          "question": None, "error": entry["last_error"]})
    order = {"blocked": 0, "running": 1, "ready": 2, "review": 3, "todo": 4, "done": 5, "archived": 6}
    items.sort(key=lambda i: (not i["waiting"], not i["error"], order.get(i["card_status"] or "", 9), i["key"]))
    backoff = state.backoff_until if state is not None else None
    return {
        "id": conn.id,
        "name": conn.name,
        "tracker": conn.tracker,
        "projects": conn.projects,
        "label": conn.label,
        "board": conn.board or "default",
        "running": running,
        "error": error,
        "last_poll_at": state.last_poll_at if state is not None else None,
        "last_error": state.last_error if state is not None else None,
        "backoff_until": backoff if backoff and backoff > time.time() else None,
        "items": items[:_MAX_ITEMS],
        "counts": {
            "linked": sum(1 for i in items if i["card_id"]),
            "waiting": sum(1 for i in items if i["waiting"]),
            "errors": sum(1 for i in items if i["error"]) + (1 if error or (state and state.last_error) else 0),
        },
    }


TREE_STYLES = ("rounded", "lines")


def tree_style_setting(value: Any) -> str:
    """The panel's tree connector style; anything unknown falls back to the default ("rounded")."""
    text = str(value or "").strip().lower()
    return text if text in TREE_STYLES else TREE_STYLES[0]


def build_snapshot(blocks: list[dict[str, Any]], *, enabled: bool = True,
                   tree_style: Any = None) -> dict[str, Any]:
    return {"version": 1, "generated_at": time.time(), "enabled": enabled,
            "display": {"tree_style": tree_style_setting(tree_style)}, "connections": blocks}


def write_snapshot(directory: Path, snapshot: Mapping[str, Any]) -> Path:
    """Atomically replace ``<directory>/pane.json`` so a reader never sees half a file."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / SNAPSHOT_FILE
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".pane-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def read_snapshot(directory: Path) -> dict[str, Any]:
    """The last snapshot, or an empty one when the gateway has not written any yet."""
    try:
        data = json.loads((directory / SNAPSHOT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "generated_at": None, "enabled": None, "connections": []}
    return data if isinstance(data, dict) else {"version": 1, "generated_at": None, "connections": []}


def data_dir() -> Path | None:
    """The plugin's data folder via Hermes' documented ``plugin_data_dir``; None outside Hermes."""
    try:
        from plugins.plugin_storage import plugin_data_dir
    except ImportError:
        return None
    return plugin_data_dir(DATA_NAME)
