"""Several connections at once (CORE-616): settings parsing, isolation, and legacy compatibility."""

from __future__ import annotations

import importlib

from issue_sync.adapters.base import AdapterError, filter_projects
from issue_sync.adapters.fake import FakeAdapter
from issue_sync.adapters.jira import build_jql
from issue_sync.connections import parse_connections
from issue_sync.engine import SyncEngine
from issue_sync.kanban import FakeKanban
from issue_sync.links import Link, card_body, card_title, idempotency_key, link_from_card, parse_marker
from issue_sync.state import MemoryStore, SyncState

PACKAGE = "hermes_issue_sync"
STATE_MAP = {"running": "In Progress", "done": "Done"}


# ── settings ─────────────────────────────────────────────────────────────


def test_legacy_single_tracker_becomes_one_unnamed_connection():
    (conn,) = parse_connections({"tracker": "jira", "filter": {"project": "TSS", "label": "agent"},
                                 "board": "b", "state_map": STATE_MAP})
    assert conn.id == "" and conn.tracker == "jira" and conn.error is None
    assert conn.projects == ["TSS"] and conn.board == "b"
    assert conn.settings["state_map"] == STATE_MAP


def test_nothing_configured_is_empty():
    assert parse_connections({}) == []
    assert parse_connections({"tracker": "  ", "connections": []}) == []


def test_connections_list_inherits_top_level_defaults_and_overrides():
    conns = parse_connections({
        "state_map": STATE_MAP, "assignee": "worker", "board": "shared",
        "connections": [
            {"id": "lin", "tracker": "linear", "projects": ["A", "B"], "label": "agent"},
            {"id": "jira-x", "tracker": "jira", "project": "TSS", "board": "ops",
             "state_map": {"done": "Closed"}, "token_env": "OTHER_TOKEN"},
        ],
    })
    lin, jira = conns
    assert lin.projects == ["A", "B"] and lin.board == "shared" and lin.settings["assignee"] == "worker"
    assert lin.settings["state_map"] == STATE_MAP
    assert jira.projects == ["TSS"] and jira.board == "ops"
    assert jira.settings["state_map"] == {"done": "Closed"} and jira.settings["token_env"] == "OTHER_TOKEN"


def test_bad_entries_are_reported_and_good_ones_kept():
    conns = parse_connections({"connections": [
        {"id": "ok", "tracker": "fake"},
        {"id": "Bad Id", "tracker": "fake"},
        {"id": "ok", "tracker": "fake"},
        {"id": "no-tracker"},
        "not a mapping",
    ]})
    assert [c.error is None for c in conns] == [True, False, False, False, False]
    assert "duplicate" in conns[2].error and "no tracker" in conns[3].error


def test_filter_projects_accepts_list_and_single():
    assert filter_projects({"project": "A", "projects": ["B", "A", " C "]}) == ["A", "B", "C"]
    assert filter_projects({}) == []


def test_jira_jql_with_several_projects():
    assert build_jql({"projects": ["TSS", "OPS"], "label": "agent"}) == \
        'project in ("TSS", "OPS") AND labels = "agent" ORDER BY created ASC'


# ── markers ──────────────────────────────────────────────────────────────


def test_marker_carries_connection_and_old_markers_still_parse():
    link = parse_marker(card_body("jira", "TSS-1", "https://x/TSS-1", "", conn="site-a"))
    assert link == Link("jira", "TSS-1", "https://x/TSS-1", "site-a")
    assert link.id == "site-a:jira:TSS-1"
    old = parse_marker(card_body("jira", "TSS-1", "https://x/TSS-1", ""))
    assert old == Link("jira", "TSS-1", "https://x/TSS-1", "") and old.id == "jira:TSS-1"
    assert idempotency_key("jira", "TSS-1", "site-a") == "issue-sync:site-a:jira:TSS-1"
    assert idempotency_key("jira", "TSS-1") == "issue-sync:jira:TSS-1"


# ── engines side by side ─────────────────────────────────────────────────


def _engine(adapter, kanban, store, conn, owns_legacy=False):
    return SyncEngine(adapter, kanban, SyncState(store, namespace=conn), {"state_map": STATE_MAP},
                      conn=conn, owns_legacy=owns_legacy)


def _cards_for(kanban, conn):
    return {link_from_card(c).key: cid for cid, c in kanban.cards.items()
            if link_from_card(c) and link_from_card(c).conn == conn}


def test_same_key_on_two_sites_gets_two_cards_and_stays_separate():
    kanban, store = FakeKanban(), MemoryStore()
    site_a, site_b = FakeAdapter(name="jira"), FakeAdapter(name="jira")
    site_a.add_issue("TSS-1", "on site A")
    site_b.add_issue("TSS-1", "on site B")
    a = _engine(site_a, kanban, store, "site-a", owns_legacy=True)
    b = _engine(site_b, kanban, store, "site-b")
    assert a.poll_inbound() == 1 and b.poll_inbound() == 1
    assert a.poll_inbound() == 0 and b.poll_inbound() == 0  # no duplicates on the next pass
    card_a, card_b = _cards_for(kanban, "site-a")["TSS-1"], _cards_for(kanban, "site-b")["TSS-1"]
    assert card_a != card_b

    kanban.set_status(card_b, "done")
    a.push_outbound()
    b.push_outbound()
    assert [c for c in site_a.calls if c[0] in ("move_issue", "post_comment")] == []
    assert ("move_issue", "TSS-1", "Done") in site_b.calls
    assert site_a.issues["TSS-1"].state == "Todo"


