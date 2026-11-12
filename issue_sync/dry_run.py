"""Read-only preview of inbound admission and done-gate landing.

Creates nothing, moves nothing, comments nothing. Reads the board (``list_cards``)
and the tracker (``fetch_issues``, ``check``) only. ``prepare_command`` runs solely
when asked, and never as part of admission.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from .adapters.base import Issue
from .connections import Connection
from .engine import WOULD_ADMIT, SyncEngine
from .links import link_from_card
from .prepare import run_prepare

Clock = Callable[[], float]


def collect(
    engine: SyncEngine | None,
    conn: Connection | None,
    error: str | None = None,
    *,
    with_prepare: bool = False,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """One connection's dry-run payload. Never mutates the board or tracker."""
    now_fn = clock or (engine.clock if engine is not None else None)
    report = _header(engine, conn, error)
    if with_prepare:
        report["prepare"] = []
    if engine is None:
        return report

    try:
        engine.adapter.check()
    except Exception as exc:
        report["check_error"] = str(exc) or type(exc).__name__

    try:
        cards = engine.kanban.list_cards(archived=True)
    except Exception as exc:
        report["error"] = report["error"] or f"{type(exc).__name__}: {exc}"
        return report

    report["active"] = engine._owned_active(cards)
    try:
        plan = engine.plan_inbound(cards)
    except Exception as exc:
        report["error"] = report["error"] or f"{type(exc).__name__}: {exc}"
        return report

    for issue, decision in plan:
        report["issues"].append(_issue_row(issue, decision))
        if with_prepare and decision == WOULD_ADMIT:
            report["prepare"].append(_prepare_preview(engine, issue))

    now = now_fn() if now_fn is not None else 0.0
    for card in cards:
        link = link_from_card(card)
        if link is None or not engine.owns(link) or engine._is_review_card(card):
            continue
        if str(card.get("status") or "").casefold() != "done":
            continue
        report["landing"].append(landing_preview(engine, card, link, now))
    return report


def run(
    entries: Sequence[tuple[SyncEngine | None, Connection | None, str | None]],
    *,
    connection: str | None = None,
    with_prepare: bool = False,
    as_json: bool = False,
    clock: Clock | None = None,
) -> tuple[str, int]:
    """Render a dry-run for ``entries``. Exit 1 if any connection is misconfigured."""
    selected, select_error = _select(entries, connection)
    if select_error is not None:
        payload = {"ok": False, "error": select_error, "connections": []}
        text = json.dumps(payload, indent=2) if as_json else select_error
        return text, 1
    reports = [
        collect(engine, conn, error, with_prepare=with_prepare, clock=clock)
        for engine, conn, error in selected
    ]
    ok = bool(reports) and all(not r.get("error") and not r.get("check_error") for r in reports)
    payload = {"ok": ok, "connections": reports}
    text = json.dumps(payload, indent=2, default=_json_default) if as_json else render_text(reports)
    return text, 0 if ok else 1


def run_runners(
    runners: Iterable[Any],
    *,
    connection: str | None = None,
    with_prepare: bool = False,
    as_json: bool = False,
    clock: Clock | None = None,
) -> tuple[str, int]:
    entries = [(r.engine, r.conn, r.error) for r in runners]
    return run(entries, connection=connection, with_prepare=with_prepare, as_json=as_json, clock=clock)


def render_text(reports: Sequence[Mapping[str, Any]]) -> str:
    if not reports:
        return "issue-sync: nothing is configured."
    blocks = []
    for report in reports:
        name = report.get("name") or report.get("id") or report.get("tracker") or "default"
        body = "\n".join("  " + line for line in _connection_lines(report))
        blocks.append(f"connection {name}:\n{body}")
    return "\n".join(blocks)


def landing_preview(engine: SyncEngine, card: Mapping[str, Any], link: Any, now: float) -> dict[str, Any]:
    """Describe a done card's landing state without launching a land command or git/gh check."""
    raw = engine.state.link(link.id).get("landing")
    landing = raw if isinstance(raw, dict) else {}
    status = str(landing.get("status") or "") or "not started"
    error = landing.get("error")
    error_text = str(error) if error not in (None, "") else None
    next_at = _as_float(landing.get("next_try_at"))
    return {
        "key": link.key,
        "card_id": card.get("id"),
        "status": status,
        "next_try_at": next_at,
        "error": error_text,
        "next": _next_action(engine, link, status, next_at, now),
    }


def _header(engine: SyncEngine | None, conn: Connection | None, error: str | None) -> dict[str, Any]:
    settings = conn.settings if conn is not None else {}
    filt = settings.get("filter") if isinstance(settings.get("filter"), Mapping) else {}
    max_active = engine.max_active if engine is not None else None
    return {
        "id": conn.id if conn is not None else (engine.conn if engine is not None else ""),
        "name": conn.name if conn is not None else (
            (engine.conn if engine is not None else "") or "default"
        ),
        "tracker": (conn.tracker if conn is not None else "") or (
            engine.adapter.name if engine is not None else ""
        ),
        "projects": list(conn.projects) if conn is not None else list(
            (engine.filter or {}).get("projects") or []
        ) if engine is not None else [],
        "label": conn.label if conn is not None else str(filt.get("label") or (engine.filter or {}).get("label") or "agent"),
        "admit": engine.admit if engine is not None else str(settings.get("admit") or "label"),
        "board": ((conn.board if conn is not None else "") or "default"),
        "max_active": max_active,
        "active": 0,
        "error": error,
        "check_error": None,
        "issues": [],
        "landing": [],
    }


