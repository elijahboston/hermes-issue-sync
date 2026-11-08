import logging

import pytest

from issue_sync.signature import sign
from issue_sync.adapters.base import AdapterError, RateLimited, TransitionNotAllowed
from issue_sync.adapters.fake import FakeAdapter
from issue_sync.engine import SyncEngine
from issue_sync.kanban import FakeKanban, KanbanError
from issue_sync.links import Link, card_body, card_title, done_note, link_from_card
from issue_sync.state import MemoryStore, SyncState

STATE_MAP = {"running": "In Progress", "review": "In Review", "done": "Done"}


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Env:
    def __init__(self, adapter=None, **settings):
        self.adapter = adapter or FakeAdapter()
        self.kanban = FakeKanban()
        self.state = SyncState(MemoryStore())
        self.clock = Clock()
        self.engine = SyncEngine(self.adapter, self.kanban, self.state,
                                 {"tracker": "fake", "state_map": STATE_MAP, **settings}, clock=self.clock)

    def card_for(self, key: str) -> str:
        """Id of the card linked to ``key``, created through a normal inbound poll."""
        ids = [cid for cid, card in self.kanban.cards.items() if (link_from_card(card) or Link("", "")).key == key]
        assert len(ids) == 1, ids
        return ids[0]

    def calls(self, method: str) -> list[tuple]:
        return [c for c in self.adapter.calls if c[0] == method]

    def kanban_calls(self, method: str) -> list[tuple]:
        return [c for c in self.kanban.calls if c[0] == method]


@pytest.fixture
def env() -> Env:
    return Env()


def linked(env: Env, *keys: str, status: str = "ready") -> list[str]:
    """Create tracker issues, poll them in, and put their cards in ``status``."""
    for key in keys:
        env.adapter.add_issue(key, f"Issue {key}")
    env.engine.poll_inbound()
    ids = [env.card_for(key) for key in keys]
    for card_id in ids:
        env.kanban.set_status(card_id, status)
    return ids


# ── inbound ───────────────────────────────────────────────────────────────

def test_inbound_creates_card_with_marker_key_and_assignee():
    env = Env(assignee="worker", filter={"labels": ["hermes"]})
    env.adapter.add_issue("FAKE-1", "Fix login", body="Steps to reproduce", url="https://t.invalid/FAKE-1",
                          labels=["hermes"])
    env.adapter.add_issue("FAKE-2", "Unlabelled")

    assert env.engine.poll_inbound() == 1

    assert env.calls("fetch_issues") == [("fetch_issues", {"labels": ["hermes"]})]
    card_id = env.card_for("FAKE-1")
    card = env.kanban.cards[card_id]
    assert card["title"] == card_title("FAKE-1", "Fix login") == "[FAKE-1] Fix login"
    assert card["body"] == card_body("fake", "FAKE-1", "https://t.invalid/FAKE-1", "Steps to reproduce")
    assert link_from_card(card) == Link("fake", "FAKE-1", "https://t.invalid/FAKE-1")
    assert card["assignee"] == "worker"
    assert env.kanban.idempotency_keys[card_id] == "issue-sync:fake:FAKE-1"
    assert env.state.link("fake:FAKE-1") == {"card_id": card_id, "last_pushed_state": "Todo", "last_error": None}


def test_blank_assignee_means_unassigned():
    env = Env(assignee="  ")
    env.adapter.add_issue("FAKE-1")
    env.engine.poll_inbound()
    assert env.kanban_calls("create_card")[0][4] is None


def test_second_poll_creates_nothing(env):
    env.adapter.add_issue("FAKE-1")
    assert env.engine.poll_inbound() == 1
    assert env.engine.poll_inbound() == 0
    assert len(env.kanban_calls("create_card")) == 1
    assert len(env.kanban.cards) == 1


@pytest.mark.parametrize("status", ["ready", "done", "archived"])
def test_existing_marked_card_is_not_duplicated(env, status):
    issue = env.adapter.add_issue("FAKE-1", "Fix")
    env.kanban.add_card(card_title(issue.key, issue.title), card_body("fake", issue.key, issue.url, ""), status=status)
    assert env.engine.poll_inbound() == 0
    assert env.kanban_calls("create_card") == []
    assert env.kanban_calls("list_cards") == [("list_cards", None, True)]


