"""Signature line on every tracker comment the plugin writes.

The plugin posts with the user's own API key, so on the tracker its comments carry the user's
name. Author checks therefore cannot tell the plugin's comments from the user's real replies.
Instead every comment the plugin posts ends with a visible signature line, and a comment is the
plugin's own exactly when its last non-empty line is that signature.

The signature is plain visible text (not an HTML comment): Linear's markdown and Jira's ADF both
keep it, and people reading the issue can see which comments came from Hermes.
"""

from __future__ import annotations

SIGNATURE = "— via Hermes issue-sync"
# Matched loosely so tracker markdown rewrites (escaped dashes, italics, extra spaces) still count.
_NEEDLE = "via hermes issue-sync"


def _last_line(body: str) -> str:
    for line in reversed((body or "").splitlines()):
        if line.strip():
            return line.strip()
    return ""


def is_signed(body: str | None) -> bool:
    """True when ``body`` was posted by the plugin (its last non-empty line is the signature)."""
    last = _last_line(body or "").casefold().replace("\\", "").replace("*", "").replace("_", "")
    return _NEEDLE in " ".join(last.split())


def sign(body: str) -> str:
    """``body`` with the signature as its last line. Idempotent."""
    text = (body or "").rstrip()
    if is_signed(text):
        return text
    return f"{text}\n\n{SIGNATURE}" if text else SIGNATURE


def strip_signature(body: str | None) -> str:
    """``body`` without a trailing signature line (for showing the plugin's text back)."""
    text = (body or "").rstrip()
    if not is_signed(text):
        return text
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines[:-1]).rstrip()
