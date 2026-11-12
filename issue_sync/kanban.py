"""The Hermes kanban board, reached only through the public ``hermes kanban`` CLI.

``KanbanClient`` shells out (``--json`` everywhere it exists); ``FakeKanban`` is the
in-memory stand-in with the same five public methods for tests and local demos.
"""

from __future__ import annotations

import copy
import itertools
import json
import re
import subprocess
import time
from typing import Any, Callable, Sequence

from .links import COMMENT_AUTHOR

_DETAIL_LIMIT = 300
_CARD_ID_LINE = re.compile(r"^(t_[A-Za-z0-9_-]+)\b")


class KanbanError(Exception):
    """A kanban CLI call failed. ``str(exc)`` is a short human message."""


def _trim(text: str | None, limit: int = _DETAIL_LIMIT) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _created_id(stdout: str) -> str | None:
    """Card id from ``kanban create --json`` output: ``{"id"}``, ``{"task": {"id"}}``, or a bare id."""
    text = stdout.strip()
    try:
        data: Any = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        task = data.get("task")
        for candidate in (data.get("id"), data.get("task_id"), task.get("id") if isinstance(task, dict) else None):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    elif isinstance(data, str) and data.strip().startswith("t_"):
        return data.strip()
    for line in text.splitlines():
        match = _CARD_ID_LINE.match(line.strip())
        if match:
            return match[1]
    return None


