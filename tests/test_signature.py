"""The plugin tells its own tracker comments apart by a signature line, not by author.

The plugin posts with the user's own API key, so author checks would mistake the user's real
replies for the plugin's comments (found in the Linear end-to-end run, CORE-603).
"""

from issue_sync.adapters.fake import FakeAdapter
from issue_sync.adapters.base import Issue
from issue_sync.signature import SIGNATURE, is_signed, sign, strip_signature


def test_sign_appends_signature_as_last_line_once():
    signed = sign("All green")
    assert signed == f"All green\n\n{SIGNATURE}"
    assert sign(signed) == signed
    assert sign("") == SIGNATURE


def test_is_signed_only_on_last_line():
    assert is_signed(sign("hi"))
    assert not is_signed("hi")
    assert not is_signed(None)
    # A human quoting the signature mid-reply is still a human reply.
    assert not is_signed(f"{SIGNATURE}\n\nThanks, use the staging DB.")


def test_is_signed_survives_tracker_markdown_rewrites():
    assert is_signed("done\n\n\\— via Hermes issue-sync")  # escaped dash
    assert is_signed("done\n\n*— via Hermes issue-sync*")  # italics
    assert is_signed("done\n\n— via  Hermes  issue-sync   \n\n")  # extra spaces, trailing blanks
    assert is_signed("done\n\n— VIA HERMES ISSUE-SYNC")


def test_strip_signature():
    assert strip_signature(sign("line one\nline two")) == "line one\nline two"
    assert strip_signature("plain") == "plain"


def test_fake_adapter_flags_by_signature_not_author():
    fake = FakeAdapter()
    fake.issues["FAKE-1"] = Issue("FAKE-1", "t", "", "", "Todo")
    mine = fake.post_comment("FAKE-1", sign("question"))
    human = fake.post_comment("FAKE-1", "answer from the same account")
    assert (mine.is_bot, human.is_bot) == (True, False)
