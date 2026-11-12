"""hermes issues dry-run (CORE-634): admission preview, landing state, no writes."""

from __future__ import annotations

import json
import subprocess
from argparse import ArgumentParser
from types import SimpleNamespace

from issue_sync.adapters.fake import FakeAdapter
from issue_sync.connections import Connection, parse_connections
from issue_sync.dry_run import collect, run, run_runners
from issue_sync.engine import WOULD_ADMIT, SyncEngine
from issue_sync.kanban import FakeKanban
from issue_sync.links import card_body, card_title, link_from_card
from issue_sync.state import MemoryStore, SyncState


def _engine(*, adapter=None, kanban=None, state=None, clock=None, prepare_runner=None, **settings):
    settings.setdefault("max_active", 0)
    adapter = adapter or FakeAdapter()
    kanban = kanban or FakeKanban()
    state = state or SyncState(MemoryStore())
    engine = SyncEngine(
        adapter, kanban, state, settings, clock=clock or (lambda: 1_000_000.0),
        prepare_runner=prepare_runner,
    )
    return engine, adapter, kanban, state


def _conn(engine, **raw):
    raw.setdefault("id", "lin")
    raw.setdefault("tracker", engine.adapter.name)
    (conn,) = parse_connections({"connections": [raw]})
    return conn


def _linked_card(kanban, key, *, status="done", title="", conn=""):
    url = f"https://fake.invalid/{key}"
    return kanban.add_card(
        card_title(key, title or key),
        card_body("fake", key, url, "", conn=conn),
        status=status,
    )


class FakeRun:
    def __init__(self, stdout="{}", *, returncode=0, stderr="", exc=None):
        self.stdout, self.returncode, self.stderr, self.exc = stdout, returncode, stderr, exc
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


def _mixed_fixture(**settings):
    engine, adapter, kanban, state = _engine(
        max_active=1, leaf_only=True, exclude_label="hold", **settings,
    )
    adapter.add_issue("URGENT", "First", priority=1)
    adapter.add_issue("LATER", "Second", priority=4)
    adapter.add_issue("CLOSED", closed=True)
    adapter.add_issue("HOLD", labels=["hold"])
    adapter.add_issue("PARENT", open_children=["CHILD"])
    adapter.add_issue("BLOCKED", open_blockers=["X"])
    adapter.add_issue("LINKED", "Already")
    _linked_card(kanban, "LINKED", status="done")
    return engine, adapter, kanban, state


# ── admission ─────────────────────────────────────────────────────────────


def test_plan_inbound_matches_poll_inbound_decisions():
    engine, adapter, kanban, _ = _mixed_fixture()
    cards = kanban.list_cards(archived=True)
    plan = engine.plan_inbound(cards)
    decisions = {issue.key: decision for issue, decision in plan}

    assert decisions["CLOSED"] == "closed"
    assert decisions["LINKED"] == "linked"
    assert decisions["HOLD"] == "excluded"
    assert decisions["PARENT"] == "parent"
    assert decisions["BLOCKED"] == "blocked"
    assert decisions["URGENT"] == WOULD_ADMIT
    assert decisions["LATER"] == "cap"

    created = engine.poll_inbound()
    assert created == 1
    assert engine.last_skips == {k: d for k, d in decisions.items() if d != WOULD_ADMIT}
    created_keys = [
        link_from_card({"body": call[2]}).key
        for call in kanban.calls if call[0] == "create_card"
    ]
    assert created_keys == [issue.key for issue, decision in plan if decision == WOULD_ADMIT]


def test_dry_run_creates_moves_comments_nothing():
    engine, adapter, kanban, _ = _mixed_fixture()
    conn = _conn(engine, projects=["P"])
    adapter.calls.clear()
    kanban.calls.clear()

    report = collect(engine, conn)
    text, code = run([(engine, conn, None)])

    assert code == 0
    assert report["issues"]
    assert {row["key"]: row["decision"] for row in report["issues"]}["URGENT"] == WOULD_ADMIT
    assert "would-admit" in text
    assert [c[0] for c in kanban.calls] == ["list_cards", "list_cards"]
    assert all(c[0] in ("check", "fetch_issues") for c in adapter.calls)
    assert not any(c[0] in ("create_card", "comment", "unblock", "show_card") for c in kanban.calls)
    assert not any(c[0] in ("move_issue", "post_comment", "list_comments_since") for c in adapter.calls)


