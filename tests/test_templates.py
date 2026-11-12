"""Card templates (CORE-632): marker first, unknown placeholders kept, missing file isolated."""

from __future__ import annotations

from issue_sync.adapters.fake import FakeAdapter
from issue_sync.engine import SyncEngine
from issue_sync.kanban import FakeKanban
from issue_sync.links import card_body, link_from_card, parse_marker
from issue_sync.state import MemoryStore, SyncState


def _engine(**settings):
    adapter = FakeAdapter()
    kanban = FakeKanban()
    engine = SyncEngine(adapter, kanban, SyncState(MemoryStore()), settings)
    return engine, adapter, kanban


def test_template_renders_after_marker_and_keeps_unknown_placeholders(tmp_path):
    path = tmp_path / "card.md"
    path.write_text("Issue {key}: {title}\n\n{body}\n\nsee {url} on {unknown}\nbase={base}\n")
    engine, adapter, kanban = _engine(card_template=str(path))
    adapter.add_issue("ISS-1", "Fix login", body="steps", url="https://t.invalid/ISS-1")
    assert engine.poll_inbound() == 1
    body = next(iter(kanban.cards.values()))["body"]
    assert parse_marker(body) is not None
    assert body.splitlines()[0].startswith("<!-- issue-sync ")
    assert "Issue ISS-1: Fix login" in body
    assert "steps" in body
    assert "see https://t.invalid/ISS-1 on {unknown}" in body
    assert "base=\n" in body or body.endswith("base=")
    assert "Tracker issue:" not in body  # template replaces the built-in body


def test_missing_template_is_a_per_issue_error(tmp_path):
    engine, adapter, kanban = _engine(card_template=str(tmp_path / "nope.md"))
    adapter.add_issue("ISS-1", "Fix")
    assert engine.poll_inbound() == 0
    assert kanban.cards == {}
    err = engine.state.link("fake:ISS-1")["last_error"]
    assert "card_template" in err


def test_unreadable_template_does_not_stop_other_issues(tmp_path):
    missing = tmp_path / "nope.md"
    engine, adapter, kanban = _engine(card_template=str(missing))
    adapter.add_issue("ISS-1")
    adapter.add_issue("ISS-2")
    assert engine.poll_inbound() == 0
    assert engine.state.link("fake:ISS-1")["last_error"]
    assert engine.state.link("fake:ISS-2")["last_error"]


def test_absent_template_keeps_builtin_card_body():
    engine, adapter, kanban = _engine()
    issue = adapter.add_issue("ISS-1", "Fix", body="steps", url="https://t.invalid/ISS-1")
    engine.poll_inbound()
    body = next(iter(kanban.cards.values()))["body"]
    assert body == card_body("fake", issue.key, issue.url, issue.body)
    assert link_from_card(next(iter(kanban.cards.values())))
