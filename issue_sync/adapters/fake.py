"""In-memory tracker for tests and local demos (``tracker: fake``). Records every call."""

from dataclasses import replace
from typing import Any, Iterable, Mapping

from ..signature import is_signed
from .base import AdapterError, Comment, Issue, TrackerAdapter, TransitionNotAllowed


class FakeAdapter(TrackerAdapter):
    def __init__(
        self,
        issues: Iterable[Issue] = (),
        *,
        name: str = "fake",
        allowed_states: Iterable[str] | None = None,
    ) -> None:
        self._name = name
        self.issues: dict[str, Issue] = {issue.key: issue for issue in issues}
        self.comments: dict[str, list[Comment]] = {}
        # None = every transition allowed; otherwise moves to other states raise TransitionNotAllowed.
        self.allowed_states = set(allowed_states) if allowed_states is not None else None
        self.calls: list[tuple[Any, ...]] = []
        self.check_error: str | None = None
        # (method, key or None) -> exceptions raised by the next matching calls, in order.
        self._failures: dict[tuple[str, str | None], list[Exception]] = {}
        self._comment_seq = 0

    @property
    def name(self) -> str:
        return self._name

    # ── test helpers ──────────────────────────────────────────────────────

    def add_issue(self, key: str, title: str = "", *, body: str = "", state: str = "Todo",
                  url: str | None = None, labels: Iterable[str] = (), updated_at: str | None = None,
                  closed: bool = False, priority: int = 0, estimate: float | None = None,
                  created_at: str | None = None, open_children: Iterable[str] = (),
                  open_blockers: Iterable[str] = (), ancestor_labels: Iterable[str] = ()) -> Issue:
        issue = Issue(
            key=key, title=title or key, body=body,
            url=url if url is not None else f"https://fake.invalid/{key}",
            state=state, labels=tuple(labels), updated_at=updated_at, closed=closed,
            priority=priority, estimate=estimate, created_at=created_at,
            open_children=tuple(open_children), open_blockers=tuple(open_blockers),
            ancestor_labels=tuple(ancestor_labels),
        )
        self.issues[key] = issue
        return issue

    def fail_next(self, method: str, exc: Exception, *, key: str | None = None) -> None:
        """Make the next ``method`` call (for ``key``, or any key when ``None``) raise ``exc``."""
        self._failures.setdefault((method, key), []).append(exc)

    def _maybe_fail(self, method: str, key: str | None = None) -> None:
        for slot in ((method, key), (method, None)):
            queue = self._failures.get(slot)
            if queue:
                raise queue.pop(0)

    def _issue(self, key: str) -> Issue:
        try:
            return self.issues[key]
        except KeyError:
            raise AdapterError(f"{self._name}: unknown issue {key}") from None

    # ── TrackerAdapter ────────────────────────────────────────────────────

    def fetch_issues(self, filter: Mapping[str, Any]) -> list[Issue]:
        self.calls.append(("fetch_issues", dict(filter)))
        self._maybe_fail("fetch_issues")
        states = filter.get("state")
        if isinstance(states, str):
            states = [states]
        labels = set(filter.get("labels") or ())
        return [
            issue for _, issue in sorted(self.issues.items())
            if (not states or issue.state in states) and labels.issubset(issue.labels)
        ]

    def move_issue(self, key: str, target_state: str) -> None:
        self.calls.append(("move_issue", key, target_state))
        self._maybe_fail("move_issue", key)
        issue = self._issue(key)
        if self.allowed_states is not None and target_state not in self.allowed_states:
            raise TransitionNotAllowed(f"{self._name}: {key} cannot move to {target_state!r}")
        self.issues[key] = replace(issue, state=target_state)

    def post_comment(self, key: str, body: str) -> Comment:
        self.calls.append(("post_comment", key, body))
        self._maybe_fail("post_comment", key)
        self._issue(key)
        self._comment_seq += 1
        comment = Comment(id=f"c{self._comment_seq}", author="hermes", body=body, is_bot=is_signed(body))
        self.comments.setdefault(key, []).append(comment)
        return comment

    def list_comments_since(self, key: str, cursor: str | None) -> tuple[list[Comment], str | None]:
        self.calls.append(("list_comments_since", key, cursor))
        self._maybe_fail("list_comments_since", key)
        self._issue(key)
        comments = self.comments.get(key, [])
        ids = [c.id for c in comments]
        start = ids.index(cursor) + 1 if cursor in ids else 0
        new = comments[start:]
        return new, (new[-1].id if new else cursor)

    def check(self) -> None:
        self.calls.append(("check",))
        if self.check_error:
            raise AdapterError(self.check_error)