def test_prepare_not_run_unless_asked():
    runner = FakeRun(json.dumps({"workspace": "scratch", "branch": "issue/URGENT"}))
    engine, adapter, kanban, _ = _engine(prepare_command=["/bin/prep"], prepare_runner=runner)
    adapter.add_issue("URGENT", "First")
    conn = _conn(engine)

    collect(engine, conn)
    assert runner.calls == []

    report = collect(engine, conn, with_prepare=True)
    assert runner.calls
    assert report["prepare"][0]["result"]["workspace"] == "scratch"
    assert report["prepare"][0]["error"] is None
    assert not any(c[0] == "create_card" for c in kanban.calls)


def test_prepare_error_is_reported_not_raised():
    runner = FakeRun(returncode=7, stderr="disk full")
    engine, adapter, _, _ = _engine(prepare_command=["/bin/prep"], prepare_runner=runner)
    adapter.add_issue("A")
    report = collect(engine, _conn(engine), with_prepare=True)
    assert report["prepare"][0]["result"] is None
    assert "7" in report["prepare"][0]["error"]


def test_connection_error_exits_1():
    conn = Connection("broken", "linear", {}, "admit: all requires at least one project")
    text, code = run([(None, conn, conn.error)])
    assert code == 1
    assert "admit: all" in text

    payload, json_code = run([(None, conn, conn.error)], as_json=True)
    data = json.loads(payload)
    assert json_code == 1 and data["ok"] is False
    assert data["connections"][0]["error"]


def test_check_failure_exits_1():
    engine, adapter, _, _ = _engine()
    adapter.check_error = "LINEAR_API_KEY is not set"
    text, code = run([(engine, _conn(engine), None)])
    assert code == 1
    assert "LINEAR_API_KEY" in text


def test_unknown_connection_exits_1():
    engine, _, _, _ = _engine()
    text, code = run([(engine, _conn(engine), None)], connection="missing")
    assert code == 1
    assert "missing" in text


# ── landing ───────────────────────────────────────────────────────────────


def test_landing_states_listed():
    engine, adapter, kanban, state = _engine(done_gate="command", land_command=["/bin/land"])
    adapter.add_issue("RUN")
    adapter.add_issue("WAIT")
    adapter.add_issue("FAIL")
    adapter.add_issue("LAND")
    adapter.add_issue("FRESH")
    ids = {
        "RUN": _linked_card(kanban, "RUN"),
        "WAIT": _linked_card(kanban, "WAIT"),
        "FAIL": _linked_card(kanban, "FAIL"),
        "LAND": _linked_card(kanban, "LAND"),
        "FRESH": _linked_card(kanban, "FRESH"),
    }
    state.update_link("fake:RUN", card_id=ids["RUN"], landing={"status": "running"})
    state.update_link("fake:WAIT", card_id=ids["WAIT"],
                      landing={"status": "waiting", "next_try_at": 1_000_300.0})
    state.update_link("fake:FAIL", card_id=ids["FAIL"],
                      landing={"status": "failed", "error": "fatal: no remote"})
    state.update_link("fake:LAND", card_id=ids["LAND"], landing={"status": "landed"})

    report = collect(engine, _conn(engine), clock=lambda: 1_000_000.0)
    by_key = {row["key"]: row for row in report["landing"]}
    assert by_key["RUN"]["status"] == "running"
    assert by_key["RUN"]["next"] == "wait for the land command to finish"
    assert by_key["WAIT"]["status"] == "waiting"
    assert by_key["WAIT"]["next_try_at"] == 1_000_300.0
    assert "retry" in by_key["WAIT"]["next"]
    assert by_key["FAIL"]["status"] == "failed"
    assert "no remote" in (by_key["FAIL"]["error"] or "")
    assert "human reply" in by_key["FAIL"]["next"]
    assert by_key["LAND"]["status"] == "landed"
    assert by_key["LAND"]["next"] == "push Done and post the summary"
    assert by_key["FRESH"]["status"] == "not started"
    assert by_key["FRESH"]["next"] == "run land_command"
    assert not any(c[0] in ("create_card", "comment") for c in kanban.calls)
    assert not any(c[0] in ("move_issue", "post_comment") for c in adapter.calls)