def test_card_linked_to_another_tracker_does_not_count(env):
    env.adapter.add_issue("FAKE-1")
    env.kanban.add_card("[FAKE-1] other", card_body("linear", "FAKE-1", "", ""))
    assert env.engine.poll_inbound() == 1


def test_create_failure_is_isolated_and_retried(env):
    env.adapter.add_issue("FAKE-1")
    env.adapter.add_issue("FAKE-2")
    env.kanban.fail_next("create_card", KanbanError("board locked"))

    assert env.engine.poll_inbound() == 1
    assert env.state.link("fake:FAKE-1") == {"last_error": "board locked"}
    env.card_for("FAKE-2")

    assert env.engine.poll_inbound() == 1
    env.card_for("FAKE-1")
    assert env.state.link("fake:FAKE-1")["last_error"] is None


# ── outbound ──────────────────────────────────────────────────────────────

def test_outbound_moves_once_per_status_change(env):
    (card_id,) = linked(env, "FAKE-1")
    env.kanban.set_status(card_id, "running")

    assert env.engine.push_outbound() == 1
    assert env.engine.push_outbound() == 0
    assert env.calls("move_issue") == [("move_issue", "FAKE-1", "In Progress")]
    assert env.state.link("fake:FAKE-1")["last_pushed_state"] == "In Progress"

    env.kanban.set_status(card_id, "review")
    assert env.engine.push_outbound() == 1
    assert env.calls("move_issue")[-1] == ("move_issue", "FAKE-1", "In Review")
    assert env.adapter.issues["FAKE-1"].state == "In Review"


def test_state_observed_at_link_time_is_not_pushed_again(env):
    env.adapter.add_issue("FAKE-1", state="In Progress")
    env.engine.poll_inbound()
    env.kanban.set_status(env.card_for("FAKE-1"), "running")
    assert env.engine.push_outbound() == 0
    assert env.calls("move_issue") == []


@pytest.mark.parametrize("status", ["ready", "blocked", "triage", "archived"])
def test_unmapped_status_is_ignored(env, status):
    linked(env, "FAKE-1", status=status)
    env.engine.tick(force=True)
    assert env.calls("move_issue") == []
    assert env.calls("post_comment") == []


def test_cards_of_another_tracker_or_unmarked_are_ignored(env):
    env.kanban.add_card("[LIN-1] x", card_body("linear", "LIN-1", "", ""), status="running")
    env.kanban.add_card("plain card", "no marker here", status="done")
    assert env.engine.push_outbound() == 0
    assert env.adapter.calls == []
    assert env.state.links() == {}


def test_push_outbound_task_id_only_touches_that_card(env):
    first, second = linked(env, "FAKE-1", "FAKE-2", status="running")
    env.kanban.calls.clear()

    assert env.engine.push_outbound(first) == 1

    assert env.calls("move_issue") == [("move_issue", "FAKE-1", "In Progress")]
    assert env.kanban.calls == [("show_card", first)]
    assert "last_pushed_state" in env.state.link("fake:FAKE-1")
    assert env.state.link("fake:FAKE-2")["last_pushed_state"] == "Todo"


def test_push_outbound_task_id_done_uses_one_show(env):
    (card_id,) = linked(env, "FAKE-1", status="done")
    env.kanban.set_summary(card_id, "All green")
    env.kanban.calls.clear()

    assert env.engine.push_outbound(card_id) == 1
    assert env.kanban_calls("show_card") == [("show_card", card_id)]
    assert env.calls("post_comment") == [("post_comment", "FAKE-1", sign("All green"))]


def test_rate_limited_propagates_from_direct_calls(env):
    linked(env, "FAKE-1", status="running")
    env.adapter.fail_next("move_issue", RateLimited(5))
    with pytest.raises(RateLimited):
        env.engine.push_outbound()
    env.adapter.fail_next("fetch_issues", RateLimited(5))
    with pytest.raises(RateLimited):
        env.engine.poll_inbound()


# ── done summary ──────────────────────────────────────────────────────────

