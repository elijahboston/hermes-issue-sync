"""Backend for the hermes-issue-sync Desktop pane: serves the latest sync snapshot.

Mounted by Hermes at ``/api/plugins/hermes-issue-sync/``. Hermes imports this file on its own
(not as part of the plugin package), so it stays self-contained: it only reads the snapshot the
gateway-side plugin writes after each sync pass (``issue_sync/pane.py``). It holds no secrets and
never calls a tracker.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter()

DATA_NAME = "hermes-issue-sync"
SNAPSHOT_FILE = "pane.json"
_EMPTY: dict[str, Any] = {"version": 1, "generated_at": None, "enabled": None, "connections": []}


def _read(directory: Path) -> dict[str, Any]:
    try:
        data = json.loads((directory / SNAPSHOT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_EMPTY)
    return data if isinstance(data, dict) else dict(_EMPTY)


@router.get("/status")
async def status() -> dict[str, Any]:
    from plugins.plugin_storage import plugin_data_dir  # documented plugin storage helper

    # File I/O off the event loop (Hermes docs: never block the gateway loop).
    return await asyncio.to_thread(lambda: _read(plugin_data_dir(DATA_NAME)))
