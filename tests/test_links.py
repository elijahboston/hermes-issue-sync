import pytest

from issue_sync import links
from issue_sync.links import Link


def test_marker_round_trip():
    marker = links.build_marker("linear", "ENG-12", "https://linear.app/x/issue/ENG-12")
    assert marker == "<!-- issue-sync tracker=linear key=ENG-12 url=https://linear.app/x/issue/ENG-12 -->"
    assert links.parse_marker(marker) == Link("linear", "ENG-12", "https://linear.app/x/issue/ENG-12")


def test_url_with_spaces_is_percent_encoded_and_parses():
    marker = links.build_marker("jira", "OPS-1", "https://example.com/a b?q=x y")
    assert " b" not in marker
    link = links.parse_marker(marker)
    assert link == Link("jira", "OPS-1", "https://example.com/a%20b?q=x%20y")


def test_url_containing_marker_terminator_still_parses():
    link = links.parse_marker(links.build_marker("fake", "K-1", "https://e.com/-->x"))
    assert link is not None and link.key == "K-1"
    assert "-->" not in link.url


def test_empty_url():
    marker = links.build_marker("fake", "K-1")
    assert marker.endswith(" url= -->")
    assert links.parse_marker(marker) == Link("fake", "K-1", "")


def test_marker_must_be_first_non_blank_line():
    marker = links.build_marker("fake", "K-1")
    assert links.parse_marker(f"intro text\n{marker}\n") is None


def test_leading_blank_lines_tolerated():
    marker = links.build_marker("fake", "K-1")
    assert links.parse_marker(f"\n\n   \n  {marker}  \nrest") == Link("fake", "K-1", "")


@pytest.mark.parametrize(
    "body",
    [None, "", "   \n", "just a card", "<!-- issue-sync tracker=x -->", "<!-- issue-sync garbage"],
)
def test_garbage_bodies_return_none(body):
    assert links.parse_marker(body) is None


@pytest.mark.parametrize(
    ("tracker", "key"),
    [("", "K-1"), ("fake", ""), ("fa ke", "K-1"), ("fake", "K 1"), ("fake", "K\t1"), ("fa-->ke", "K-1"), ("fake", "K-->1")],
)
def test_invalid_tokens_raise(tracker, key):
    with pytest.raises(ValueError):
        links.build_marker(tracker, key)
    with pytest.raises(ValueError):
        links.idempotency_key(tracker, key)


def test_idempotency_key_format():
    assert links.idempotency_key("linear", "ENG-12") == "issue-sync:linear:ENG-12"


def test_card_title_format():
    assert links.card_title("ENG-12", "Fix login") == "[ENG-12] Fix login"


def test_card_body_layout_and_round_trip():
    body = links.card_body("linear", "ENG-12", "https://l.app/ENG-12", "  Steps to reproduce\n")
    lines = body.split("\n")
    assert lines[0] == links.build_marker("linear", "ENG-12", "https://l.app/ENG-12")
    assert "Steps to reproduce" in body
    assert "Tracker issue: https://l.app/ENG-12" in body
    assert links.parse_marker(body) == Link("linear", "ENG-12", "https://l.app/ENG-12")


def test_card_body_without_body_or_url():
    body = links.card_body("fake", "K-1", "", "   ")
    assert body == links.build_marker("fake", "K-1") + "\n"
    assert "Tracker issue" not in body
    assert links.parse_marker(body) == Link("fake", "K-1", "")


def test_link_from_card():
    card = {"id": "t_1", "body": links.card_body("fake", "K-1", "", "x")}
    assert links.link_from_card(card) == Link("fake", "K-1", "")
    assert links.link_from_card(None) is None
    assert links.link_from_card({}) is None
    assert links.link_from_card({"id": "t_2"}) is None
    assert links.link_from_card({"id": "t_3", "body": None}) is None


def test_link_id():
    assert Link("linear", "ENG-12").id == "linear:ENG-12"
    assert Link("linear", "ENG-12", "u").id == "linear:ENG-12"


def test_done_note_and_has_done_note():
    link = Link("fake", "K-1")
    note = links.done_note(link)
    assert "fake" in note and "K-1" in note
    assert not links.has_done_note(None, link)
    assert not links.has_done_note([], link)
    assert links.has_done_note([{"author": links.COMMENT_AUTHOR, "body": note}], link)
    assert links.has_done_note([{"author": "issue-sync", "body": f"\n  {note}  \n"}], link)


def test_has_done_note_ignores_other_authors_and_links():
    link = Link("fake", "K-1")
    note = links.done_note(link)
    assert not links.has_done_note([{"author": "alice", "body": note}], link)
    assert not links.has_done_note([{"body": note}], link)
    assert not links.has_done_note([{"author": "issue-sync", "body": None}], link)
    other = links.done_note(Link("fake", "K-2"))
    assert not links.has_done_note([{"author": "issue-sync", "body": other}], link)
    assert links.has_done_note(
        [{"author": "alice", "body": "hi"}, {"author": "issue-sync", "body": note}], link
    )
