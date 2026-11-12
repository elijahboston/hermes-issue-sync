"""Admission control (CORE-631): which filtered issues become cards."""

from __future__ import annotations

from issue_sync.adapters.fake import FakeAdapter
from issue_sync.connections import parse_connections
from issue_sync.engine import SyncEngine
from issue_sync.kanban import FakeKanban
from issue_sync.links import card_body, link_from_card
from issue_sync.state import MemoryStore, SyncState


def _engine(adapter=None, kanban=None, **settings):
    adapter = adapter or FakeAdapter()
    kanban = kanban or FakeKanban()
    state = SyncState(MemoryStore())
    return SyncEngine(adapter, kanban, state, settings), adapter, kanban


def _keys(kanban: FakeKanban) -> list[str]:
    return [link_from_card(c).key for c in kanban.cards.values() if link_from_card(c)]


def _create_order(kanban: FakeKanban) -> list[str]:
    return [link_from_card({"body": call[2]}).key for call in kanban.calls if call[0] == "create_card"]


# ── defaults ──────────────────────────────────────────────────────────────


def test_defaults_admit_every_open_unblocked_issue():
    engine, adapter, kanban = _engine()
    adapter.add_issue("A")
    adapter.add_issue("B")
    adapter.add_issue("C", closed=True)
    assert engine.poll_inbound() == 2
    assert sorted(_keys(kanban)) == ["A", "B"]
    assert engine.last_skips == {"C": "closed"}
    assert engine.admit == "label"
    assert engine.max_active is None
    assert engine.order == "priority"
    assert engine.leaf_only is False
    assert engine.skip_blocked is True
    assert engine.exclude_label == ""


# ── admit ─────────────────────────────────────────────────────────────────


def test_admit_all_without_projects_is_a_connection_error():
    (conn,) = parse_connections({"tracker": "linear", "admit": "all"})
    assert conn.error and "project" in conn.error.lower()
    (ok,) = parse_connections({"tracker": "linear", "admit": "all", "projects": ["Sandbox"]})
    assert ok.error is None and ok.settings["admit"] == "all"


def test_admit_inherits_from_top_level():
    (conn,) = parse_connections({
        "admit": "all", "max_active": 3, "order": "created", "leaf_only": True,
        "skip_blocked": False, "exclude_label": "hold",
        "connections": [{"id": "lin", "tracker": "linear", "projects": ["A"]}],
    })
    assert conn.settings["admit"] == "all"
    assert conn.settings["max_active"] == 3
    assert conn.settings["order"] == "created"
    assert conn.settings["leaf_only"] is True
    assert conn.settings["skip_blocked"] is False
    assert conn.settings["exclude_label"] == "hold"


# ── max_active ────────────────────────────────────────────────────────────


def test_max_active_caps_new_cards():
    engine, adapter, kanban = _engine(max_active=2)
    for key in ("A", "B", "C"):
        adapter.add_issue(key)
    assert engine.poll_inbound() == 2
    assert engine.last_skips["C"] == "cap"
    assert len(kanban.cards) == 2


def test_max_active_ignores_blocked_triage_done_archived():
    engine, adapter, kanban = _engine(max_active=2)
    adapter.add_issue("OLD")
    engine.poll_inbound()
    (old_id,) = list(kanban.cards)
    kanban.set_status(old_id, "blocked")
    kanban.add_card("triage", card_body("fake", "TRIAGE", "", ""), status="triage")
    kanban.add_card("done", card_body("fake", "DONE", "", ""), status="done")
    kanban.add_card("arch", card_body("fake", "ARCH", "", ""), status="archived")
    adapter.add_issue("NEW-1")
    adapter.add_issue("NEW-2")
    # blocked/triage/done/archived do not count, so remaining = 2.
    assert engine.poll_inbound() == 2
    assert "NEW-1" in _keys(kanban) and "NEW-2" in _keys(kanban)


def test_max_active_counts_ready_and_scheduled():
    engine, adapter, kanban = _engine(max_active=2)
    adapter.add_issue("OLD")
    engine.poll_inbound()
    kanban.set_status(next(iter(kanban.cards)), "scheduled")
    adapter.add_issue("NEW-1")
    adapter.add_issue("NEW-2")
    assert engine.poll_inbound() == 1
    assert engine.last_skips["NEW-2"] == "cap"


