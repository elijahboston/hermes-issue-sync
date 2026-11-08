"""The card <-> tracker issue link, stored on the card itself.

Every synced card carries a first-line marker
``<!-- issue-sync tracker=<tracker> key=<issue_key> url=<url> -->`` and was created
with the idempotency key ``issue-sync:<tracker>:<issue_key>``. Any process that can
read the card can recover the link; no plugin state is needed to know a card is synced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import quote

MARKER_PREFIX = "<!-- issue-sync "
_MARKER_RE = re.compile(
    r"^<!-- issue-sync tracker=(?P<tracker>\S+) key=(?P<key>\S+) url=(?P<url>\S*) -->$"
)
# Author used for every comment the plugin writes on a kanban card.
COMMENT_AUTHOR = "issue-sync"
_DONE_NOTE = "issue-sync: posted the completion summary to {tracker} issue {key}"
_ASKED_NOTE = "issue-sync: asked on {tracker} issue {key} (block {block})"


@dataclass(frozen=True)
class Link:
    tracker: str
    key: str
    url: str = ""

    @property
    def id(self) -> str:
        """Stable id used to key per-link state: ``<tracker>:<issue_key>``."""
        return f"{self.tracker}:{self.key}"


def _token(name: str, value: str) -> str:
    if not value or any(ch.isspace() for ch in value) or "-->" in value:
        raise ValueError(f"issue-sync {name} must be non-empty without whitespace or '-->': {value!r}")
    return value


def build_marker(tracker: str, key: str, url: str = "") -> str:
    # URLs never legitimately contain whitespace or '-->'; percent-encode so the marker stays one token.
    safe_url = quote(url, safe=":/?#[]@!$&'()*+,;=%~") if url else ""
    return f"{MARKER_PREFIX}tracker={_token('tracker', tracker)} key={_token('key', key)} url={safe_url} -->"


def idempotency_key(tracker: str, key: str) -> str:
    return f"issue-sync:{_token('tracker', tracker)}:{_token('key', key)}"


def card_title(key: str, title: str) -> str:
    return f"[{key}] {title}"


def card_body(tracker: str, key: str, url: str, body: str) -> str:
    parts = [build_marker(tracker, key, url)]
    if body and body.strip():
        parts.append(body.strip())
    if url:
        parts.append(f"Tracker issue: {url}")
    return "\n\n".join(parts) + "\n"


def parse_marker(body: str | None) -> Link | None:
    """Parse the marker from the first non-blank line of a card body."""
    if not body:
        return None
    first = body.lstrip().split("\n", 1)[0].strip()
    match = _MARKER_RE.match(first)
    if match is None:
        return None
    return Link(match["tracker"], match["key"], match["url"])


def link_from_card(card: Mapping[str, Any] | None) -> Link | None:
    """Recover the link from a kanban card dict (``list``/``show`` JSON shape)."""
    if not card:
        return None
    return parse_marker(card.get("body"))


def done_note(link: Link) -> str:
    """Card comment recording that the completion summary reached the tracker.

    Lives on the card so a worker-process hook and the dispatcher reconciler, which see
    different plugin state, never both post the summary.
    """
    return _DONE_NOTE.format(tracker=link.tracker, key=link.key)


def has_done_note(comments: Iterable[Mapping[str, Any]] | None, link: Link) -> bool:
    note = done_note(link)
    return any(
        c.get("author") == COMMENT_AUTHOR and (c.get("body") or "").strip() == note
        for c in comments or ()
    )


def asked_note(link: Link, block_id: str) -> str:
    """Card comment recording that the blocked question for ``block_id`` reached the tracker."""
    return _ASKED_NOTE.format(tracker=link.tracker, key=link.key, block=block_id)


def has_note(comments: Iterable[Mapping[str, Any]] | None, note: str) -> bool:
    return any(
        c.get("author") == COMMENT_AUTHOR and (c.get("body") or "").strip() == note
        for c in comments or ()
    )
