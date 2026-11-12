"""The reconciler: tracker issues -> kanban cards (inbound), card status -> tracker state (outbound).

Pure logic over three seams: a ``TrackerAdapter``, a kanban client (``KanbanClient`` or
``FakeKanban``), and ``SyncState``. Which card belongs to which issue is read from the card
marker (``links``); state only remembers what was already pushed so calls are not repeated.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Callable, Iterable, Mapping

from .adapters.base import Issue, RateLimited, TrackerAdapter
from .signature import sign, strip_signature
from .links import (Link, asked_note, card_body, card_title, done_note, has_done_note, has_note,
                    idempotency_key, link_from_card)
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


def _max_active(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
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


class SyncEngine:
    def __init__(self, adapter: TrackerAdapter, kanban: Any, state: SyncState,
                 settings: Mapping[str, Any] | None, clock: Callable[[], float] = time.time,
                 *, conn: str = "", owns_legacy: bool = False) -> None:
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
        # Why the last inbound pass skipped each issue key (for a later dry-run command).
        self.last_skips: dict[str, str] = {}
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
                card_id = self.kanban.create_card(
                    card_title(issue.key, issue.title),
                    card_body(tracker, issue.key, issue.url, issue.body, self.conn),
                    idempotency_key=idempotency_key(tracker, issue.key, self.conn),
                    assignee=self.assignee,
                )
            except RateLimited:
                self.last_skips = skips
                raise
            except Exception as exc:
                self.state.update_link(link.id, last_error=_error_text(exc))
                logger.warning("issue-sync: could not create a card for %s: %s", link.id, _error_text(exc))
                continue
            self.state.update_link(link.id, card_id=card_id, last_pushed_state=issue.state, last_error=None)
            linked.add(issue.key)
            created += 1
        self.last_skips = skips
        return created

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
