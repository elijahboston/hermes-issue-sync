"""Atlassian Document Format (ADF) <-> plain text, just enough for issue bodies and comments.

Jira Cloud REST v3 returns descriptions and comments as ADF trees and requires ADF when posting.
``to_text`` flattens a tree to readable text; ``from_text`` builds a minimal document (one
paragraph per line, blank lines dropped).
"""

from __future__ import annotations

from typing import Any

_BLOCKS = {"paragraph", "heading", "blockquote", "codeBlock", "panel", "rule", "mediaSingle", "table"}


def to_text(node: Any) -> str:
    """Plain text for an ADF document (or any node). ``None`` and non-ADF values give ``""``."""
    if isinstance(node, str):  # some endpoints already return plain text
        return node
    if not isinstance(node, dict):
        return ""
    lines: list[str] = []
    _block(node, lines, indent="")
    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


def _inline(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return str(node.get("text") or "")
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        attrs = node.get("attrs") or {}
        name = str(attrs.get("text") or attrs.get("displayName") or attrs.get("id") or "")
        return name if name.startswith("@") else f"@{name}"
    if kind == "emoji":
        attrs = node.get("attrs") or {}
        return str(attrs.get("text") or attrs.get("shortName") or "")
    if kind == "inlineCard":
        return str((node.get("attrs") or {}).get("url") or "")
    return "".join(_inline(child) for child in node.get("content") or [])


def _block(node: dict[str, Any], lines: list[str], indent: str) -> None:
    kind = node.get("type")
    content = node.get("content") or []
    if kind in ("doc", "panel", "blockquote", "tableRow", "tableCell", "tableHeader", "table", "expand"):
        for child in content:
            if isinstance(child, dict):
                _block(child, lines, indent)
        return
    if kind in ("bulletList", "orderedList"):
        for number, item in enumerate(content, start=1):
            marker = f"{number}." if kind == "orderedList" else "-"
            sub: list[str] = []
            for child in (item or {}).get("content") or []:
                if isinstance(child, dict):
                    _block(child, sub, indent + "  ")
            sub = [line for line in sub if line.strip()] or [""]
            lines.append(f"{indent}{marker} {sub[0].strip()}")
            lines.extend(sub[1:])
        lines.append("")
        return
    if kind == "codeBlock":
        lines.append("```")
        lines.extend("".join(_inline(c) for c in content).splitlines())
        lines.append("```")
        lines.append("")
        return
    if kind == "rule":
        lines.append("---")
        return
    text = "".join(_inline(child) for child in content)
    for line in text.split("\n"):
        lines.append(f"{indent}{line}" if line else "")
    if kind in _BLOCKS or kind is None:
        lines.append("")


def from_text(text: str) -> dict[str, Any]:
    """A minimal ADF document: one paragraph per non-empty line."""
    paragraphs = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]}
        for line in (text or "").splitlines()
        if line.strip()
    ]
    if not paragraphs:
        paragraphs = [{"type": "paragraph", "content": []}]
    return {"type": "doc", "version": 1, "content": paragraphs}
