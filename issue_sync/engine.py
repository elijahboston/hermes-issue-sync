"""The reconciler: tracker issues -> kanban cards (inbound), card status -> tracker state (outbound).

Pure logic over three seams: a ``TrackerAdapter``, a kanban client (``KanbanClient`` or
``FakeKanban``), and ``SyncState``. Which card belongs to which issue is read from the card
marker (``links``); state only remembers what was already pushed so calls are not repeated.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .adapters.base import Issue, RateLimited, TrackerAdapter
from .land_runner import launch_argv, read_result
from .links import (Link, asked_note, build_marker, card_body, card_title, done_note, has_done_note,
                    has_note, idempotency_key, link_from_card, parse_marker)
from .prepare import PrepareError, run_prepare
from .signature import sign, strip_signature
from .state import SyncState

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 60.0
MIN_BACKOFF = 1.0
# Kanban statuses a user may map to tracker states via ``state_map``.
MAPPABLE_STATUSES = frozenset({"ready", "running", "blocked", "review", "done"})
# Cards in these statuses count toward ``max_active``.
ACTIVE_STATUSES = frozenset({"todo", "ready", "running", "review", "scheduled"})


def _poll_interval(value: Any) -> float:
    if isinstance(value, bool):
        return DEFAULT_POLL_INTERVAL
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_POLL_INTERVAL
    return seconds if math.isfinite(seconds) and seconds > 0 else DEFAULT_POLL_INTERVAL


def _state_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    mapped = {
        status: target.strip() for status, target in value.items()
        if status in MAPPABLE_STATUSES and isinstance(target, str) and target.strip()
    }
    ignored = sorted(str(status) for status in value if status not in mapped)
    if ignored:
        logger.warning("issue-sync: ignoring state_map entries for %s (keys must be among %s with a "
                       "non-empty tracker state)", ", ".join(ignored), ", ".join(sorted(MAPPABLE_STATUSES)))
    return mapped


def _error_text(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def _admit(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in ("label", "all") else "label"


DEFAULT_MAX_ACTIVE = 1


def _max_active(value: Any) -> int | None:
    """Cap on active cards. Unset or invalid -> 1, so a fresh install never floods a board;
    an explicit 0 (or negative) opts out to unlimited."""
    if isinstance(value, bool) or value in (None, ""):
        return DEFAULT_MAX_ACTIVE
    try:
        n = int(value)
    except (TypeError, ValueError):
        logger.warning("issue-sync: max_active %r is not a number; using %d", value, DEFAULT_MAX_ACTIVE)
        return DEFAULT_MAX_ACTIVE
    return n if n >= 1 else None


def _order(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in ("priority", "created") else "priority"


def _as_bool(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(value)


def _issue_sort_key(issue: Issue, order: str) -> tuple:
    created = issue.created_at or "\uffff"
    if order == "created":
        return (created,)
    # Urgent(1)..Low(4), then no priority (0) last; smaller estimate, then oldest created.
    rank = issue.priority if issue.priority else 10**9
    estimate = issue.estimate if issue.estimate is not None else float("inf")
    return (rank, estimate, created)


DEFAULT_PREPARE_TIMEOUT = 120.0


def _prepare_command(value: Any) -> str | list[str] | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        argv = [str(part) for part in value]
        return argv or None
    logger.warning("issue-sync: prepare_command must be a string or list; ignoring it")
    return None


def _prepare_timeout(value: Any) -> float:
    if isinstance(value, bool) or value in (None, ""):
        return DEFAULT_PREPARE_TIMEOUT
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        logger.warning("issue-sync: prepare_timeout_seconds %r is not a number; using %g",
                       value, DEFAULT_PREPARE_TIMEOUT)
        return DEFAULT_PREPARE_TIMEOUT
    return seconds if math.isfinite(seconds) and seconds > 0 else DEFAULT_PREPARE_TIMEOUT


DONE_GATES = frozenset({"immediate", "merged", "command"})
DEFAULT_DONE_GATE = "immediate"
DEFAULT_LAND_TIMEOUT = 1800.0
DEFAULT_LAND_RETRY = 300.0
LAND_TIMEOUT_GRACE = 60.0
LAND_COMMENT_LIMIT = 1500
LAND_CMD_TIMEOUT = 60.0


def _done_gate(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in DONE_GATES else DEFAULT_DONE_GATE


def _land_command(value: Any) -> str | list[str] | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        argv = [str(part) for part in value]
        return argv or None
    logger.warning("issue-sync: land_command must be a string or list; ignoring it")
    return None


def _seconds(value: Any, default: float, setting: str) -> float:
    if isinstance(value, bool) or value in (None, ""):
        return default
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        logger.warning("issue-sync: %s %r is not a number; using %g", setting, value, default)
        return default
    return seconds if math.isfinite(seconds) and seconds > 0 else default


def _land_failure_body(tail: str) -> str:
    clip = (tail or "")[-LAND_COMMENT_LIMIT:]
    quoted = ""
    if clip.strip():
        quoted = "\n".join(f"> {line}" for line in clip.splitlines()) + "\n\n"
    return ("Hermes finished the work but could not land it:\n\n"
            + quoted
            + "Reply here when it's fixed and Hermes will try again.")


def _prepared_card_body(tracker: str, key: str, url: str, conn: str, body: str) -> str:
    """Marker on line 1, then a blank line, then the hook body (a leading marker stripped)."""
    marker = build_marker(tracker, key, url, conn)
    lines = body.splitlines()
    if lines and parse_marker(lines[0]) is not None:
        lines = lines[1:]
    rest = "\n".join(lines)
    if rest.strip():
        return f"{marker}\n\n{rest}\n"
    return f"{marker}\n"


class SyncEngine:
    def __init__(self, adapter: TrackerAdapter, kanban: Any, state: SyncState,
                 settings: Mapping[str, Any] | None, clock: Callable[[], float] = time.time,
                 *, conn: str = "", owns_legacy: bool = False,
                 prepare_runner: Callable[..., Any] | None = None,
                 land_launcher: Callable[..., Any] | None = None,
                 git_runner: Callable[..., Any] | None = None,
                 land_dir: str | Path | None = None,
                 which: Callable[[str], str | None] | None = None) -> None:
        settings = settings if isinstance(settings, Mapping) else {}
        self.adapter = adapter
        # Connection id written into new card markers; "" for the single-tracker setup.
        self.conn = conn
        # Also handle cards made before connections existed (markers without ``conn=``).
        self.owns_legacy = owns_legacy or not conn
        self.kanban = kanban
        self.state = state
        self.clock = clock
        filter_ = settings.get("filter")
        self.filter: dict[str, Any] = dict(filter_) if isinstance(filter_, Mapping) else {}
        self.state_map = _state_map(settings.get("state_map"))
        assignee = settings.get("assignee")
        self.assignee: str | None = (assignee.strip() or None) if isinstance(assignee, str) else None
        self.poll_interval = _poll_interval(settings.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL))
        self.admit = _admit(settings.get("admit"))
        self.max_active = _max_active(settings.get("max_active"))
        self.order = _order(settings.get("order"))
        self.leaf_only = _as_bool(settings.get("leaf_only"), False)
        self.skip_blocked = _as_bool(settings.get("skip_blocked"), True)
        exclude = settings.get("exclude_label")
        self.exclude_label = exclude.strip() if isinstance(exclude, str) else ""
        self.prepare_command = _prepare_command(settings.get("prepare_command"))
        self.prepare_timeout = _prepare_timeout(settings.get("prepare_timeout_seconds"))
        self._prepare_runner = prepare_runner
        self.done_gate = _done_gate(settings.get("done_gate"))
        self.land_command = _land_command(settings.get("land_command"))
        self.land_timeout = _seconds(settings.get("land_timeout_seconds"), DEFAULT_LAND_TIMEOUT,
                                    "land_timeout_seconds")
        self.land_retry = _seconds(settings.get("land_retry_seconds"), DEFAULT_LAND_RETRY,
                                  "land_retry_seconds")
        self.merged_base = str(settings.get("merged_base") or "").strip()
        self._land_launcher = land_launcher
        self._git_runner = git_runner
        self._land_dir = Path(land_dir) if land_dir is not None else None
        self._which = which or shutil.which
        # Why the last inbound pass skipped each issue key (for a later dry-run command).
        self.last_skips: dict[str, str] = {}
        # Landing status per issue key (for a later dry-run command).
        self.last_landing: dict[str, str] = {}
        # Cards of this connection seen by the last full pass; the sync pane reads them.
        self.last_cards: list[dict[str, Any]] = []
        # Hooks and the dispatcher tick may race; a busy engine skips rather than queueing work.
        self._lock = threading.Lock()

    def owns(self, link: Link | None) -> bool:
        """Whether a card's link belongs to this engine's connection."""
        if link is None or link.tracker != self.adapter.name:
            return False
        return link.conn == self.conn or (not link.conn and self.owns_legacy)

    # ── scheduling ────────────────────────────────────────────────────────

    def due(self) -> bool:
        """The poll interval has elapsed and no tracker back-off is in force."""
        now = self.clock()
        backoff_until = self.state.backoff_until
        if backoff_until is not None and now < backoff_until:
            return False
        last = self.state.last_poll_at
        # A last poll "in the future" means the clock moved backwards: poll rather than stall.
        return last is None or last > now or now - last >= self.poll_interval

    def tick(self, *, force: bool = False) -> bool:
        """Run one full sync pass if due (or ``force``). Returns whether a pass ran; never raises."""
        try:
            if not force and not self.due():
                return False
            if not self._lock.acquire(blocking=False):
                return False
        except Exception:
            logger.exception("issue-sync: could not schedule a sync pass")
            return False
        try:
            self._run_pass()
        except Exception:
            logger.exception("issue-sync: sync pass failed unexpectedly")
        finally:
            self._lock.release()
        return True

    def _run_pass(self) -> None:
        self.state.last_poll_at = self.clock()
        errors: list[str] = []
        try:
            # One listing serves both directions. It includes archived cards so they are never
            # recreated; outbound ignores them (``archived`` is not mappable). Cards created by
            # this pass are pushed on the next one.
            cards = self.kanban.list_cards(archived=True)
            self.last_cards = [c for c in cards if self.owns(link_from_card(c))]
            # Phases are independent: a broken tracker filter must not stop completion summaries.
            self._phase("inbound", errors, self._poll_inbound, cards)
            self._phase("outbound", errors, self._push_outbound, cards)
            self._phase("blocking", errors, self.sync_block_questions, cards)
        except RateLimited as exc:
            delay = max(exc.retry_after, MIN_BACKOFF)
            self.state.backoff_until = self.clock() + delay
            errors.append(f"{type(exc).__name__}: {_error_text(exc)}")
            logger.warning("issue-sync: %s rate limited the sync; backing off %gs", self.adapter.name, delay)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {_error_text(exc)}")
            logger.warning("issue-sync: sync pass failed: %s", errors[-1])
        else:
            if self.state.backoff_until is not None:
                self.state.backoff_until = None
        self.state.last_error = "; ".join(errors) or None

    @staticmethod
    def _phase(name: str, errors: list[str], run: Callable[..., Any], *args: Any) -> None:
        """Run one sync phase, recording (not raising) its failure; ``RateLimited`` aborts the pass."""
        try:
            run(*args)
        except RateLimited:
            raise
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {_error_text(exc)}")
            logger.warning("issue-sync: %s sync failed: %s", name, errors[-1])

    # ── inbound: tracker issues -> kanban cards ───────────────────────────

    def poll_inbound(self) -> int:
        """Create a card for every filtered issue without one. Returns cards created (0 if busy)."""
        if not self._lock.acquire(blocking=False):
            return 0
        try:
            return self._poll_inbound(self.kanban.list_cards(archived=True))
        finally:
            self._lock.release()

    def _poll_inbound(self, cards: Iterable[Mapping[str, Any]]) -> int:
        tracker = self.adapter.name
        cards = list(cards)
        linked = {link.key for link in map(link_from_card, cards) if self.owns(link)}
        active = sum(
            1 for card in cards
            if self.owns(link_from_card(card))
            and str(card.get("status") or "").casefold() in ACTIVE_STATUSES
        )
        skips: dict[str, str] = {}
        candidates: list[Issue] = []
        for issue in self.adapter.fetch_issues(self.filter):
            if issue.closed:
                skips[issue.key] = "closed"
                continue
            if issue.key in linked:
                skips[issue.key] = "linked"
                continue
            if self._excluded(issue):
                skips[issue.key] = "excluded"
                continue
            if self.leaf_only and issue.open_children:
                skips[issue.key] = "parent"
                continue
            if self.skip_blocked and issue.open_blockers:
                skips[issue.key] = "blocked"
                continue
            candidates.append(issue)
        candidates.sort(key=lambda issue: _issue_sort_key(issue, self.order))
        remaining = None if self.max_active is None else max(0, self.max_active - active)
        created = 0
        for issue in candidates:
            if remaining is not None and created >= remaining:
                skips[issue.key] = "cap"
                continue
            link = Link(tracker, issue.key, issue.url, self.conn)
            try:
                title, body, assignee, extra = self._shape_card(issue)
                if extra is None:
                    skips[issue.key] = "prepare-skip"
                    continue
                card_id = self.kanban.create_card(
                    title, body,
                    idempotency_key=idempotency_key(tracker, issue.key, self.conn),
                    assignee=assignee, **extra,
                )
            except RateLimited:
                self.last_skips = skips
                raise
            except Exception as exc:
                if isinstance(exc, PrepareError):
                    skips[issue.key] = "prepare-failed"
                self.state.update_link(link.id, last_error=_error_text(exc))
                logger.warning("issue-sync: could not create a card for %s: %s", link.id, _error_text(exc))
                continue
            self.state.update_link(link.id, card_id=card_id, last_pushed_state=issue.state, last_error=None)
            linked.add(issue.key)
            created += 1
        self.last_skips = skips
        return created


    def _shape_card(self, issue: Issue) -> tuple[str, str, str | None, dict[str, Any] | None]:
        """Title, body, assignee, create_card extras. extras is None when the prepare hook skips."""
        tracker = self.adapter.name
        title = card_title(issue.key, issue.title)
        body = card_body(tracker, issue.key, issue.url, issue.body, self.conn)
        assignee = self.assignee
        extra: dict[str, Any] = {}
        if not self.prepare_command:
            return title, body, assignee, extra
        kwargs: dict[str, Any] = {}
        if self._prepare_runner is not None:
            kwargs["runner"] = self._prepare_runner
        prepared = run_prepare(
            self.prepare_command,
            {
                "connection": self.conn, "tracker": tracker, "key": issue.key,
                "title": issue.title, "url": issue.url, "body": issue.body,
                "labels": list(issue.labels), "priority": issue.priority,
                "estimate": issue.estimate, "created_at": issue.created_at, "state": issue.state,
            },
            timeout=self.prepare_timeout, **kwargs,
        )
        if prepared.skip:
            return title, body, assignee, None
        if prepared.title is not None:
            title = prepared.title
        if prepared.body is not None:
            body = _prepared_card_body(tracker, issue.key, issue.url, self.conn, prepared.body)
        if prepared.assignee is not None:
            assignee = prepared.assignee.strip() or None
        if prepared.workspace:
            extra["workspace"] = prepared.workspace
        if prepared.branch:
            extra["branch"] = prepared.branch
        if prepared.skills:
            extra["skills"] = prepared.skills
        if prepared.model:
            extra["model"] = prepared.model
            if prepared.provider:
                extra["provider"] = prepared.provider
        return title, body, assignee, extra

    def _excluded(self, issue: Issue) -> bool:
        needle = self.exclude_label.casefold()
        if not needle:
            return False
        return any(str(name).casefold() == needle for name in (*issue.labels, *issue.ancestor_labels))

    # ── outbound: card status -> tracker state ────────────────────────────

    def push_outbound(self, task_id: str | None = None) -> int:
        """Push mapped card statuses to the tracker. ``task_id`` limits it to that card.

        Returns tracker moves made (0 if the engine is busy). Per-link failures are recorded
        on the link; ``RateLimited`` and kanban listing/show failures propagate.
        """
        if not self._lock.acquire(blocking=False):
            return 0
        try:
            if task_id is None:
                return self._push_outbound(self.kanban.list_cards())
            show = self.kanban.show_card(task_id)
            moved = self._push_card(show["task"], show)
            if show["task"].get("status") == "blocked":
                self._sync_block_card(show["task"], show)
            return moved
        finally:
            self._lock.release()

    def _push_outbound(self, cards: Iterable[Mapping[str, Any]]) -> int:
        return sum(self._push_card(card) for card in cards)

    def _push_card(self, card: Mapping[str, Any], show: Mapping[str, Any] | None = None) -> int:
        link = link_from_card(card)
        if link is None or not self.owns(link):
            return 0
        status = card.get("status")
        if (
            status == "done"
            and self.done_gate in ("merged", "command")
            and (self.state.link(link.id).get("landing") or {}).get("status") != "landed"
        ):
            try:
                if not self._progress_landing(link, card, show):
                    return 0
            except RateLimited:
                raise
            except Exception as exc:
                self.state.update_link(link.id, card_id=card.get("id"), last_error=_error_text(exc))
                logger.warning("issue-sync: landing failed for %s: %s", link.id, _error_text(exc))
                return 0
        target = self.state_map.get(status) if isinstance(status, str) else None
        if target is None:
            return 0
        card_id = card.get("id")
        entry = self.state.link(link.id)
        errors: list[str] = []
        moved = 0
        if target != entry.get("last_pushed_state"):
            try:
                self.adapter.move_issue(link.key, target)
            except RateLimited:
                raise
            except Exception as exc:
                errors.append(_error_text(exc))
            else:
                self.state.update_link(link.id, last_pushed_state=target)
                moved = 1
        # The summary is posted even when the move failed: a workflow that refuses the
        # transition must not swallow the completion report.
        if status == "done" and not entry.get("done_comment_posted"):
            try:
                self._post_done_summary(link, card_id, show)
            except RateLimited:
                raise
            except Exception as exc:
                errors.append(_error_text(exc))
        last_error = "; ".join(errors) or None
        self.state.update_link(link.id, card_id=card_id, last_error=last_error)
        if last_error:
            logger.warning("issue-sync: outbound sync failed for %s: %s", link.id, last_error)
        return moved

    def _post_done_summary(self, link: Link, card_id: Any, show: Mapping[str, Any] | None) -> None:
        if show is None:
            show = self.kanban.show_card(card_id)
        # The note on the card covers processes with different plugin state (worker vs dispatcher).
        if not has_done_note(show.get("comments"), link):
            summary = show.get("latest_summary")
            text = summary.strip() if isinstance(summary, str) else ""
            self.adapter.post_comment(link.key, sign(text or f"Completed by Hermes kanban card {card_id}"))
            # Recorded before the card note so a failing note never causes a second post here.
            self.state.update_link(link.id, done_comment_posted=True)
            self.kanban.comment(card_id, done_note(link))
        else:
            self.state.update_link(link.id, done_comment_posted=True)

    # ── done gate / background landing (CORE-633) ─────────────────────────

    def _progress_landing(self, link: Link, card: Mapping[str, Any],
                         show: Mapping[str, Any] | None) -> bool:
        """Advance landing for a done card. True when it has landed (caller then pushes Done)."""
        raw = self.state.link(link.id).get("landing")
        landing = dict(raw) if isinstance(raw, dict) else {}
        status = landing.get("status")
        now = self.clock()
        card_id = card.get("id")

        if status == "landed":
            self.last_landing[link.key] = "landed"
            return True

        if status == "failed":
            if not self._requeue_land_on_reply(link, card_id, landing):
                self.last_landing[link.key] = "failed"
                return False
            status = landing.get("status")

        if status == "running":
            outcome = self._land_outcome(landing, now)
            if outcome is None:
                self.last_landing[link.key] = "running"
                return False
            return self._apply_land_exit(link, card, landing, outcome)

        next_try = landing.get("next_try_at")
        try:
            next_at = float(next_try) if next_try is not None else None
        except (TypeError, ValueError):
            next_at = None
        if next_at is not None and now < next_at:
            self.last_landing[link.key] = status or "waiting"
            return False

        if self.done_gate == "merged":
            return self._check_merged(link, card, show, landing)
        return self._launch_land(link, card, show, landing)

    def _land_outcome(self, landing: Mapping[str, Any], now: float) -> dict[str, Any] | None:
        path = landing.get("result_file")
        if path:
            data = read_result(path)
            if data is not None:
                return data
            if Path(path).is_file():
                return {"exit": 2, "stdout_tail": "", "stderr_tail": "invalid land result file"}
        started = landing.get("started_at")
        try:
            started_at = float(started) if started is not None else None
        except (TypeError, ValueError):
            started_at = None
        if started_at is not None and now - started_at >= self.land_timeout + LAND_TIMEOUT_GRACE:
            return {"exit": 2, "stdout_tail": "", "stderr_tail": "land command timed out"}
        return None

    def _apply_land_exit(self, link: Link, card: Mapping[str, Any], landing: dict[str, Any],
                        result: Mapping[str, Any]) -> bool:
        try:
            code = int(result.get("exit"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            code = 2
        stdout = str(result.get("stdout_tail") or "")
        stderr = str(result.get("stderr_tail") or "")
        if code == 0:
            landing["status"] = "landed"
            landing.pop("error", None)
            self.state.update_link(link.id, landing=landing, last_error=None)
            self.last_landing[link.key] = "landed"
            return True
        if code == 1:
            landing["status"] = "waiting"
            landing["next_try_at"] = self.clock() + self.land_retry
            landing.pop("error", None)
            self.state.update_link(link.id, landing=landing)
            self.last_landing[link.key] = "waiting"
            return False
        tail = stdout if stdout.strip() else stderr
        landing["status"] = "failed"
        landing["error"] = tail[-LAND_COMMENT_LIMIT:]
        self.state.update_link(link.id, landing=landing)
        self.last_landing[link.key] = "failed"
        self._post_land_failure(link, card.get("id"), landing, tail)
        return False

    def _post_land_failure(self, link: Link, card_id: Any, landing: dict[str, Any], tail: str) -> None:
        if landing.get("failure_posted_attempt") == landing.get("attempt"):
            return
        self.adapter.post_comment(link.key, sign(_land_failure_body(tail)))
        _, cursor = self.adapter.list_comments_since(link.key, self.state.link(link.id).get("comment_cursor"))
        landing["failure_posted_attempt"] = landing.get("attempt")
        self.state.update_link(link.id, landing=landing, comment_cursor=cursor, last_error=None)

    def _requeue_land_on_reply(self, link: Link, card_id: Any, landing: dict[str, Any]) -> bool:
        entry = self.state.link(link.id)
        comments, new_cursor = self.adapter.list_comments_since(link.key, entry.get("comment_cursor"))
        answers = [c for c in comments if not c.is_bot and c.body.strip()]
        if not answers:
            if new_cursor != entry.get("comment_cursor"):
                self.state.update_link(link.id, comment_cursor=new_cursor)
            return False
        text = "\n\n".join(
            f"{c.author or 'someone'}: {strip_signature(c.body).strip()}" for c in answers
        )
        if card_id:
            self.kanban.comment(card_id, text)
        landing["status"] = "waiting"
        landing["next_try_at"] = self.clock()
        landing.pop("error", None)
        self.state.update_link(link.id, landing=landing, comment_cursor=new_cursor, last_error=None)
        self.last_landing[link.key] = "waiting"
        return True

    def _launch_land(self, link: Link, card: Mapping[str, Any], show: Mapping[str, Any] | None,
                    landing: dict[str, Any]) -> bool:
        if not self.land_command:
            return self._apply_land_exit(
                link, card, landing,
                {"exit": 2, "stdout_tail": "", "stderr_tail": "done_gate: command requires land_command"},
            )
        show = self._ensure_show(card, show)
        task = show.get("task") or dict(card)
        card_id = card.get("id") or task.get("id")
        try:
            attempt = int(landing.get("attempt") or 0) + 1
        except (TypeError, ValueError):
            attempt = 1
        folder = self._land_root() / "land" / (self.conn or "_")
        folder.mkdir(parents=True, exist_ok=True)
        result_file = folder / f"{card_id}-{attempt}.json"
        summary_file = folder / f"{card_id}-{attempt}.summary"
        summary = show.get("latest_summary")
        summary_file.write_text(summary if isinstance(summary, str) else "", encoding="utf-8")
        argv = launch_argv(result_file, self.land_command, self.land_timeout)
        env = os.environ.copy()
        env["ISSUE_SYNC_CONNECTION"] = str(self.conn or "")
        env["ISSUE_KEY"] = link.key
        env["ISSUE_URL"] = link.url or ""
        env["CARD_ID"] = str(card_id or "")
        env["WORKTREE"] = str(task.get("workspace_path") or card.get("workspace_path") or "")
        env["BRANCH"] = str(task.get("branch_name") or card.get("branch_name") or "")
        env["SUMMARY_FILE"] = str(summary_file)
        landing.update({
            "attempt": attempt,
            "started_at": self.clock(),
            "result_file": str(result_file),
            "status": "running",
            "next_try_at": None,
        })
        landing.pop("error", None)
        self.state.update_link(link.id, landing=landing, card_id=card_id)
        try:
            self._spawn_land(argv, env)
        except FileNotFoundError:
            return self._apply_land_exit(
                link, card, landing, {"exit": 2, "stdout_tail": "", "stderr_tail": f"{argv[0]!r} not found"},
            )
        except OSError as exc:
            return self._apply_land_exit(
                link, card, landing, {"exit": 2, "stdout_tail": "", "stderr_tail": _error_text(exc)},
            )
        self.last_landing[link.key] = "running"
        return False

    def _spawn_land(self, argv: list[str], env: dict[str, str]) -> None:
        launcher = self._land_launcher or subprocess.Popen
        launcher(
            argv,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=env,
        )

    def _check_merged(self, link: Link, card: Mapping[str, Any], show: Mapping[str, Any] | None,
                     landing: dict[str, Any]) -> bool:
        worktree = str(card.get("workspace_path") or "")
        branch = str(card.get("branch_name") or "")
        if not worktree or not branch:
            show = self._ensure_show(card, show)
            task = show.get("task") or {}
            worktree = worktree or str(task.get("workspace_path") or "")
            branch = branch or str(task.get("branch_name") or "")
        base = self.merged_base
        landed = False
        if worktree and branch and base:
            try:
                self._run_cmd(["git", "fetch", "origin"], cwd=worktree)
                proc = self._run_cmd(
                    ["git", "merge-base", "--is-ancestor", branch, f"origin/{base}"], cwd=worktree,
                )
                if getattr(proc, "returncode", 1) == 0:
                    landed = True
            except Exception:
                landed = False
            if not landed and self._which("gh"):
                try:
                    proc = self._run_cmd(
                        ["gh", "pr", "view", branch, "--json", "state", "-q", ".state"], cwd=worktree,
                    )
                    state = (getattr(proc, "stdout", None) or "").strip()
                    if getattr(proc, "returncode", 1) == 0 and state == "MERGED":
                        landed = True
                except Exception:
                    pass
        if landed:
            landing["status"] = "landed"
            landing.pop("error", None)
            self.state.update_link(link.id, landing=landing, last_error=None)
            self.last_landing[link.key] = "landed"
            return True
        landing["status"] = "waiting"
        landing["next_try_at"] = self.clock() + self.land_retry
        self.state.update_link(link.id, landing=landing)
        self.last_landing[link.key] = "waiting"
        return False

    def _run_cmd(self, argv: list[str], *, cwd: str) -> Any:
        runner = self._git_runner or subprocess.run
        return runner(
            argv, cwd=cwd, capture_output=True, text=True, check=False, shell=False,
            timeout=LAND_CMD_TIMEOUT,
        )

    def _ensure_show(self, card: Mapping[str, Any], show: Mapping[str, Any] | None) -> Mapping[str, Any]:
        if show is not None:
            return show
        card_id = card.get("id")
        if not card_id:
            return {"task": dict(card), "latest_summary": None, "comments": []}
        return self.kanban.show_card(card_id)

    def _land_root(self) -> Path:
        if self._land_dir is not None:
            return self._land_dir
        from .pane import data_dir
        folder = data_dir()
        return folder if folder is not None else Path(tempfile.gettempdir()) / "hermes-issue-sync"


    # ── blocking questions (CORE-605) ─────────────────────────────────────

    def sync_block_questions(self, cards: Iterable[Mapping[str, Any]] | None = None) -> int:
        """Two-way blocking. Returns questions posted + answers relayed.

        A blocked card's question (the reason of its latest ``blocked`` event) is posted once as a
        signed comment on its tracker issue. The first human comments after that question are
        relayed back with ``kanban unblock``, so the worker resumes with the answer. Comments the
        plugin wrote are recognised by their signature, not their author (the plugin posts with
        the user's own key).
        """
        if cards is None:
            cards = self.kanban.list_cards(status="blocked")
        return sum(self._sync_block_card(card) for card in cards if card.get("status") == "blocked")

    @staticmethod
    def _latest_block(show: Mapping[str, Any]) -> tuple[str, str] | None:
        blocks = [e for e in show.get("events") or [] if isinstance(e, Mapping) and e.get("kind") == "blocked"]
        if not blocks:
            return None
        latest = max(blocks, key=lambda e: e.get("created_at") or 0)
        reason = str((latest.get("payload") or {}).get("reason") or "").strip()
        return f"{latest.get('created_at')}:{latest.get('run_id')}", reason

    def _sync_block_card(self, card: Mapping[str, Any], show: Mapping[str, Any] | None = None) -> int:
        link = link_from_card(card)
        if link is None or not self.owns(link):
            return 0
        card_id = card.get("id")
        try:
            show = show if show is not None else self.kanban.show_card(card_id)
            block = self._latest_block(show)
            if block is None:
                return 0
            block_id, reason = block
            entry = self.state.link(link.id)
            if entry.get("answered_block") == block_id:
                return 0  # answered; waiting for the unblock to show up on the board
            note = asked_note(link, block_id)
            if entry.get("asked_block") != block_id and not has_note(show.get("comments"), note):
                return self._ask(link, card_id, block_id, reason, entry.get("comment_cursor"), note)
            if entry.get("asked_block") != block_id:
                # Asked by another process (worker hook); adopt its question without a cursor reset.
                self.state.update_link(link.id, asked_block=block_id)
            return self._relay_answer(link, card_id, block_id, entry.get("comment_cursor"))
        except RateLimited:
            raise
        except Exception as exc:
            self.state.update_link(link.id, last_error=_error_text(exc))
            logger.warning("issue-sync: blocking sync failed for %s: %s", link.id, _error_text(exc))
            return 0

    def _ask(self, link: Link, card_id: Any, block_id: str, reason: str, cursor: str | None, note: str) -> int:
        question = reason or "(no reason given)"
        body = ("Hermes is blocked on this issue and needs an answer:\n\n"
                + "\n".join(f"> {line}" for line in question.splitlines())
                + "\n\nReply in a comment here and the card will resume with your answer.")
        self.adapter.post_comment(link.key, sign(body))
        # Move the cursor past everything up to and including the question, so only replies
        # written after it count as answers.
        _, cursor = self.adapter.list_comments_since(link.key, cursor)
        self.state.update_link(link.id, asked_block=block_id, comment_cursor=cursor, last_error=None,
                               question=question)
        self.kanban.comment(card_id, note)
        return 1

    def _relay_answer(self, link: Link, card_id: Any, block_id: str, cursor: str | None) -> int:
        comments, new_cursor = self.adapter.list_comments_since(link.key, cursor)
        answers = [c for c in comments if not c.is_bot and c.body.strip()]
        if not answers:
            if new_cursor != cursor:
                self.state.update_link(link.id, comment_cursor=new_cursor)
            return 0
        text = "\n\n".join(f"{c.author or 'someone'}: {strip_signature(c.body).strip()}" for c in answers)
        self.kanban.unblock(card_id, f"Answer from {self.adapter.name} issue {link.key}:\n{text}")
        self.state.update_link(link.id, comment_cursor=new_cursor, answered_block=block_id, last_error=None)
        return 1
