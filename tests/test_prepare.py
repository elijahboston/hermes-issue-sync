"""prepare_command hook (CORE-632): shape each card before it is created."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from issue_sync.adapters.fake import FakeAdapter
from issue_sync.connections import parse_connections
from issue_sync.engine import SyncEngine
from issue_sync.kanban import FakeKanban, KanbanClient
from issue_sync.links import build_marker, card_body, card_title, link_from_card, parse_marker
from issue_sync.prepare import PrepareError, run_prepare
from issue_sync.state import MemoryStore, SyncState

NASTY = "$(rm -rf ~) `reboot`; echo pwned"


def _engine(adapter=None, kanban=None, *, prepare_runner=None, default_cap=False, **settings):
    """Tests of ordering/filters run uncapped; ``default_cap`` keeps the real default (1)."""
    if not default_cap:
        settings.setdefault("max_active", 0)
    adapter = adapter or FakeAdapter()
    kanban = kanban or FakeKanban()
    state = SyncState(MemoryStore())
    return SyncEngine(adapter, kanban, state, settings, prepare_runner=prepare_runner), adapter, kanban


def _keys(kanban: FakeKanban) -> list[str]:
    return [link_from_card(c).key for c in kanban.cards.values() if link_from_card(c)]


class FakeRun:
    """Stands in for ``subprocess.run``: records argv/kwargs, returns canned output."""

    def __init__(self, stdout="{}", *, returncode=0, stderr="", exc=None):
        self.stdout, self.returncode, self.stderr, self.exc = stdout, returncode, stderr, exc
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)

    @property
    def argv(self):
        return self.calls[-1][0]

    @property
    def kwargs(self):
        return self.calls[-1][1]

    @property
    def payload(self):
        return json.loads(self.kwargs["input"])


class QueueRun:
    """Several FakeRun responses, in order."""

    def __init__(self, *items: FakeRun):
        self.items = list(items)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return self.items.pop(0)(argv, **kwargs)


def _error_run(*, returncode=2, stderr="boom", stdout="", exc=None) -> FakeRun:
    return FakeRun(stdout, returncode=returncode, stderr=stderr, exc=exc)


# ── no hook ───────────────────────────────────────────────────────────────


def test_no_hook_creates_card_exactly_as_before():
    engine, adapter, kanban = _engine()
    issue = adapter.add_issue("A", "Fix", body="hello")
    assert engine.poll_inbound() == 1
    expected_title = card_title("A", "Fix")
    expected_body = card_body("fake", "A", issue.url, "hello")
    assert kanban.calls[-1] == (
        "create_card", expected_title, expected_body, "issue-sync:fake:A", None,
    )
    assert kanban.create_kwargs[-1] == {
        "workspace": None, "branch": None, "skills": (), "model": None, "provider": None,
    }
    card = next(iter(kanban.cards.values()))
    assert card["title"] == expected_title
    assert card["body"] == expected_body


# ── hook output reaches create_card ───────────────────────────────────────


def test_hook_output_fields_reach_create_card():
    runner = FakeRun(json.dumps({
        "workspace": "worktree:/tmp/wt", "branch": "feat/a", "skills": ["review", "test"],
        "model": "gpt", "provider": "openai", "title": "custom title", "assignee": "alice",
    }))
    engine, adapter, kanban = _engine(prepare_command=["prep"], prepare_runner=runner, assignee="worker")
    adapter.add_issue("A", "Fix")
    assert engine.poll_inbound() == 1
    card = next(iter(kanban.cards.values()))
    assert card["title"] == "custom title"
    assert card["assignee"] == "alice"
    assert card["workspace"] == "worktree:/tmp/wt"
    assert card["branch"] == "feat/a"
    assert card["skills"] == ("review", "test")
    assert card["model"] == "gpt"
    assert card["provider"] == "openai"
    assert kanban.calls[-1][1] == "custom title"
    assert kanban.calls[-1][2] == card_body("fake", "A", "https://fake.invalid/A", "")
    assert kanban.calls[-1][4] == "alice"
    assert kanban.create_kwargs[-1] == {
        "workspace": "worktree:/tmp/wt", "branch": "feat/a",
        "skills": ("review", "test"), "model": "gpt", "provider": "openai",
    }


def test_returned_body_keeps_exactly_one_leading_marker():
    existing = build_marker("fake", "A", "https://fake.invalid/A")
    runner = FakeRun(json.dumps({"body": f"{existing}\n\nshaped body"}))
    engine, adapter, kanban = _engine(prepare_command=["prep"], prepare_runner=runner)
    adapter.add_issue("A", "Fix", body="original")
    assert engine.poll_inbound() == 1
    card = next(iter(kanban.cards.values()))
    lines = card["body"].splitlines()
    assert parse_marker(lines[0]) is not None
    assert sum(1 for line in lines if parse_marker(line) is not None) == 1
    assert link_from_card(card).key == "A"
    assert "shaped body" in card["body"]
    assert "original" not in card["body"]


def test_returned_body_without_marker_gets_one():
    runner = FakeRun(json.dumps({"body": "just text"}))
    engine, adapter, kanban = _engine(prepare_command=["prep"], prepare_runner=runner)
    adapter.add_issue("A")
    engine.poll_inbound()
    card = next(iter(kanban.cards.values()))
    assert parse_marker(card["body"].splitlines()[0]) is not None
    assert link_from_card(card).key == "A"
    assert card["body"].splitlines()[0].startswith("<!-- issue-sync ")


# ── untrusted issue text is never interpolated ────────────────────────────


def test_issue_text_is_not_interpolated_into_argv():
    runner = FakeRun("{}")
    engine, adapter, kanban = _engine(
        prepare_command=["/usr/bin/prep", "--flag"], prepare_runner=runner)
    adapter.add_issue("A", NASTY, body=NASTY)
    assert engine.poll_inbound() == 1
    assert runner.argv == ["/usr/bin/prep", "--flag"]
    assert runner.kwargs["shell"] is False
    assert runner.payload["title"] == NASTY
    assert runner.payload["body"] == NASTY
    assert NASTY not in " ".join(runner.argv)


def test_string_command_is_split_and_tilde_expanded():
    runner = FakeRun("{}")
    payload = {"connection": "c", "key": "K-1", "title": NASTY}
    run_prepare("~/bin/prep --flag", payload, timeout=5, runner=runner)
    assert runner.argv == [os.path.expanduser("~/bin/prep"), "--flag"]
    assert runner.payload["title"] == NASTY
    assert runner.kwargs["env"]["ISSUE_SYNC_CONNECTION"] == "c"
    assert runner.kwargs["env"]["ISSUE_SYNC_KEY"] == "K-1"


# ── failures ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("runner, needle", [
    (_error_run(returncode=2, stderr="x" * 400 + " disk exploded"), "2"),
    (_error_run(exc=subprocess.TimeoutExpired(["prep"], 120, stderr="hung")), "timed out"),
    (FakeRun("not json at all"), "JSON"),
    (FakeRun(json.dumps({"body": 1}), stderr="type-err"), "body"),
])
def test_prepare_failure_creates_no_card_and_admits_the_next(runner, needle):
    queue = QueueRun(runner, FakeRun("{}"))
    engine, adapter, kanban = _engine(prepare_command=["prep"], prepare_runner=queue)
    adapter.add_issue("A", "first")
    adapter.add_issue("B", "second")
    assert engine.poll_inbound() == 1
    assert _keys(kanban) == ["B"]
    assert engine.last_skips["A"] == "prepare-failed"
    err = engine.state.link("fake:A")["last_error"]
    assert needle in err
    if needle == "2":
        assert "disk exploded" in err
        assert "exit" in err or "status" in err
    assert engine.state.link("fake:B")["last_error"] is None


def test_skip_creates_no_card_and_no_error():
    runner = FakeRun(json.dumps({"skip": True, "reason": "disk low"}))
    engine, adapter, kanban = _engine(prepare_command=["prep"], prepare_runner=runner)
    adapter.add_issue("A")
    adapter.add_issue("B")
    # skip A, then B also skips (same runner)
    assert engine.poll_inbound() == 0
    assert kanban.cards == {}
    assert engine.last_skips["A"] == "prepare-skip"
    assert engine.last_skips["B"] == "prepare-skip"
    assert engine.state.link("fake:A") == {}


def test_skip_does_not_consume_max_active_slot():
    queue = QueueRun(FakeRun(json.dumps({"skip": True, "reason": "later"})), FakeRun("{}"))
    engine, adapter, kanban = _engine(max_active=1, prepare_command=["prep"], prepare_runner=queue)
    adapter.add_issue("A")
    adapter.add_issue("B")
    assert engine.poll_inbound() == 1
    assert _keys(kanban) == ["B"]
    assert engine.last_skips["A"] == "prepare-skip"


def test_max_active_one_failed_prepare_gives_slot_to_next():
    queue = QueueRun(_error_run(returncode=7, stderr="nope"), FakeRun("{}"))
    engine, adapter, kanban = _engine(max_active=1, prepare_command=["prep"], prepare_runner=queue)
    adapter.add_issue("A")
    adapter.add_issue("B")
    assert engine.poll_inbound() == 1
    assert _keys(kanban) == ["B"]
    assert engine.last_skips["A"] == "prepare-failed"
    assert "7" in engine.state.link("fake:A")["last_error"]
    assert "nope" in engine.state.link("fake:A")["last_error"]


def test_provider_without_model_is_a_failure():
    runner = FakeRun(json.dumps({"provider": "openai"}))
    with pytest.raises(PrepareError, match="provider"):
        run_prepare(["prep"], {"key": "A"}, timeout=5, runner=runner)
    engine, adapter, kanban = _engine(prepare_command=["prep"], prepare_runner=FakeRun(
        json.dumps({"provider": "openai"})))
    adapter.add_issue("A")
    assert engine.poll_inbound() == 0
    assert kanban.cards == {}
    assert engine.last_skips["A"] == "prepare-failed"
    assert "provider" in engine.state.link("fake:A")["last_error"]


def test_missing_executable_is_a_failure():
    runner = FakeRun(exc=FileNotFoundError("prep"))
    with pytest.raises(PrepareError, match="not found"):
        run_prepare(["/no/such"], {"key": "A"}, timeout=5, runner=runner)


def test_unknown_keys_are_ignored_with_a_warning(caplog):
    runner = FakeRun(json.dumps({"title": "ok", "bonus": 1, "secret": "x"}))
    result = run_prepare(["prep"], {"key": "A"}, timeout=5, runner=runner)
    assert result.title == "ok"
    assert "bonus" in caplog.text and "secret" in caplog.text


# ── real subprocess ───────────────────────────────────────────────────────


def test_real_subprocess_stdin_json_to_stdout_json(tmp_path):
    script = tmp_path / "prep.py"
    script.write_text(
        "import json, sys\n"
        "issue = json.load(sys.stdin)\n"
        "json.dump({'title': issue['title'] + '-ok', 'workspace': 'scratch'}, sys.stdout)\n"
    )
    payload = {
        "connection": "lin", "tracker": "linear", "key": "A-1", "title": "Fix",
        "url": "https://x/A-1", "body": "hi", "labels": ["agent"], "priority": 2,
        "estimate": None, "created_at": "2026-01-01", "state": "Todo",
    }
    result = run_prepare([sys.executable, str(script)], payload, timeout=10)
    assert result.title == "Fix-ok"
    assert result.workspace == "scratch"
    assert result.skip is False


# ── KanbanClient argv ─────────────────────────────────────────────────────


class Runner:
    def __init__(self, stdout="", *, returncode=0, stderr="", exc=None):
        self.stdout, self.returncode, self.stderr, self.exc = stdout, returncode, stderr, exc
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)

    @property
    def argv(self):
        return self.calls[-1][0]


def test_kanban_create_argv_for_prepare_flags():
    runner = Runner('{"id": "t_1"}')
    KanbanClient(runner=runner).create_card(
        "T", "B", idempotency_key="k",
        workspace="scratch", branch="feat/x", skills=["a", "b"],
        model="gpt", provider="openai",
    )
    assert runner.argv == [
        "hermes", "kanban", "create", "T", "--body-file", "-", "--idempotency-key", "k",
        "--workspace", "scratch", "--branch", "feat/x",
        "--skill", "a", "--skill", "b", "--model", "gpt", "--provider", "openai", "--json",
    ]


def test_kanban_create_omits_provider_without_model():
    runner = Runner('{"id": "t_1"}')
    KanbanClient(runner=runner).create_card("T", "B", idempotency_key="k", provider="openai")
    assert "--provider" not in runner.argv
    assert "--model" not in runner.argv


def test_kanban_create_omits_unset_prepare_flags():
    runner = Runner('{"id": "t_1"}')
    KanbanClient(runner=runner).create_card("T", "B", idempotency_key="k", assignee="worker")
    assert runner.argv == [
        "hermes", "kanban", "create", "T", "--body-file", "-",
        "--idempotency-key", "k", "--assignee", "worker", "--json",
    ]


# ── settings ──────────────────────────────────────────────────────────────


def test_prepare_settings_inherit_and_override():
    conns = parse_connections({
        "prepare_command": "/bin/prep",
        "prepare_timeout_seconds": 30,
        "connections": [
            {"id": "a", "tracker": "linear", "projects": ["P"]},
            {"id": "b", "tracker": "linear", "projects": ["Q"],
             "prepare_command": ["/opt/prep"], "prepare_timeout_seconds": 9},
        ],
    })
    assert conns[0].settings["prepare_command"] == "/bin/prep"
    assert conns[0].settings["prepare_timeout_seconds"] == 30
    assert conns[1].settings["prepare_command"] == ["/opt/prep"]
    assert conns[1].settings["prepare_timeout_seconds"] == 9


def test_prepare_timeout_default_and_invalid():
    engine, _, _ = _engine()
    assert engine.prepare_command is None
    assert engine.prepare_timeout == 120
    engine, _, _ = _engine(prepare_timeout_seconds="nope")
    assert engine.prepare_timeout == 120
