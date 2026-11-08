from issue_sync.adapters import adf


def doc(*content):
    return {"type": "doc", "version": 1, "content": list(content)}


def para(*inline):
    return {"type": "paragraph", "content": list(inline)}


def text(value):
    return {"type": "text", "text": value}


def test_paragraphs_and_hard_breaks():
    tree = doc(para(text("Hello "), text("world")), para(text("line one"), {"type": "hardBreak"}, text("line two")))
    assert adf.to_text(tree) == "Hello world\n\nline one\nline two"


def test_mentions_lists_and_code():
    tree = doc(
        para({"type": "mention", "attrs": {"id": "abc", "text": "@Ada"}}, text(" please check")),
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [para(text("first"))]},
            {"type": "listItem", "content": [para(text("second"))]},
        ]},
        {"type": "orderedList", "content": [{"type": "listItem", "content": [para(text("step"))]}]},
        {"type": "codeBlock", "content": [text("print(1)\nprint(2)")]},
    )
    out = adf.to_text(tree)
    assert "@Ada please check" in out
    assert "- first\n- second" in out
    assert "1. step" in out
    assert "```\nprint(1)\nprint(2)\n```" in out


def test_mention_without_at_sign_gets_one():
    assert adf.to_text(doc(para({"type": "mention", "attrs": {"text": "Bob"}}))) == "@Bob"


def test_unknown_nodes_fall_back_to_their_text():
    tree = doc({"type": "mysteryBlock", "content": [para(text("inside"))]},
               para({"type": "status", "content": [text("chip")]}))
    assert "inside" in adf.to_text(tree)
    assert "chip" in adf.to_text(tree)


def test_none_string_and_garbage():
    assert adf.to_text(None) == ""
    assert adf.to_text("already plain") == "already plain"
    assert adf.to_text(42) == ""


def test_from_text_round_trips_lines():
    built = adf.from_text("first\n\nsecond\n")
    assert built["type"] == "doc" and built["version"] == 1
    assert [p["content"][0]["text"] for p in built["content"]] == ["first", "second"]
    assert adf.to_text(built) == "first\n\nsecond"


def test_from_text_empty_is_valid_doc():
    built = adf.from_text("")
    assert built["content"] == [{"type": "paragraph", "content": []}]