class KanbanClient:
    """``hermes kanban`` over subprocess. Every failure surfaces as ``KanbanError``."""

    def __init__(
        self,
        board: str | None = None,
        *,
        hermes_bin: str = "hermes",
        timeout: float = 30.0,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.board = board or None
        self.hermes_bin = hermes_bin
        self.timeout = timeout
        self._runner = runner

    def _run(self, args: Sequence[str], *, input: str | None = None) -> str:
        argv = [self.hermes_bin, "kanban", *(["--board", self.board] if self.board else []), *args]
        what = f"kanban {args[0]}"
        try:
            proc = self._runner(argv, input=input, capture_output=True, text=True,
                                timeout=self.timeout, check=False)
        except FileNotFoundError:
            raise KanbanError(f"{what} failed: {self.hermes_bin!r} not found") from None
        except subprocess.TimeoutExpired:
            raise KanbanError(f"{what} timed out after {self.timeout:g}s") from None
        except OSError as exc:
            raise KanbanError(f"{what} failed: {_trim(str(exc))}") from None
        if proc.returncode != 0:
            detail = _trim(proc.stderr) or _trim(proc.stdout) or "no output"
            raise KanbanError(f"{what} exited with status {proc.returncode}: {detail}")
        return proc.stdout or ""

    def _run_json(self, args: Sequence[str]) -> Any:
        stdout = self._run(args)
        try:
            return json.loads(stdout)
        except ValueError:
            raise KanbanError(f"kanban {args[0]} returned invalid JSON: {_trim(stdout, 120) or 'empty output'}") from None

    def list_cards(self, status: str | None = None, *, archived: bool = False) -> list[dict]:
        args = ["list", "--json"]
        if status:
            args += ["--status", status]
        if archived:
            args.append("--archived")
        data = self._run_json(args)
        if not isinstance(data, list):
            raise KanbanError(f"kanban list returned {type(data).__name__}, expected a JSON array")
        return [card for card in data if isinstance(card, dict)]

    def show_card(self, task_id: str) -> dict:
        data = self._run_json(["show", task_id, "--json"])
        if not isinstance(data, dict) or not isinstance(data.get("task"), dict):
            raise KanbanError(f"kanban show {task_id} returned no task")
        return data

    def create_card(self, title: str, body: str, *, idempotency_key: str, assignee: str | None = None,
                    workspace: str | None = None, branch: str | None = None, skills: Sequence[str] = (),
                    model: str | None = None, provider: str | None = None) -> str:
        args = ["create", title, "--body-file", "-", "--idempotency-key", idempotency_key]
        if assignee:
            args += ["--assignee", assignee]
        if workspace:
            args += ["--workspace", workspace]
        if branch:
            args += ["--branch", branch]
        for skill in skills or ():
            args += ["--skill", skill]
        if model:
            args += ["--model", model]
            if provider:
                args += ["--provider", provider]
        args.append("--json")
        stdout = self._run(args, input=body)
        card_id = _created_id(stdout)
        if card_id is None:
            raise KanbanError(f"kanban create returned no card id: {_trim(stdout, 120) or 'empty output'}")
        return card_id

    def comment(self, task_id: str, text: str) -> None:
        self._run(["comment", "--author", COMMENT_AUTHOR, "--", task_id, text])

    def unblock(self, task_id: str, reason: str) -> None:
        self._run(["unblock", task_id, "--reason", reason])


class FakeKanban:
    """In-memory board with ``KanbanClient``'s public methods. Records every call in ``calls``."""

    def __init__(self) -> None:
        self.cards: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.summaries: dict[str, str | None] = {}
        self.comments: dict[str, list[dict[str, Any]]] = {}
        self.idempotency_keys: dict[str, str] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.create_kwargs: list[dict[str, Any]] = []
        self._failures: dict[str, list[Exception]] = {}
        self._seq = itertools.count(1)

    # ── test helpers ──────────────────────────────────────────────────────

    def add_card(self, title: str, body: str = "", *, status: str = "ready", assignee: str | None = None,
                 summary: str | None = None, idempotency_key: str | None = None) -> str:
        task_id = f"t_{next(self._seq):08x}"
        self.cards[task_id] = {"id": task_id, "title": title, "body": body, "assignee": assignee,
                               "status": status, "result": None, "completed_at": None}
        self.summaries[task_id] = summary
        self.comments[task_id] = []
        if idempotency_key:
            self.idempotency_keys[task_id] = idempotency_key
        return task_id

    def set_status(self, task_id: str, status: str) -> None:
        self._card(task_id)["status"] = status

    def block(self, task_id: str, reason: str) -> None:
        """Block a card the way ``hermes kanban block`` does: a ``blocked`` event + a comment."""
        card = self._card(task_id)
        card["status"] = "blocked"
        stamp = 1_794_000_000 + next(self._seq)
        self.events.setdefault(task_id, []).append(
            {"kind": "blocked", "payload": {"reason": reason}, "created_at": stamp, "run_id": None})
        self.comments[task_id].append({"author": "worker", "body": f"BLOCKED: {reason}", "created_at": stamp})

    def set_summary(self, task_id: str, summary: str | None) -> None:
        self._card(task_id)
        self.summaries[task_id] = summary

    def fail_next(self, method: str, exc: Exception) -> None:
        """Make the next call to public method ``method`` raise ``exc``."""
        self._failures.setdefault(method, []).append(exc)

    def _maybe_fail(self, method: str) -> None:
        queue = self._failures.get(method)
        if queue:
            raise queue.pop(0)

    def _card(self, task_id: str) -> dict[str, Any]:
        try:
            return self.cards[task_id]
        except KeyError:
            raise KanbanError(f"unknown kanban card {task_id}") from None

    # ── KanbanClient surface ──────────────────────────────────────────────

    def list_cards(self, status: str | None = None, *, archived: bool = False) -> list[dict]:
        self.calls.append(("list_cards", status, archived))
        self._maybe_fail("list_cards")
        return [
            copy.deepcopy(card) for card in self.cards.values()
            if (archived or card["status"] != "archived") and (status is None or card["status"] == status)
        ]

    def show_card(self, task_id: str) -> dict:
        self.calls.append(("show_card", task_id))
        self._maybe_fail("show_card")
        card = self._card(task_id)
        return {"task": copy.deepcopy(card), "latest_summary": self.summaries.get(task_id),
                "parents": [], "children": [], "comments": copy.deepcopy(self.comments[task_id]),
                "events": copy.deepcopy(self.events.get(task_id, [])), "runs": []}

    def create_card(self, title: str, body: str, *, idempotency_key: str, assignee: str | None = None,
                    workspace: str | None = None, branch: str | None = None, skills: Sequence[str] = (),
                    model: str | None = None, provider: str | None = None) -> str:
        self.calls.append(("create_card", title, body, idempotency_key, assignee))
        self.create_kwargs.append({
            "workspace": workspace, "branch": branch,
            "skills": tuple(skills) if skills else (), "model": model, "provider": provider,
        })
        self._maybe_fail("create_card")
        for task_id, key in self.idempotency_keys.items():
            if key == idempotency_key and self.cards[task_id]["status"] != "archived":
                return task_id
        task_id = self.add_card(title, body, assignee=assignee, idempotency_key=idempotency_key)
        self.cards[task_id].update(workspace=workspace, branch=branch,
                                   skills=tuple(skills) if skills else (), model=model, provider=provider)
        return task_id

    def comment(self, task_id: str, text: str) -> None:
        self.calls.append(("comment", task_id, text))
        self._maybe_fail("comment")
        self._card(task_id)
        self.comments[task_id].append({"author": COMMENT_AUTHOR, "body": text, "created_at": int(time.time())})

    def unblock(self, task_id: str, reason: str) -> None:
        self.calls.append(("unblock", task_id, reason))
        self._maybe_fail("unblock")
        card = self._card(task_id)
        if card["status"] == "blocked":
            card["status"] = "ready"
            self.comments[task_id].append({"author": "user", "body": reason, "created_at": int(time.time())})
