"""Model rules, roles, skills and reviewer (CORE-632)."""

from __future__ import annotations

import logging

from issue_sync.adapters.fake import FakeAdapter
from issue_sync.engine import SyncEngine
from issue_sync.kanban import FakeKanban
from issue_sync.state import MemoryStore, SyncState


def _engine(**settings):
    adapter = FakeAdapter()
    kanban = FakeKanban()
    engine = SyncEngine(adapter, kanban, SyncState(MemoryStore()), settings)
    return engine, adapter, kanban


def _create(kanban):
    return [c for c in kanban.calls if c[0] == "create_card"][-1]


def test_first_matching_rule_wins():
    rules = [
        {"when": {"label": "hard"}, "model": "strong", "provider": "anthropic"},
        {"when": {"estimate_max": 2}, "model": "small", "provider": "openai"},
        {"when": {}, "model": "default", "provider": "openai"},
    ]
    engine, adapter, kanban = _engine(model_rules=rules)
    adapter.add_issue("A", labels=("hard",), estimate=1)
    adapter.add_issue("B", estimate=1)
    adapter.add_issue("C", estimate=5)
    engine.poll_inbound()
    by_key = {}
    for call in kanban.calls:
        if call[0] == "create_card":
            # title is "[KEY] ..."
            key = call[1].split("]", 1)[0][1:]
            by_key[key] = (call[8], call[9])
    assert by_key["A"] == ("strong", "anthropic")
    assert by_key["B"] == ("small", "openai")
    assert by_key["C"] == ("default", "openai")


def test_role_resolves_through_roles_file(tmp_path):
    roles = tmp_path / "roles.yaml"
    roles.write_text(
        "roles:\n  strong: big\nmodels:\n  big:\n    anthropic: claude-sonnet\n    openai: gpt-4.1\n"
    )
    engine, adapter, kanban = _engine(
        roles_file=str(roles),
        model_rules=[{"when": {"label": "hard"}, "role": "strong", "provider": "anthropic"}],
    )
    adapter.add_issue("A", labels=("HARD",))
    engine.poll_inbound()
    call = _create(kanban)
    assert call[8] == "claude-sonnet" and call[9] == "anthropic"


def test_unknown_role_passes_no_model_and_warns(caplog):
    caplog.set_level(logging.WARNING)
    engine, adapter, kanban = _engine(
        roles_file="/nope.yaml",
        model_rules=[{"when": {}, "role": "missing", "provider": "openai"}],
    )
    adapter.add_issue("A")
    engine.poll_inbound()
    call = _create(kanban)
    assert call[8] is None and call[9] is None
    assert any("roles_file" in r.message or "unknown role" in r.message for r in caplog.records)


def test_unreadable_roles_file_does_not_crash(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    engine, adapter, kanban = _engine(
        roles_file=str(tmp_path / "missing.yaml"),
        model_rules=[{"when": {}, "role": "strong", "provider": "openai"}],
    )
    adapter.add_issue("A")
    assert engine.poll_inbound() == 1
    call = _create(kanban)
    assert call[8] is None and call[9] is None


def test_skills_are_passed_and_reviewer_is_exposed_not_to_kanban():
    engine, adapter, kanban = _engine(skills=["repo", "kanban"], reviewer="reviewer")
    assert engine.reviewer == "reviewer"
    adapter.add_issue("A")
    engine.poll_inbound()
    call = _create(kanban)
    assert call[7] == ("repo", "kanban")
    assert call[8] is None and call[9] is None


def test_defaults_pass_no_model_workspace_or_skills():
    engine, adapter, kanban = _engine()
    assert engine.reviewer is None and engine.skills == () and engine.card_template is None
    adapter.add_issue("A")
    engine.poll_inbound()
    call = _create(kanban)
    assert call[5] is None and call[6] is None and call[7] == () and call[8] is None and call[9] is None