def test_json_shape():
    engine, adapter, kanban, state = _engine(max_active=1)
    adapter.add_issue("A", "Admit me", priority=1)
    adapter.add_issue("B", "Capped", priority=4)
    card_id = _linked_card(kanban, "DONE")
    state.update_link("fake:DONE", card_id=card_id, landing={"status": "landed"})
    conn = _conn(engine, projects=["Sandbox"], board="ops", label="agent")
    text, code = run([(engine, conn, None)], as_json=True)
    assert code == 0
    data = json.loads(text)
    assert data["ok"] is True
    assert isinstance(data["connections"], list) and len(data["connections"]) == 1
    block = data["connections"][0]
    for key in ("id", "tracker", "projects", "label", "admit", "board", "max_active",
                "active", "error", "check_error", "issues", "landing"):
        assert key in block, key
    assert block["id"] == "lin"
    assert block["projects"] == ["Sandbox"]
    assert block["board"] == "ops"
    assert block["max_active"] == 1
    assert block["check_error"] is None
    decisions = {row["key"]: row["decision"] for row in block["issues"]}
    assert decisions["A"] == WOULD_ADMIT and decisions["B"] == "cap"
    assert set(block["issues"][0]) >= {"key", "title", "url", "decision"}
    landing = {row["key"]: row for row in block["landing"]}
    assert landing["DONE"]["status"] == "landed"
    assert set(landing["DONE"]) >= {"key", "card_id", "status", "next_try_at", "error", "next"}


# ── CLI / slash ───────────────────────────────────────────────────────────


def test_slash_dry_run_is_read_only(plugin, stub_ctx):
    kanban = FakeKanban()
    ctx = stub_ctx({"tracker": "fake"})
    plugin.register(ctx, kanban=kanban, background=False)
    handler, kwargs = ctx.commands["issues"]
    assert "dry-run" in kwargs["args_hint"]
    text = handler("dry-run")
    assert "tracker: fake" in text
    assert "check: ok" in text
    assert not any(c[0] in ("create_card", "comment", "unblock") for c in kanban.calls)
    assert handler("bogus").startswith("usage:")


def test_cli_dry_run_json_and_unknown_connection(plugin, stub_ctx, capsys):
    kanban = FakeKanban()
    ctx = stub_ctx({"tracker": "fake"})
    plugin.register(ctx, kanban=kanban, background=False)
    spec = ctx.cli_commands["issues"]
    parser = ArgumentParser()
    spec["setup_fn"](parser)
    args = parser.parse_args(["dry-run", "--json"])
    assert spec["handler_fn"](args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["connections"][0]["tracker"] == "fake"

    args = parser.parse_args(["dry-run", "--connection", "nope"])
    assert spec["handler_fn"](args) == 1
    assert "nope" in capsys.readouterr().out


def test_cli_handler_namespace_with_prepare_flag(plugin, stub_ctx, capsys):
    kanban = FakeKanban()
    ctx = stub_ctx({"tracker": "fake", "prepare_command": "/bin/prep"})
    plugin.register(ctx, kanban=kanban, background=False)
    handler = ctx.cli_commands["issues"]["handler_fn"]
    args = SimpleNamespace(
        issues_command="dry-run", connection=None, with_prepare=False, as_json=False,
    )
    assert handler(args) == 0
    out = capsys.readouterr().out
    assert "prepare:" not in out


def test_run_runners_filters_connection():
    engine_a, _, _, _ = _engine()
    engine_b, adapter_b, _, _ = _engine()
    adapter_b.add_issue("B-1")
    conn_a = Connection("aaa", "fake", {"filter": {"projects": ["A"]}})
    conn_b = Connection("bbb", "fake", {"filter": {"projects": ["B"]}})

    class R:
        def __init__(self, engine, conn):
            self.engine, self.conn, self.error = engine, conn, None

    text, code = run_runners([R(engine_a, conn_a), R(engine_b, conn_b)], connection="bbb")
    assert code == 0
    assert "connection bbb:" in text
    assert "connection aaa:" not in text
    assert "B-1" in text
