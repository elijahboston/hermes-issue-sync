"""Pure functions of the Desktop pane (desktop/plugin.js), run under node with stubbed imports."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node not installed")

NOW = 1_800_000_000


def js(expr: str):
    out = subprocess.run([NODE, str(REPO / "tests" / "pane_js_harness.mjs"), str(REPO / "desktop" / "plugin.js"), expr],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def item(key, status="running", **extra):
    return {"key": key, "card_id": f"t_{key}", "card_status": status, "title": key, "url": f"https://x/{key}", **extra}


def conn(name, tracker="linear", board="b", items=(), **extra):
    return {"name": name, "tracker": tracker, "board": board, "projects": [name], "items": list(items),
            "running": True, "last_poll_at": NOW - 10, "poll_interval_seconds": 60, **extra}


def call(fn: str, *args):
    return js(f"m.{fn}(" + ", ".join(json.dumps(a) for a in args) + ")")


# -- vocabulary / small helpers ------------------------------------------------------------------

def test_short_times():
    assert [call("shortAgo", NOW - d, NOW) for d in (2, 23, 300, 7200, 3 * 86400)] == \
        ["just now", "23s ago", "5m ago", "2h ago", "3d ago"]
    assert call("shortAgo", None, NOW) == "never"


def test_card_kind_uses_one_word_per_idea():
    kinds = [call("cardKind", i) for i in (
        item("A", "ready"), item("B", "running"), item("C", "blocked", waiting=True), item("D", error="boom"),
        item("E", "done", landing="waiting"), item("F", "done"), item("G", "done", landing="failed", waiting=True),
        item("H", "review"))]
    assert kinds == ["queued", "running", "needs", "error", "landing", "done", "error", "running"]


def test_issue_state_kind_prefers_the_type_and_falls_back_to_the_name():
    assert call("issueStateKind", {"tracker_state": "Shipped", "tracker_state_kind": "done"}) == "done"
    assert [call("issueStateKind", {"tracker_state": n}) for n in ("Todo", "In Progress", "In Review", "Done", None)] == \
        ["todo", "progress", "review", "done", None]


def test_counts_label_leaves_out_zero_problems():
    assert call("countsLabel", {"issues": 8, "needs": 1, "errors": 1}) == "8 issues · 1 needs you · 1 error"
    assert call("countsLabel", {"issues": 1, "needs": 0, "errors": 2}) == "1 issue · 2 errors"


# -- grouping, done-folding and single-child skipping --------------------------------------------

def test_group_by_tracker_counts_and_source_tooltips():
    trackers = call("groupByTracker", [
        conn("core", items=[item("A", "blocked", waiting=True), item("B", error="x"), item("C", "done")]),
        conn("plat", items=[item("D")]),
        conn("j", tracker="jira", board="trm", items=[item("T-1")]),
    ])
    assert [t["key"] for t in trackers] == ["linear", "jira"]
    lin = trackers[0]
    assert lin["counts"] == {"issues": 4, "needs": 1, "errors": 1} and lin["tone"] == "bad"
    (board,) = lin["boards"]  # both linear connections feed board "b"
    assert board["inlineSources"] is True  # one board, several sources → shown inline
    assert "connection: core" in board["sourceTip"] and "connection: plat" in board["sourceTip"]
    assert trackers[1]["boards"][0]["inlineSources"] is False


def test_connection_errors_become_problems_with_a_fix():
    (t,) = call("groupByTracker", [conn("core", last_error="HttpError: linear: HTTP 401: not authorized; check your API key/token")])
    (p,) = t["boards"][0]["problems"]
    assert p["title"] == "Linear rejected the API key for core." and "LINEAR_API_KEY" in p["fix"]
    assert "401" in p["raw"]
    assert t["counts"]["errors"] == 1


def test_fold_done_keeps_order_and_separates_done():
    items = [item("A", "blocked", waiting=True), item("B"), item("C", "done"), item("D", "done", landing="waiting"),
             item("E", "done")]
    out = call("foldDone", items)
    assert [i["key"] for i in out["active"]] == ["A", "B", "D"]  # landing is not done yet
    assert [i["key"] for i in out["done"]] == ["C", "E"]
    # Only a few done items and nothing else: shown as they are, no "N done" row.
    only_done = call("foldDone", [item("C", "done"), item("E", "done")])
    assert [i["key"] for i in only_done["active"]] == ["C", "E"] and only_done["done"] == []
    many = call("foldDone", [item(f"D{n}", "done") for n in range(4)])
    assert many["active"] == [] and len(many["done"]) == 4


def test_pane_tree_skips_single_child_levels():
    one_tracker = call("paneTree", [conn("core", board="core"), conn("plat", board="plat")])
    assert [(n["type"], n["board"]["name"]) for n in one_tracker] == [("board", "core"), ("board", "plat")]

    two = call("paneTree", [conn("core", board="core"), conn("plat", board="plat"), conn("j", tracker="jira", board="trm")])
    assert [n["type"] for n in two] == ["tracker", "tracker"]
    assert "board" not in two[0]  # linear has two boards → its own header with boards under it
    assert two[1]["board"]["name"] == "trm"  # jira has one board → folded into the tracker row


# -- errors and staleness -------------------------------------------------------------------------

@pytest.mark.parametrize("raw,title", [
    ("linear: HTTP 401: not authorized; check your API key/token", "Linear rejected the API key for core."),
    ("RateLimited: linear: rate limited (HTTP 429); retry after 30s", "Linear is rate limiting for core."),
    ("URLError: <urlopen error [Errno 8] nodename nor servname provided>", "Can't reach Linear for core."),
    ("TimeoutError: timed out", "Can't reach Linear for core."),
    ("linear: no state 'Shipped' for CORE-1; valid states: Todo, Done", "Linear has no matching state for core."),
])
def test_error_mapping(raw, title):
    out = call("friendlyError", raw, {"tracker": "linear", "conn": "core"})
    assert out["title"] == title and out["raw"] == raw and out["fix"]


def test_unknown_errors_keep_their_first_line_and_raw_text():
    out = call("friendlyError", "something odd\nwith detail", {"tracker": "jira"})
    assert out["title"] == "something odd" and out["raw"] == "something odd\nwith detail"


def test_landing_failure_mapping():
    out = call("landingError", "gh: Pull request #108 is not mergeable: the merge commit cannot be cleanly created")
    assert out["title"] == "Landing failed: the PR has merge conflicts." and "Rebase" in out["fix"]
    assert call("landingError", "fatal: no remote")["title"] == "Landing failed."


def test_stale_detection():
    fresh = {"connections": [conn("core")]}
    assert call("staleness", fresh, NOW) == {"last": NOW - 10, "stale": False}
    old = {"connections": [conn("core", last_poll_at=NOW - 2 * 3600)]}
    assert call("staleness", old, NOW)["stale"] is True
    # Just under 3× the poll interval is still fine; the longest interval counts.
    edge = {"connections": [conn("a", last_poll_at=NOW - 170), conn("b", last_poll_at=NOW - 500, poll_interval_seconds=60)]}
    assert call("staleness", edge, NOW)["stale"] is False
    slow = {"connections": [conn("a", last_poll_at=NOW - 500, poll_interval_seconds=300)]}
    assert call("staleness", slow, NOW)["stale"] is False
    assert call("staleness", dict(old, enabled=False), NOW)["stale"] is False
    broken = {"connections": [conn("core", last_poll_at=NOW - 9999, running=False, error="unknown tracker")]}
    assert call("staleness", broken, NOW)["stale"] is False


# -- chip and card badges --------------------------------------------------------------------------

def test_chip_is_icon_only_when_healthy_and_words_for_problems():
    healthy = call("chipState", {"data": {"connections": [conn("core", items=[item("A")])]}}, NOW)
    assert healthy["label"] == "" and healthy["icon"] == "sync" and healthy["tone"] == "muted"

    busy = call("chipState", {"data": {"connections": [conn("core", items=[
        item("A", "blocked", waiting=True), item("B", error="x")])]}}, NOW)
    assert busy == {"tone": "bad", "icon": "alert", "label": "1 needs you · 1 error",
                    "title": "Issue Sync: 1 needs you, 1 error — click to open"}

    waiting = call("chipState", {"data": {"connections": [conn("core", items=[item("A", "blocked", waiting=True)])]}}, NOW)
    assert waiting["tone"] == "warn" and waiting["label"] == "1 needs you"

    stale = call("chipState", {"data": {"connections": [conn("core", last_poll_at=NOW - 7200)]}}, NOW)
    assert stale["tone"] == "warn" and "sync may have stopped" in stale["title"]


def test_chip_unreachable_empty_and_off():
    assert call("chipState", {"error": {"message": "502"}})["tone"] == "bad"
    assert call("chipState", {"isLoading": True})["tone"] == "muted"
    assert call("chipState", {"data": {"connections": []}})["label"] == "set up"
    assert call("chipState", {"data": {"enabled": False, "connections": [conn("c")]}})["label"] == "off"


def test_card_badges_use_the_shared_vocabulary():
    hit = lambda i: dict(i, tracker="linear", conn="core")  # noqa: E731
    assert call("cardBadge", hit(item("A", "blocked", waiting=True)))["tooltip"] == \
        "A: needs you. Answer on the issue in Linear."
    err = call("cardBadge", hit(item("B", error="HTTP 401: not authorized")))
    assert err["tone"] == "red" and err["tooltip"].startswith("B: error.")
    assert call("cardBadge", hit(item("C", "done", landing="running")))["tooltip"] == "C: landing"
    assert call("cardBadge", hit(item("D")))["tooltip"] == "Linear issue D"
    assert call("cardBadge", None) is None


# -- landing (CORE-642) and connectors -------------------------------------------------------------

def test_landing_info():
    assert call("landingInfo", {"card_status": "done", "landing": "waiting", "landing_message": "PR #101 set to auto-merge"}) == \
        {"marker": "landing", "tone": "warn", "line": "PR #101 set to auto-merge"}
    assert call("landingInfo", {"card_status": "done", "landing": "landed"}) is None
    assert call("landingInfo", {"card_status": "running", "landing": "waiting"}) is None
    assert "Last state Issue Sync pushed" in js("m.ISSUE_STATE_TOOLTIP")


def test_connector_styles_unchanged():
    mid = call("connectorStyles", "rounded", "linear", False)
    assert mid["rail"]["bottom"] == 0 and mid["elbow"]["width"] == 13 and "#5e6ad2" in mid["rail"]["borderLeft"]
    last = call("connectorStyles", "rounded", "linear", True)
    assert last["elbow"] is None and last["rail"]["borderBottomLeftRadius"] == 7 and last["rail"]["height"] == 13
    lines = call("connectorStyles", "lines", "linear", True)
    assert lines["rail"]["height"] == 12 and "#5e6ad2" not in lines["rail"]["borderLeft"]
    assert call("treeStyleOf", {"display": {"tree_style": "zigzag"}}) == "rounded"


# -- rendered tree ---------------------------------------------------------------------------------

def _texts(expr: str) -> str:
    """All strings and aria-labels in a rendered element tree, joined (stub jsx → plain objects)."""
    return js("""(() => { const out = []; const walk = n => {
        if (n == null || n === false) return; if (typeof n === 'string' || typeof n === 'number') { out.push(String(n)); return }
        if (Array.isArray(n)) { n.forEach(walk); return }
        if (typeof n.type === 'function') { walk(n.type(n.props)); return }
        const p = n.props || {}; for (const k of ['aria-label', 'data-tip', 'title']) if (p[k]) out.push('[' + k + ':' + p[k] + ']');
        walk(p.children) }; walk(""" + expr + """); return out.join(' ') })()""")


def test_rendered_pane_has_no_repeated_text_and_labels_every_icon():
    data = {"connections": [conn("core", board="typeanvil-core", items=[
        item("CORE-652", "blocked", waiting=True, question="Box or skip?", tracker_state="In Progress"),
        item("CORE-641", tracker_state="In Progress", tracker_state_kind="progress"),
        item("CORE-640", "done", tracker_state="Done"), item("CORE-637", "done", tracker_state="Done")])]}
    text = _texts("m.SyncView({ ctx: {}, data: " + json.dumps(data) + ", collapsed: new Set(), toggle() {} })")
    for gone in ("linked", "checked", "card:", "issue:", "waiting", "No synced issues", "via "):
        assert gone not in text
    assert "2 done" in text and "CORE-640" not in text  # done folded and closed by default
    assert "needs you" in text and "“Box or skip?”" in text
    assert "[aria-label:Issue: In Progress (last pushed)]" in text and "[aria-label:Card running]" in text
    assert "[aria-label:Open in Linear]" in text and "[aria-label:Open kanban card]" in text
    assert "Linear · synced " in text  # one footer, not a time per board


def test_rendered_empty_state_offers_the_setup_command():
    text = _texts("m.SyncView({ ctx: {}, data: { connections: [] }, collapsed: new Set(), toggle() {} })")
    assert "hermes issues setup" in text and "Copy" in text and "plugins.entries" not in text


def test_no_typed_chevrons_or_tailwind_amber():
    src = (REPO / "desktop" / "plugin.js").read_text(encoding="utf-8")
    assert "▲" not in src and "▼" not in src and "text-amber-600" not in src
    assert "#c99a06" in src and "#b8700b" in src  # light-mode status colours
