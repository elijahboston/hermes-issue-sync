"""Reviewer gates merged landing (CORE-633)."""

from __future__ import annotations

from issue_sync.links import idempotency_key, link_from_card
from issue_sync.signature import sign

from test_blocking import questions
from test_engine import linked
from test_landing import FakeGit, _env, done_moves, primed


def _review_cards(env):
    return [c for c in env.kanban.cards.values() if " review: " in (c.get("title") or "")]


def test_no_reviewer_merged_is_unchanged(tmp_path):
    git = FakeGit(ancestry=True)
    env = _env(tmp_path, done_gate="merged", merged_base="main", git_runner=git, which=lambda n: None)
    primed(env, workspace=str(tmp_path), branch="feat/x")
    env.engine.push_outbound()
    assert _review_cards(env) == []
    assert done_moves(env) == [("move_issue", "FAKE-1", "Done")]
    assert env.engine.reviewer is None


def test_reviewer_creates_one_idempotent_review_card(tmp_path):
    git = FakeGit(ancestry=True)
    env = _env(
        tmp_path, done_gate="merged", merged_base="main", reviewer="alice",
        git_runner=git, which=lambda n: None,
    )
    card = primed(env, workspace=str(tmp_path), branch="feat/x")
    env.engine.push_outbound()
    env.engine.push_outbound()
    reviews = _review_cards(env)
    assert len(reviews) == 1
    review = reviews[0]
    assert review["title"] == "[FAKE-1] review: Issue FAKE-1"
    assert review["assignee"] == "alice"
    assert review["id"] == env.state.link("fake:FAKE-1")["review_card_id"]
    assert env.kanban.idempotency_keys[review["id"]] == f"{idempotency_key('fake', 'FAKE-1')}:review"
    kwargs = env.kanban.create_kwargs[-1]
    assert kwargs["workspace"] == f"worktree:{tmp_path}"
    assert kwargs["branch"] == "feat/x"
    link = link_from_card(review)
    assert link is not None and link.key == "FAKE-1"
    assert "complete this card" in review["body"] and "block" in review["body"]
    assert git.calls == []
    assert done_moves(env) == []
    assert env.kanban.cards[card]["status"] == "done"


def test_merge_check_waits_for_review_done(tmp_path):
    git = FakeGit(ancestry=True)
    env = _env(
        tmp_path, done_gate="merged", merged_base="main", reviewer="alice",
        git_runner=git, which=lambda n: None,
    )
    primed(env, workspace=str(tmp_path), branch="feat/x")
    env.engine.push_outbound()
    review = _review_cards(env)[0]
    env.engine.push_outbound()
    assert git.calls == []
    assert done_moves(env) == []

    env.kanban.set_status(review["id"], "done")
    env.engine.push_outbound()
    assert git.calls
    assert done_moves(env) == [("move_issue", "FAKE-1", "Done")]
    assert env.calls("post_comment") == [("post_comment", "FAKE-1", sign("All green"))]


def test_review_card_not_counted_in_max_active(tmp_path):
    git = FakeGit(ancestry=False)
    env = _env(
        tmp_path, done_gate="merged", merged_base="main", reviewer="alice", max_active=1,
        git_runner=git, which=lambda n: None,
    )
    env.adapter.add_issue("A-1", "first")
    env.adapter.add_issue("B-1", "second")
    assert env.engine.poll_inbound() == 1
    card = env.card_for("A-1")
    env.kanban.set_status(card, "done")
    env.kanban.cards[card]["workspace_path"] = str(tmp_path)
    env.kanban.cards[card]["branch_name"] = "feat/x"
    env.engine.push_outbound()
    assert _review_cards(env)
    assert env.engine.poll_inbound() == 1
    assert env.card_for("B-1")


def test_review_card_never_lands_itself(tmp_path):
    git = FakeGit(ancestry=True)
    env = _env(
        tmp_path, done_gate="merged", merged_base="main", reviewer="alice",
        git_runner=git, which=lambda n: None,
    )
    primed(env, workspace=str(tmp_path), branch="feat/x")
    env.engine.push_outbound()
    review_id = _review_cards(env)[0]["id"]
    env.kanban.set_status(review_id, "done")
    env.engine.push_outbound(review_id)
    assert git.calls == []
    assert done_moves(env) == []
    assert env.state.link("fake:FAKE-1").get("landing") in (None, {})


def test_reviewer_ignored_in_command_mode(tmp_path, caplog):
    env = _env(tmp_path, done_gate="command", land_command=["/bin/land"], reviewer="alice")
    assert env.engine.reviewer is None
    primed(env)
    env.engine.push_outbound()
    assert _review_cards(env) == []
    assert env.launcher.calls
    assert "reviewer is only used" in caplog.text


def test_blocked_review_card_asks_on_the_issue(tmp_path):
    git = FakeGit(ancestry=False)
    env = _env(
        tmp_path, done_gate="merged", merged_base="main", reviewer="alice",
        git_runner=git, which=lambda n: None,
    )
    primed(env, workspace=str(tmp_path), branch="feat/x")
    env.engine.push_outbound()
    review_id = _review_cards(env)[0]["id"]
    env.kanban.block(review_id, "Looks wrong in prod?")
    assert env.engine.sync_block_questions() == 1
    assert questions(env)
    assert "Looks wrong in prod?" in questions(env)[0][2]
