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

from .adapters.base import RateLimited, TrackerAdapter
from .links import Link, card_body, card_title, done_note, has_done_note, idempotency_key, link_from_card
from .state import SyncState

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 60.0
MIN_BACKOFF = 1.0
# Kanban statuses a user may map to tracker states via ``state_map``.
MAPPABLE_STATUSES = frozenset({"ready", "running", "blocked", "review", "done"})


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
        logger.warning("ticket-sync: ignoring state_map entries for %s (keys must be among %s with a "
                       "non-empty tracker state)", ", ".join(ignored), ", ".join(sorted(MAPPABLE_STATUSES)))
    return mapped


def _error_text(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


class SyncEngine:
    def __init__(self, adapter: TrackerAdapter, kanban: Any, state: SyncState,
                 settings: Mapping[str, Any] | None, clock: Callable[[], float] = time.time) -> None:
        settings = settings if isinstance(settings, Mapping) else {}
        self.adapter = adapter
        self.kanban = kanban
        self.state = state
        self.clock = clock
        filter_ = settings.get("filter")
        self.filter: dict[str, Any] = dict(filter_) if isinstance(filter_, Mapping) else {}
        self.state_map = _state_map(settings.get("state_map"))
        assignee = settings.get("assignee")
        self.assignee: str | None = (assignee.strip() or None) if isinstance(assignee, str) else None
        self.poll_interval = _poll_interval(settings.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL))
        # Hooks and the dispatcher tick may race; a busy engine skips rather than queueing work.
        self._lock = threading.Lock()

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
            logger.exception("ticket-sync: could not schedule a sync pass")
            return False
        try:
            self._run_pass()
        except Exception:
            logger.exception("ticket-sync: sync pass failed unexpectedly")
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
            # Phases are independent: a broken tracker filter must not stop completion summaries.
            self._phase("inbound", errors, self._poll_inbound, cards)
            self._phase("outbound", errors, self._push_outbound, cards)
            self._phase("blocking", errors, self.sync_block_questions)
        except RateLimited as exc:
            delay = max(exc.retry_after, MIN_BACKOFF)
            self.state.backoff_until = self.clock() + delay
            errors.append(f"{type(exc).__name__}: {_error_text(exc)}")
            logger.warning("ticket-sync: %s rate limited the sync; backing off %gs", self.adapter.name, delay)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {_error_text(exc)}")
            logger.warning("ticket-sync: sync pass failed: %s", errors[-1])
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
            logger.warning("ticket-sync: %s sync failed: %s", name, errors[-1])

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
        linked = {link.id for link in map(link_from_card, cards) if link is not None and link.tracker == tracker}
        created = 0
        for issue in self.adapter.fetch_issues(self.filter):
            link = Link(tracker, issue.key, issue.url)
            if link.id in linked:
                continue
            try:
                card_id = self.kanban.create_card(
                    card_title(issue.key, issue.title),
                    card_body(tracker, issue.key, issue.url, issue.body),
                    idempotency_key=idempotency_key(tracker, issue.key),
                    assignee=self.assignee,
                )
            except RateLimited:
                raise
            except Exception as exc:
                self.state.update_link(link.id, last_error=_error_text(exc))
                logger.warning("ticket-sync: could not create a card for %s: %s", link.id, _error_text(exc))
                continue
            self.state.update_link(link.id, card_id=card_id, last_pushed_state=issue.state, last_error=None)
            linked.add(link.id)
            created += 1
        return created

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
            return self._push_card(show["task"], show)
        finally:
            self._lock.release()

    def _push_outbound(self, cards: Iterable[Mapping[str, Any]]) -> int:
        return sum(self._push_card(card) for card in cards)

    def _push_card(self, card: Mapping[str, Any], show: Mapping[str, Any] | None = None) -> int:
        link = link_from_card(card)
        if link is None or link.tracker != self.adapter.name:
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
            logger.warning("ticket-sync: outbound sync failed for %s: %s", link.id, last_error)
        return moved

    def _post_done_summary(self, link: Link, card_id: Any, show: Mapping[str, Any] | None) -> None:
        if show is None:
            show = self.kanban.show_card(card_id)
        # The note on the card covers processes with different plugin state (worker vs dispatcher).
        if not has_done_note(show.get("comments"), link):
            summary = show.get("latest_summary")
            text = summary.strip() if isinstance(summary, str) else ""
            self.adapter.post_comment(link.key, text or f"Completed by Hermes kanban card {card_id}")
            # Recorded before the card note so a failing note never causes a second post here.
            self.state.update_link(link.id, done_comment_posted=True)
            self.kanban.comment(card_id, done_note(link))
        else:
            self.state.update_link(link.id, done_comment_posted=True)

    # ── blocking questions (CORE-605) ─────────────────────────────────────

    def sync_block_questions(self) -> None:
        """Seam for CORE-605 two-way blocking; intentionally a no-op in CORE-602.

        CORE-605 will mirror a blocked card's question onto its tracker issue and, when a
        human answers on the tracker, relay the answer with ``kanban unblock`` (using the
        per-link ``comment_cursor`` from ``list_comments_since``).
        """