def test_state_is_kept_per_connection():
    store = MemoryStore()
    SyncState(store, namespace="a").update_link("jira:X-1", card_id="t_1")
    assert SyncState(store, namespace="b").link("jira:X-1") == {}
    assert SyncState(store).link("jira:X-1") == {}  # legacy, un-namespaced keys untouched


def test_legacy_cards_go_to_the_first_connection_of_their_tracker_only():
    kanban, store = FakeKanban(), MemoryStore()
    old = kanban.add_card(card_title("TSS-9", "old"), card_body("jira", "TSS-9", "", ""), status="done")
    first, second = FakeAdapter(name="jira"), FakeAdapter(name="jira")
    first.add_issue("TSS-9", "old")
    second.add_issue("TSS-9", "unrelated")
    a = _engine(first, kanban, store, "first", owns_legacy=True)
    b = _engine(second, kanban, store, "second")
    assert a.poll_inbound() == 0  # the old card already covers TSS-9 for the first connection
    a.push_outbound()
    b.push_outbound()
    assert ("move_issue", "TSS-9", "Done") in first.calls
    assert not any(c[0] == "move_issue" for c in second.calls)
    assert old in kanban.cards


# ── register(): several connections in one plugin ────────────────────────


def _mod(name):
    return importlib.import_module(f"{PACKAGE}.issue_sync.{name}")


def test_one_failing_connection_does_not_stop_the_others(plugin, stub_ctx, monkeypatch):
    adapters_mod = _mod("adapters")
    good, bad = adapters_mod.FakeAdapter(name="fake"), adapters_mod.FakeAdapter(name="fake")
    good.add_issue("G-1", "good one")
    bad.fail_next("fetch_issues", adapters_mod.AdapterError("fake: token rejected"))
    made = {"good": good, "bad": bad}
    real_get = plugin.get_adapter
    monkeypatch.setattr(plugin, "get_adapter",
                        lambda name, settings: made[settings["api_key_env"]] if name == "fake" else real_get(name, settings))
    kanban = _mod("kanban").FakeKanban()
    ctx = stub_ctx({"connections": [
        {"id": "bad", "tracker": "fake", "api_key_env": "bad"},
        {"id": "good", "tracker": "fake", "api_key_env": "good"},
        {"id": "broken", "tracker": "nope"},
    ]})
    plugin.register(ctx, kanban=kanban, background=False)
    ctx.hooks["on_kanban_dispatch_tick"](board="default")

    titles = [c["title"] for c in kanban.cards.values()]
    assert titles == ["[G-1] good one"]
    text = ctx.commands["issues"][0]("status")
    assert "connection bad:" in text and "token rejected" in text
    assert "connection good:" in text and "linked cards: 1" in text
    assert "connection broken:" in text and "unknown tracker 'nope'" in text


def test_status_lists_projects_per_connection(plugin, stub_ctx):
    ctx = stub_ctx({"connections": [
        {"id": "lin", "tracker": "fake", "projects": ["Sandbox", "Issue Sync"], "board": "b1"},
    ]})
    plugin.register(ctx, kanban=_mod("kanban").FakeKanban(), background=False)
    text = ctx.commands["issues"][0]("status")
    assert "connection lin:" in text and "projects: Sandbox, Issue Sync" in text and "board: b1" in text


def test_hooks_only_tick_connections_on_that_board(plugin, stub_ctx):
    kanban = _mod("kanban").FakeKanban()
    ctx = stub_ctx({"connections": [{"id": "a", "tracker": "fake", "board": "one"},
                                    {"id": "b", "tracker": "fake", "board": "two"}]})
    plugin.register(ctx, kanban=kanban, background=False)
    ctx.hooks["on_kanban_dispatch_tick"](board="two")
    text = ctx.commands["issues"][0]("status")
    a_block, b_block = text.split("connection b:")
    assert "last poll: never" in a_block and "last poll: never" not in b_block


def test_adapter_error_type_is_shared():
    assert issubclass(AdapterError, Exception)


def test_namespaced_state_keys_follow_hermes_key_rules():
    """Regression: a '/' prefix was rejected by Hermes' ctx.state in the live gateway."""
    import pytest
    store = MemoryStore()
    with pytest.raises(ValueError):
        store.set("conn/a/links", {})
    state = SyncState(store, namespace="linear-sandbox")
    state.update_link("linear-sandbox:linear:CORE-1", card_id="t_1")
    state.last_poll_at = 1.0
    assert set(store.data) == {"conn.linear-sandbox.links", "conn.linear-sandbox.last_poll_at"}
