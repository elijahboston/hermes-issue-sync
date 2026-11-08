"""Two-way blocking (CORE-605): a blocked card's question goes to the tracker issue, and the
first human reply there unblocks the card with the answer."""

from ticket_sync.adapters.base import AdapterError
from ticket_sync.links import asked_note, link_from_card
from ticket_sync.signature import is_signed

from test_engine import Env, linked


def questions(env):
    return [c for c in env.calls("post_comment") if "blocked on this issue" in c[2]]


def human_reply(env, key, text):
    """A reply posted with the same account as the plugin (no signature)."""
    env.adapter.post_comment(key, text)


def test_blocked_card_posts_signed_question_once():
    env = Env()
    (card,) = linked(env, "FAKE-1")
    env.kanban.block(card, "Which database, staging or prod?")
    assert env.engine.sync_block_questions() == 1
    assert env.engine.sync_block_questions() == 0
    (post,) = questions(env)
    assert "> Which database, staging or prod?" in post[2] and is_signed(post[2])
    link = link_from_card(env.kanban.cards[card])
    assert link is not None
    assert any(c["body"].startswith("ticket-sync: asked on fake issue FAKE-1") for c in env.kanban.comments[card])
    assert env.state.link(link.id)["asked_block"]


def test_human_reply_unblocks_with_answer_and_ignores_own_comments():
    env = Env()
    (card,) = linked(env, "FAKE-1")
    env.kanban.block(card, "Which database?")
    env.engine.sync_block_questions()
    assert env.engine.sync_block_questions() == 0  # only our own question so far
    assert env.kanban_calls("unblock") == []
    human_reply(env, "FAKE-1", "Use staging.")
    assert env.engine.sync_block_questions() == 1
    (call,) = env.kanban_calls("unblock")
    assert call[1] == card and "Use staging." in call[2] and "FAKE-1" in call[2]
    assert env.kanban.cards[card]["status"] == "ready"


def test_comments_before_the_question_are_not_answers():
    env = Env()
    (card,) = linked(env, "FAKE-1")
    human_reply(env, "FAKE-1", "Old chatter from last week.")
    env.kanban.block(card, "Which database?")
    env.engine.sync_block_questions()
    assert env.engine.sync_block_questions() == 0
    assert env.kanban_calls("unblock") == []


def test_answer_relayed_once_even_if_card_still_shows_blocked():
    env = Env()
    (card,) = linked(env, "FAKE-1")
    env.kanban.block(card, "Q?")
    env.engine.sync_block_questions()
    human_reply(env, "FAKE-1", "A.")
    env.engine.sync_block_questions()
    env.kanban.cards[card]["status"] = "blocked"  # board not yet updated
    human_reply(env, "FAKE-1", "Another note.")
    assert env.engine.sync_block_questions() == 0
    assert len(env.kanban_calls("unblock")) == 1


def test_second_block_asks_again():
    env = Env()
    (card,) = linked(env, "FAKE-1")
    env.kanban.block(card, "First question?")
    env.engine.sync_block_questions()
    human_reply(env, "FAKE-1", "First answer.")
    env.engine.sync_block_questions()
    env.kanban.block(card, "Second question?")
    assert env.engine.sync_block_questions() == 1
    assert len(questions(env)) == 2
    human_reply(env, "FAKE-1", "Second answer.")
    env.engine.sync_block_questions()
    unblocks = env.kanban_calls("unblock")
    assert len(unblocks) == 2 and "Second answer." in unblocks[1][2] and "First answer." not in unblocks[1][2]


def test_question_asked_by_other_process_is_not_reposted():
    env = Env()
    (card,) = linked(env, "FAKE-1")
    env.kanban.block(card, "Q?")
    env.engine.sync_block_questions()
    # A fresh engine with empty state (another process) sees the card note and does not ask again.
    other = Env(adapter=env.adapter)
    other.kanban = env.kanban
    other.engine.kanban = env.kanban
    assert other.engine.sync_block_questions() == 0
    assert len(questions(env)) == 1


def test_unlinked_and_other_tracker_cards_are_ignored():
    env = Env()
    plain = env.kanban.add_card("no link")
    env.kanban.block(plain, "Q?")
    other = env.kanban.add_card("jira card", "<!-- ticket-sync tracker=jira key=TSS-1 url= -->\n")
    env.kanban.block(other, "Q?")
    assert env.engine.sync_block_questions() == 0
    assert questions(env) == []


def test_tracker_error_is_recorded_not_raised():
    env = Env()
    (card,) = linked(env, "FAKE-1")
    env.kanban.block(card, "Q?")
    env.adapter.fail_next("post_comment", AdapterError("fake: boom"))
    assert env.engine.sync_block_questions() == 0
    link = link_from_card(env.kanban.cards[card])
    assert link is not None
    assert "boom" in env.state.link(link.id)["last_error"]
    assert env.engine.sync_block_questions() == 1  # retried on the next pass


def test_full_tick_runs_blocking_phase():
    env = Env(poll_interval_seconds=0)
    (card,) = linked(env, "FAKE-1")
    env.kanban.block(card, "Q?")
    env.engine.tick(force=True)
    assert len(questions(env)) == 1


def test_hook_path_handles_blocked_card():
    env = Env()
    (card,) = linked(env, "FAKE-1")
    env.kanban.block(card, "Q?")
    env.engine.push_outbound(card)
    assert len(questions(env)) == 1


def test_asked_note_is_stable():
    from ticket_sync.links import Link
    assert asked_note(Link("linear", "CORE-1"), "1:2") == "ticket-sync: asked on linear issue CORE-1 (block 1:2)"
