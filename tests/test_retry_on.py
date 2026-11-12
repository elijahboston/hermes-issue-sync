"""retry_on: auto-unblock a matching block once (CORE-633)."""

from __future__ import annotations

from issue_sync.connections import parse_connections
from issue_sync.links import link_from_card

from test_blocking import questions
from test_engine import Env, linked


def test_retry_on_unblocks_once_with_no_issue_comment():
    env = Env(retry_on=["token.*expired"])
    (card,) = linked(env, "FAKE-1")
    env.kanban.block(card, "Token expired, please refresh")
    assert env.engine.sync_block_questions() == 0
    assert env.kanban.cards[card]["status"] == "ready"
    (call,) = env.kanban_calls("unblock")
    assert call[1] == card
    assert call[2] == "issue-sync: automatic retry (block reason matched token.*expired)"
    assert env.calls("post_comment") == []
    assert questions(env) == []
    assert env.state.link("fake:FAKE-1")["auto_retried"] is True


def test_second_matching_block_asks_the_question():
    env = Env(retry_on=["token.*expired"])
    (card,) = linked(env, "FAKE-1")
    env.kanban.block(card, "token expired")
    env.engine.sync_block_questions()
    env.kanban.block(card, "token expired again")
    assert env.engine.sync_block_questions() == 1
    assert len(questions(env)) == 1
    assert len(env.kanban_calls("unblock")) == 1
    link = link_from_card(env.kanban.cards[card])
    assert link is not None
    assert env.state.link(link.id)["asked_block"]


def test_no_match_asks_the_question():
    env = Env(retry_on=["token.*expired"])
    (card,) = linked(env, "FAKE-1")
    env.kanban.block(card, "Which database, staging or prod?")
    assert env.engine.sync_block_questions() == 1
    assert len(questions(env)) == 1
    assert env.kanban_calls("unblock") == []
    assert not env.state.link("fake:FAKE-1").get("auto_retried")


def test_retry_on_is_case_insensitive():
    env = Env(retry_on=["MAX(IMUM)? OUTPUT TOKENS"])
    (card,) = linked(env, "FAKE-1")
    env.kanban.block(card, "Hit max output tokens")
    env.engine.sync_block_questions()
    assert env.kanban.cards[card]["status"] == "ready"
    assert "MAX(IMUM)? OUTPUT TOKENS" in env.kanban_calls("unblock")[0][2]
    assert questions(env) == []


def test_invalid_retry_on_regex_is_a_connection_error():
    (conn,) = parse_connections({"tracker": "linear", "retry_on": ["foo("]})
    assert conn.error and "invalid regex" in conn.error


def test_unset_retry_on_never_retries():
    env = Env()
    (card,) = linked(env, "FAKE-1")
    env.kanban.block(card, "token expired")
    env.engine.sync_block_questions()
    assert questions(env)
    assert env.kanban_calls("unblock") == []
