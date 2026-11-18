"""The sync pane's data (CORE-606): the snapshot the gateway writes and the backend route serves."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

from issue_sync import pane
from issue_sync.adapters.fake import FakeAdapter
from issue_sync.connections import parse_connections
from issue_sync.engine import SyncEngine
from issue_sync.kanban import FakeKanban
from issue_sync.state import MemoryStore, SyncState

REPO = Path(__file__).resolve().parent.parent
STATE_MAP = {"running": "In Progress", "done": "Done"}


def _setup(tmp_path):
    (conn,) = parse_connections({"connections": [
        {"id": "lin", "tracker": "fake", "projects": ["Sandbox"], "label": "agent", "board": "b",
         "api_key_env": "SECRET_NAME_ONLY"}]})
    adapter, kanban = FakeAdapter(), FakeKanban()
    state = SyncState(MemoryStore(), namespace="lin")
    engine = SyncEngine(adapter, kanban, state, {"state_map": STATE_MAP, "max_active": 0}, conn="lin")
    return conn, adapter, kanban, state, engine


def test_snapshot_shows_connection_items_and_waiting_question(tmp_path):
    conn, adapter, kanban, state, engine = _setup(tmp_path)
    adapter.add_issue("F-1", "needs an answer")
    adapter.add_issue("F-2", "plain")
    engine.tick(force=True)
    card = next(cid for cid, c in kanban.cards.items() if "F-1" in c["title"])
    kanban.block(card, "English or French?")
    engine.tick(force=True)

    block = pane.connection_snapshot(conn, state, engine.last_cards, None, True)
    assert block["tracker"] == "fake" and block["projects"] == ["Sandbox"] and block["board"] == "b"
    assert block["counts"] == {"linked": 2, "waiting": 1, "errors": 0}
    first = block["items"][0]  # waiting items sort first
    assert first["key"] == "F-1" and first["waiting"] and first["question"] == "English or French?"
    assert first["title"] == "needs an answer" and first["url"].endswith("/F-1")
    assert "SECRET_NAME_ONLY" not in json.dumps(block)  # credential setting names never reach the pane


def test_answered_question_is_no_longer_waiting(tmp_path):
    conn, adapter, kanban, state, engine = _setup(tmp_path)
    adapter.add_issue("F-1", "q")
    engine.tick(force=True)
    card = next(iter(kanban.cards))
    kanban.block(card, "Q?")
    engine.tick(force=True)
    adapter.post_comment("F-1", "A.")
    engine.tick(force=True)
    block = pane.connection_snapshot(conn, state, engine.last_cards, None, True)
    assert block["counts"]["waiting"] == 0 and block["items"][0]["question"] is None


def test_archived_cards_leave_the_panel(tmp_path):
    conn, adapter, kanban, state, engine = _setup(tmp_path)
    adapter.add_issue("F-1", "old test")
    adapter.add_issue("F-2", "live")
    engine.tick(force=True)
    old = next(cid for cid, c in kanban.cards.items() if "F-1" in c["title"])
    kanban.set_status(old, "archived")
    engine.tick(force=True)
    block = pane.connection_snapshot(conn, state, engine.last_cards, None, True)
    assert [i["key"] for i in block["items"]] == ["F-2"]
    assert block["counts"]["linked"] == 1


def test_broken_connection_is_reported_without_items(tmp_path):
    (conn,) = parse_connections({"connections": [{"id": "bad", "tracker": "nope"}]})
    block = pane.connection_snapshot(conn, SyncState(MemoryStore(), namespace="bad"), [], "unknown tracker", False)
    assert block["running"] is False and block["error"] == "unknown tracker" and block["items"] == []
    assert block["counts"]["errors"] == 1


def test_write_and_read_round_trip_and_missing_file(tmp_path):
    assert pane.read_snapshot(tmp_path)["connections"] == []
    pane.write_snapshot(tmp_path, pane.build_snapshot([{"name": "x"}]))
    assert pane.read_snapshot(tmp_path)["connections"] == [{"name": "x"}]
    assert not list(tmp_path.glob(".pane-*"))  # no temp files left behind


def test_register_publishes_snapshot_after_each_tick(plugin, stub_ctx, tmp_path):
    from importlib import import_module
    kanban = import_module("hermes_issue_sync.issue_sync.kanban").FakeKanban()
    ctx = stub_ctx({"connections": [{"id": "a", "tracker": "fake", "projects": ["P"]}]})
    plugin.register(ctx, kanban=kanban, background=False, pane_dir=tmp_path)
    snap = pane.read_snapshot(tmp_path)
    assert [c["name"] for c in snap["connections"]] == ["a"] and snap["connections"][0]["last_poll_at"] is None
    ctx.hooks["on_kanban_dispatch_tick"](board="default")
    assert pane.read_snapshot(tmp_path)["connections"][0]["last_poll_at"] is not None


def test_backend_route_serves_the_snapshot(tmp_path, monkeypatch):
    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_data_dir = lambda name: tmp_path / name
    monkeypatch.setitem(sys.modules, "plugins", types.ModuleType("plugins"))
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)
    spec = importlib.util.spec_from_file_location("issue_sync_plugin_api", REPO / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)

    assert asyncio.run(api.status())["connections"] == []  # nothing written yet
    pane.write_snapshot(tmp_path / "hermes-issue-sync", pane.build_snapshot([{"name": "lin"}]))
    assert asyncio.run(api.status())["connections"] == [{"name": "lin"}]


def test_snapshot_carries_tree_style_and_defaults_to_rounded():
    assert pane.build_snapshot([])["display"]["tree_style"] == "rounded"
    assert pane.build_snapshot([], tree_style="lines")["display"]["tree_style"] == "lines"
    assert pane.build_snapshot([], tree_style=" LINES ")["display"]["tree_style"] == "lines"
    assert pane.build_snapshot([], tree_style="zigzag")["display"]["tree_style"] == "rounded"


def _done_card_with_landing(tmp_path, landing):
    conn, adapter, kanban, state, engine = _setup(tmp_path)
    adapter.add_issue("F-1", "lands later")
    engine.tick(force=True)
    card_id = next(iter(kanban.cards))
    kanban.cards[card_id]["status"] = "done"
    cards = [dict(c, id=cid) for cid, c in kanban.cards.items()]
    from issue_sync.links import link_from_card
    state.update_link(link_from_card(cards[0]).id, card_id=card_id, landing=landing)
    return pane.connection_snapshot(conn, state, cards, None, True)["items"][0]


def test_landing_waiting_item_carries_the_land_scripts_last_line(tmp_path):
    item = _done_card_with_landing(tmp_path, {"status": "waiting", "message": "PR #101 set to auto-merge"})
    assert item["card_status"] == "done" and item["landing"] == "waiting"
    assert item["landing_message"] == "PR #101 set to auto-merge"
    assert item["landing_error"] is None and not item["waiting"]


def test_landing_failed_item_is_waiting_with_the_error_as_reason(tmp_path):
    item = _done_card_with_landing(tmp_path, {"status": "failed", "error": "fatal: no remote", "message": "old"})
    assert item["landing"] == "failed" and item["waiting"]
    assert item["landing_error"] == "fatal: no remote" and item["landing_message"] is None


def test_pane_js_renders_landing_marker_and_states_issue_source():
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    script = """
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const body = src.slice(src.indexOf('export function landingInfo'), src.indexOf('function ItemRow'));
const m = {}; new Function('m', body.replace(/export /g, '') + '; m.f = landingInfo; m.t = ISSUE_STATE_TOOLTIP')(m);
console.log(JSON.stringify([
  m.f({card_status: 'done', landing: 'waiting', landing_message: 'PR #101 set to auto-merge'}),
  m.f({card_status: 'done', landing: 'failed', landing_error: 'fatal: no remote'}),
  m.f({card_status: 'done', landing: 'landed'}),
  m.f({card_status: 'running', landing: 'waiting'}),
  m.t]));
"""
    out = subprocess.run([node, "-e", script, str(REPO / "desktop" / "plugin.js")],
                         capture_output=True, text=True, check=True).stdout
    waiting, failed, landed, not_done, tip = json.loads(out)
    assert waiting == {"marker": "landing", "tone": "warn", "line": "PR #101 set to auto-merge"}
    assert failed["line"] == "fatal: no remote" and failed["tone"] == "bad"
    assert landed is None and not_done is None
    assert "Last state Issue Sync pushed" in tip