def test_done_posts_latest_summary_once_and_notes_the_card(env):
    (card_id,) = linked(env, "FAKE-1", status="running")
    env.engine.tick(force=True)
    env.kanban.set_status(card_id, "done")
    env.kanban.set_summary(card_id, "  Fixed the login redirect.  ")

    env.engine.tick(force=True)
    env.engine.tick(force=True)

    assert env.calls("post_comment") == [("post_comment", "FAKE-1", sign("Fixed the login redirect."))]
    assert env.calls("move_issue") == [("move_issue", "FAKE-1", "In Progress"), ("move_issue", "FAKE-1", "Done")]
    note = done_note(Link("fake", "FAKE-1"))
    assert [(c["author"], c["body"]) for c in env.kanban.comments[card_id]] == [("issue-sync", note)]
    assert env.state.link("fake:FAKE-1")["done_comment_posted"] is True


def test_done_without_summary_posts_fallback(env):
    (card_id,) = linked(env, "FAKE-1", status="done")
    env.engine.push_outbound()
    assert env.calls("post_comment") == [("post_comment", "FAKE-1", sign(f"Completed by Hermes kanban card {card_id}"))]


def test_done_note_from_another_process_prevents_posting(env):
    (card_id,) = linked(env, "FAKE-1", status="done")
    env.kanban.set_summary(card_id, "Done elsewhere")
    env.kanban.comment(card_id, done_note(Link("fake", "FAKE-1")))
    env.kanban.calls.clear()

    env.engine.tick(force=True)

    assert env.calls("post_comment") == []
    assert env.kanban_calls("comment") == []
    assert env.state.link("fake:FAKE-1")["done_comment_posted"] is True


def test_refused_done_transition_still_posts_summary_once():
    env = Env(FakeAdapter(allowed_states={"In Progress"}))
    (card_id,) = linked(env, "FAKE-1", status="done")
    env.kanban.set_summary(card_id, "Shipped")

    env.engine.tick(force=True)
    env.engine.tick(force=True)

    assert env.calls("post_comment") == [("post_comment", "FAKE-1", sign("Shipped"))]
    assert len(env.calls("move_issue")) == 2  # the move is retried, the summary is not
    assert "cannot move" in env.state.link("fake:FAKE-1")["last_error"]


def test_failed_card_note_never_reposts_summary(env):
    (card_id,) = linked(env, "FAKE-1", status="done")
    env.kanban.fail_next("comment", KanbanError("comment failed"))

    env.engine.tick(force=True)
    assert env.state.link("fake:FAKE-1")["last_error"] == "comment failed"
    env.engine.tick(force=True)

    assert len(env.calls("post_comment")) == 1
    assert env.state.link("fake:FAKE-1")["done_comment_posted"] is True
    assert env.state.link("fake:FAKE-1")["last_error"] is None


# ── error isolation ───────────────────────────────────────────────────────

@pytest.mark.parametrize("exc", [TransitionNotAllowed("workflow forbids it"), AdapterError("workflow forbids it"),
                                 RuntimeError("workflow forbids it")])
def test_one_failing_link_does_not_stop_the_others(env, exc, caplog):
    linked(env, "FAKE-1", "FAKE-2", status="running")
    env.adapter.fail_next("move_issue", exc, key="FAKE-1")

    with caplog.at_level(logging.WARNING, logger="issue_sync.engine"):
        assert env.engine.tick(force=True) is True

    assert env.state.link("fake:FAKE-1")["last_error"] == "workflow forbids it"
    assert env.state.link("fake:FAKE-1")["last_pushed_state"] == "Todo"
    assert env.state.link("fake:FAKE-2")["last_pushed_state"] == "In Progress"
    assert env.state.link("fake:FAKE-2")["last_error"] is None
    assert env.state.last_error is None
    assert "fake:FAKE-1" in caplog.text

    env.clock.advance(60)
    assert env.engine.tick() is True
    assert env.state.link("fake:FAKE-1")["last_error"] is None
    assert env.state.link("fake:FAKE-1")["last_pushed_state"] == "In Progress"


def test_tick_never_raises_on_unexpected_fetch_error(env):
    (card_id,) = linked(env, "FAKE-1", status="running")
    env.adapter.fail_next("fetch_issues", ValueError("boom"))

    assert env.engine.tick(force=True) is True
    assert env.state.last_error == "ValueError: boom"
    # Outbound is independent of the tracker listing and still ran.
    assert env.calls("move_issue") == [("move_issue", "FAKE-1", "In Progress")]

    assert env.engine.tick(force=True) is True
    assert env.state.last_error is None