def test_max_active_does_not_count_other_connections_cards():
    engine, adapter, kanban = _engine(max_active=1)
    kanban.add_card("other", card_body("linear", "LIN-1", "", ""), status="ready")
    adapter.add_issue("A")
    adapter.add_issue("B")
    assert engine.poll_inbound() == 1
    assert engine.last_skips["B"] == "cap"


# ── order ─────────────────────────────────────────────────────────────────


def test_priority_order_puts_no_priority_last():
    engine, adapter, kanban = _engine(order="priority")
    adapter.add_issue("LOW", priority=4, created_at="2026-01-04")
    adapter.add_issue("NONE", priority=0, created_at="2026-01-01")
    adapter.add_issue("URGENT", priority=1, created_at="2026-01-03")
    adapter.add_issue("HIGH", priority=2, created_at="2026-01-02")
    engine.poll_inbound()
    assert _create_order(kanban) == ["URGENT", "HIGH", "LOW", "NONE"]


def test_priority_ties_break_by_smaller_estimate_then_oldest():
    engine, adapter, kanban = _engine(order="priority")
    adapter.add_issue("BIG", priority=2, estimate=5, created_at="2026-01-01")
    adapter.add_issue("OLD", priority=2, estimate=1, created_at="2026-01-01")
    adapter.add_issue("NEW", priority=2, estimate=1, created_at="2026-01-02")
    engine.poll_inbound()
    assert _create_order(kanban) == ["OLD", "NEW", "BIG"]


def test_created_order_is_oldest_first():
    engine, adapter, kanban = _engine(order="created")
    adapter.add_issue("NEW", priority=1, created_at="2026-02-01")
    adapter.add_issue("OLD", priority=4, created_at="2026-01-01")
    engine.poll_inbound()
    assert _create_order(kanban) == ["OLD", "NEW"]


# ── leaf_only ─────────────────────────────────────────────────────────────


def test_leaf_only_skips_issues_with_open_children():
    engine, adapter, kanban = _engine(leaf_only=True)
    adapter.add_issue("PARENT", open_children=("CHILD-1",))
    adapter.add_issue("LEAF")
    adapter.add_issue("DONE-PARENT", open_children=())
    assert engine.poll_inbound() == 2
    assert sorted(_keys(kanban)) == ["DONE-PARENT", "LEAF"]
    assert engine.last_skips["PARENT"] == "parent"


# ── skip_blocked ──────────────────────────────────────────────────────────


def test_skip_blocked_defaults_on():
    engine, adapter, kanban = _engine()
    adapter.add_issue("BLOCKED", open_blockers=("OTHER",))
    adapter.add_issue("FREE")
    assert engine.poll_inbound() == 1
    assert _keys(kanban) == ["FREE"]
    assert engine.last_skips["BLOCKED"] == "blocked"


def test_skip_blocked_false_admits_blocked_issues():
    engine, adapter, kanban = _engine(skip_blocked=False)
    adapter.add_issue("BLOCKED", open_blockers=("OTHER",))
    assert engine.poll_inbound() == 1
    assert _keys(kanban) == ["BLOCKED"]


# ── exclude_label ─────────────────────────────────────────────────────────


def test_exclude_label_skips_matching_issue():
    engine, adapter, kanban = _engine(exclude_label="Hold")
    adapter.add_issue("SKIP", labels=("hold",))
    adapter.add_issue("KEEP", labels=("agent",))
    assert engine.poll_inbound() == 1
    assert _keys(kanban) == ["KEEP"]
    assert engine.last_skips["SKIP"] == "excluded"


def test_exclude_label_is_inherited_from_ancestors():
    engine, adapter, kanban = _engine(exclude_label="hold")
    adapter.add_issue("CHILD", ancestor_labels=("theme", "HOLD"))
    adapter.add_issue("OK")
    assert engine.poll_inbound() == 1
    assert _keys(kanban) == ["OK"]
    assert engine.last_skips["CHILD"] == "excluded"


# ── skip reasons ──────────────────────────────────────────────────────────


def test_already_linked_is_recorded():
    engine, adapter, kanban = _engine()
    adapter.add_issue("A")
    engine.poll_inbound()
    adapter.add_issue("B")
    engine.poll_inbound()
    assert engine.last_skips["A"] == "linked"
    assert "B" not in engine.last_skips