def _issue_row(issue: Issue, decision: str) -> dict[str, Any]:
    return {"key": issue.key, "title": issue.title, "url": issue.url, "decision": decision}


def _prepare_preview(engine: SyncEngine, issue: Issue) -> dict[str, Any]:
    if not engine.prepare_command:
        return {"key": issue.key, "result": None, "error": "no prepare_command"}
    payload = {
        "connection": engine.conn, "tracker": engine.adapter.name, "key": issue.key,
        "title": issue.title, "url": issue.url, "body": issue.body,
        "labels": list(issue.labels), "priority": issue.priority,
        "estimate": issue.estimate, "created_at": issue.created_at, "state": issue.state,
    }
    kwargs: dict[str, Any] = {}
    if engine._prepare_runner is not None:
        kwargs["runner"] = engine._prepare_runner
    try:
        prepared = run_prepare(
            engine.prepare_command, payload, timeout=engine.prepare_timeout, **kwargs,
        )
    except Exception as exc:
        return {"key": issue.key, "result": None, "error": str(exc) or type(exc).__name__}
    if prepared.skip:
        result: dict[str, Any] = {"skip": True}
        if prepared.reason:
            result["reason"] = prepared.reason
        return {"key": issue.key, "result": result, "error": None}
    result = {}
    for field in ("title", "body", "assignee", "workspace", "branch", "model", "provider"):
        value = getattr(prepared, field)
        if value is not None:
            result[field] = value
    if prepared.skills:
        result["skills"] = list(prepared.skills)
    return {"key": issue.key, "result": result, "error": None}


def _next_action(engine: SyncEngine, link: Any, status: str, next_at: float | None, now: float) -> str:
    gate = engine.done_gate
    if status == "landed" or gate == "immediate":
        return "push Done and post the summary"
    if status == "running":
        return "wait for the land command to finish"
    if status == "failed":
        return "wait for a human reply on the issue, then retry"
    if status == "waiting":
        if next_at is not None:
            when = "now" if next_at <= now else _iso(next_at)
            return f"retry at {when}"
        return "retry"
    if gate == "command":
        return "run land_command"
    if gate == "merged":
        review_id = engine.state.link(link.id).get("review_card_id")
        if engine.reviewer and not review_id:
            return f"create a review card for {engine.reviewer}"
        if engine.reviewer and review_id:
            return "wait for the review card to complete, then check merged_base"
        base = engine.merged_base or "the merged base"
        return f"check whether the branch has reached {base}"
    return "push Done and post the summary"


def _connection_lines(report: Mapping[str, Any]) -> list[str]:
    lines = [f"tracker: {report.get('tracker') or '?'}"]
    projects = report.get("projects") or []
    if projects:
        lines.append("projects: " + ", ".join(str(p) for p in projects))
    lines.append(f"label: {report.get('label') or 'agent'}")
    lines.append(f"admit: {report.get('admit') or 'label'}")
    lines.append(f"board: {report.get('board') or 'default'}")
    cap = report.get("max_active")
    cap_text = "unlimited" if cap is None else str(cap)
    lines.append(f"max_active: {cap_text} ({report.get('active') or 0} active)")
    if report.get("error"):
        lines.append(f"error: {report['error']}")
    check_error = report.get("check_error")
    lines.append(f"check: {check_error}" if check_error else "check: ok")
    issues = report.get("issues") or []
    if not issues:
        lines.append("issues: none")
    else:
        lines.append("issues:")
        for row in issues:
            title = str(row.get("title") or "").strip()
            title_bit = f"  {title}" if title and title != row.get("key") else ""
            lines.append(f"  {row.get('key')}{title_bit}  {row.get('decision')}")
    prepare = report.get("prepare")
    if prepare:
        lines.append("prepare:")
        for row in prepare:
            if row.get("error"):
                lines.append(f"  {row.get('key')}: error: {row['error']}")
            else:
                lines.append(f"  {row.get('key')}: {json.dumps(row.get('result'), default=_json_default)}")
    landing = report.get("landing") or []
    if not landing:
        lines.append("landing: none")
    else:
        lines.append("landing:")
        for row in landing:
            extra = _landing_extra(row)
            lines.append(
                f"  {row.get('key')}  {row.get('card_id') or '?'}  {row.get('status')}{extra}"
            )
            if row.get("error"):
                lines.append(f"    error: {_tail(str(row['error']))}")
            if row.get("next"):
                lines.append(f"    next: {row['next']}")
    return lines


def _landing_extra(row: Mapping[str, Any]) -> str:
    status = row.get("status")
    if status == "waiting" and row.get("next_try_at") is not None:
        return f"  next try {_iso(row['next_try_at'])}"
    if status == "failed" and row.get("error"):
        return f"  {_tail(str(row['error']))}"
    return ""


def _select(
    entries: Sequence[tuple[SyncEngine | None, Connection | None, str | None]],
    connection: str | None,
) -> tuple[list[tuple[SyncEngine | None, Connection | None, str | None]], str | None]:
    if not connection:
        return list(entries), None
    needle = connection.strip().lower()
    selected = []
    for engine, conn, error in entries:
        names = []
        if conn is not None:
            names.extend([conn.id, conn.name, conn.tracker])
        if engine is not None:
            names.append(engine.conn)
        if any(str(name).strip().lower() == needle for name in names if name):
            selected.append((engine, conn, error))
    if not selected:
        return [], f"issue-sync: no connection named {connection!r}"
    return selected, None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="seconds")


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _tail(text: str, limit: int = 120) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, tuple)):
        return list(value)
    return str(value)