def test_tick_survives_kanban_listing_failure(env):
    env.kanban.fail_next("list_cards", KanbanError("hermes not found"))
    assert env.engine.tick(force=True) is True
    assert env.state.last_error == "KanbanError: hermes not found"
    assert env.adapter.calls == []


# ── scheduling ────────────────────────────────────────────────────────────

def test_poll_interval_respected_and_force_bypasses():
    env = Env(poll_interval_seconds=60)
    assert env.engine.due() is True
    assert env.engine.tick() is True
    assert env.state.last_poll_at == env.clock.now

    assert env.engine.tick() is False
    env.clock.advance(59)
    assert env.engine.due() is False
    assert env.engine.tick() is False
    assert len(env.calls("fetch_issues")) == 1

    assert env.engine.tick(force=True) is True
    assert len(env.calls("fetch_issues")) == 2
    env.clock.advance(59)
    assert env.engine.tick() is False
    env.clock.advance(1)
    assert env.engine.tick() is True
    assert len(env.calls("fetch_issues")) == 3


def test_rate_limited_backs_off_and_skips_outbound(env):
    linked(env, "FAKE-1", status="running")
    env.adapter.fail_next("fetch_issues", RateLimited(120))
    start = env.clock.now

    assert env.engine.tick(force=True) is True
    assert env.state.backoff_until == start + 120
    assert env.calls("move_issue") == []
    assert env.state.last_error.startswith("RateLimited:")

    env.clock.advance(61)
    assert env.engine.due() is False
    assert env.engine.tick() is False
    env.clock.advance(59)
    assert env.engine.due() is True
    assert env.engine.tick() is True
    assert env.calls("move_issue") == [("move_issue", "FAKE-1", "In Progress")]
    assert env.state.backoff_until is None
    assert env.state.last_error is None


def test_rate_limited_backoff_has_a_floor(env):
    env.adapter.fail_next("fetch_issues", RateLimited(0))
    env.engine.tick(force=True)
    assert env.state.backoff_until == env.clock.now + 1.0


def test_rate_limited_during_outbound_also_backs_off(env):
    linked(env, "FAKE-1", "FAKE-2", status="running")
    env.adapter.fail_next("move_issue", RateLimited(30), key="FAKE-1")
    env.engine.tick(force=True)
    assert env.state.backoff_until == env.clock.now + 30
    assert env.calls("move_issue") == [("move_issue", "FAKE-1", "In Progress")]


def test_concurrent_calls_skip_instead_of_waiting():
    seen = {}

    class Reentrant(FakeAdapter):
        def fetch_issues(self, filter):
            seen["tick"] = engine.tick(force=True)
            seen["push"] = engine.push_outbound()
            seen["poll"] = engine.poll_inbound()
            return super().fetch_issues(filter)

    env = Env(Reentrant())
    engine = env.engine
    assert engine.tick(force=True) is True
    assert seen == {"tick": False, "push": 0, "poll": 0}
    assert engine.tick(force=True) is True  # lock released afterwards


# ── settings coercion ─────────────────────────────────────────────────────

@pytest.mark.parametrize("interval, expected", [("30", 30.0), (5, 5.0), ("soon", 60.0), (-1, 60.0), (0, 60.0),
                                                (None, 60.0), (True, 60.0), (float("nan"), 60.0)])
def test_poll_interval_coercion(interval, expected):
    assert Env(poll_interval_seconds=interval).engine.poll_interval == expected


def test_bad_filter_and_state_map_are_ignored():
    env = Env(filter="state=Todo", state_map=["running"])
    env.adapter.add_issue("FAKE-1")
    assert env.engine.poll_inbound() == 1
    assert env.calls("fetch_issues") == [("fetch_issues", {})]
    env.kanban.set_status(env.card_for("FAKE-1"), "running")
    assert env.engine.push_outbound() == 0


def test_state_map_drops_unknown_statuses_and_blank_targets():
    env = Env(state_map={"running": "Doing", "todo": "Todo", "review": "", "done": 3})
    assert env.engine.state_map == {"running": "Doing"}
