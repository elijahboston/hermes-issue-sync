"""hermes-issue-sync: two-way sync between Hermes kanban cards and issue trackers.

Hermes loads this package and calls ``register(ctx)``. Only documented plugin surfaces are
used: ``ctx.register_hook``, ``ctx.register_command``, ``ctx.get_config``, ``ctx.state``.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .issue_sync.adapters import get_adapter
from .issue_sync.engine import SyncEngine
from .issue_sync.kanban import KanbanClient
from .issue_sync.secrets import ensure_profile_scope
from .issue_sync.state import SyncState

logger = logging.getLogger(__name__)

TRACKER_SETTING = "plugins.entries.hermes-issue-sync.settings.tracker"
ENABLED_SETTING = "plugins.entries.hermes-issue-sync.settings.enabled"
HOOKS = ("on_kanban_dispatch_tick", "kanban_task_claimed", "kanban_task_completed", "kanban_task_blocked")
USAGE = "usage: /issues status"

_DEFAULTS: dict[str, Any] = {
    "tracker": "",
    "filter": {},
    "state_map": {},
    "assignee": "",
    "board": "",
    "poll_interval_seconds": 60,
    "enabled": True,
    # Adapter credentials: names of the env vars / profile secrets to read, never the values.
    "api_key_env": "LINEAR_API_KEY",
    "base_url_env": "JIRA_BASE_URL",
    "email_env": "JIRA_EMAIL",
    "token_env": "JIRA_API_TOKEN",
}

_logged: set[str] = set()


def _log_once(level: int, message: str, *args: Any) -> None:
    key = message % args if args else message
    if key in _logged:
        return
    _logged.add(key)
    logger.log(level, message, *args)


def _read_settings(ctx: Any) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    for key, default in _DEFAULTS.items():
        value = ctx.get_config(key, default=default)
        settings[key] = default if value is None else value
    for key in ("tracker", "assignee", "board", "api_key_env", "base_url_env", "email_env", "token_env"):
        settings[key] = str(settings[key]).strip()
    for key in ("filter", "state_map"):
        if not isinstance(settings[key], Mapping):
            _log_once(logging.WARNING, "issue-sync: setting %r must be a mapping; ignoring it", key)
            settings[key] = {}
        settings[key] = dict(settings[key])
    return settings


class _CtxStateStore:
    """``ctx.state`` resolved on each use, so ``register()`` itself never touches it."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    def get(self, key: str, default: Any = None) -> Any:
        return self._ctx.state.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._ctx.state.set(key, value)


def _enabled(settings: Mapping[str, Any]) -> bool:
    value = settings.get("enabled", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(value)


def _board_matches(settings: Mapping[str, Any], kwargs: Mapping[str, Any]) -> bool:
    """False only when a board is configured and the hook names a different one."""
    board = settings.get("board") or ""
    if not board or "board" not in kwargs:
        return True
    return (kwargs.get("board") or "default") == board


def _scoped(fn: Callable[..., Any], *args: Any) -> Any:
    """Run ``fn`` with this profile's secret scope bound (see ``ensure_profile_scope``)."""
    with ensure_profile_scope():
        return fn(*args)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def status_text(engine_or_none: Any, state: SyncState, settings: Mapping[str, Any]) -> str:
    tracker = (settings.get("tracker") or "").strip()
    if not tracker:
        return f"issue-sync: no tracker is configured. Set `{TRACKER_SETTING}` to enable sync."
    if not _enabled(settings):
        return f"issue-sync: sync is disabled (`{ENABLED_SETTING}` is false). Tracker: {tracker}"

    links = state.links()
    linked = sum(1 for entry in links.values() if isinstance(entry, dict) and entry.get("card_id"))
    last_poll = state.last_poll_at
    lines = [
        f"tracker: {tracker}",
        f"board: {settings.get('board') or 'default'}",
        f"linked cards: {linked}",
        f"last poll: {_iso(last_poll) if last_poll is not None else 'never'}",
    ]
    if engine_or_none is None:
        lines.insert(1, "engine: not running in this process")
    backoff = state.backoff_until
    if backoff is not None and backoff > time.time():
        lines.append(f"backoff until: {_iso(backoff)}")
    if state.last_error:
        lines.append(f"last error: {state.last_error}")
    errors = [
        f"  {link_id} (card {entry.get('card_id') or '?'}): {entry['last_error']}"
        for link_id, entry in sorted(links.items())
        if isinstance(entry, dict) and entry.get("last_error")
    ]
    if errors:
        lines.append("link errors:")
        lines.extend(errors)
    else:
        lines.append("link errors: no errors")
    return "\n".join(lines)


def register(ctx: Any, *, kanban: Any = None, background: bool = True) -> None:
    settings = _read_settings(ctx)
    state = SyncState(_CtxStateStore(ctx))
    engine: Any = None
    setup_error: str | None = None
    tracker = settings["tracker"]

    if not tracker:
        _log_once(logging.INFO, "issue-sync: no tracker configured (%s); sync is off", TRACKER_SETTING)
    elif not _enabled(settings):
        _log_once(logging.INFO, "issue-sync: sync is disabled by configuration")
    else:
        try:
            adapter = get_adapter(tracker, settings)
            client = kanban if kanban is not None else KanbanClient(settings["board"] or None)
            engine = SyncEngine(adapter, client, state, settings)
        except Exception as exc:  # a bad tracker config must not break Hermes startup
            engine = None
            setup_error = f"{type(exc).__name__}: {exc}"
            # Only the exception message is logged, never the settings (they may hold tokens).
            _log_once(logging.WARNING, "issue-sync: tracker %r unavailable: %s", tracker, setup_error)

    def issues_command(raw_args: str = "", **_kwargs: Any) -> str:
        sub = (raw_args or "").strip().lower()
        if sub not in ("", "status"):
            return USAGE
        try:
            text = status_text(engine, state, settings)
        except Exception as exc:
            logger.warning("issue-sync: status failed: %s: %s", type(exc).__name__, exc)
            return f"issue-sync: status unavailable ({type(exc).__name__}: {exc})"
        if setup_error:
            return f"issue-sync: tracker {tracker!r} could not be started: {setup_error}\n{text}"
        return text

    ctx.register_command(
        "issues",
        handler=issues_command,
        description="Issue tracker sync for kanban cards",
        args_hint="status",
    )

    if engine is None:
        return

    worker: list[threading.Thread] = []

    def on_dispatch_tick(**kwargs: Any) -> None:
        try:
            if kwargs.get("dry_run") or not _board_matches(settings, kwargs):
                return
            if not engine.due():
                return
            if not background:
                _scoped(engine.tick)
                return
            if worker and worker[0].is_alive():
                return
            # The copied context carries the profile's secret scope (and any other ContextVar)
            # into the worker thread; a bare Thread would start with an empty context.
            context = contextvars.copy_context()
            thread = threading.Thread(target=context.run, args=(_scoped, engine.tick),
                                      name="issue-sync-tick", daemon=True)
            worker[:] = [thread]
            thread.start()
        except Exception as exc:
            logger.warning("issue-sync: dispatch tick failed: %s: %s", type(exc).__name__, exc)

    def push_hook(event: str) -> Callable[..., None]:
        def hook(**kwargs: Any) -> None:
            try:
                task_id = kwargs.get("task_id")
                if not task_id or not _board_matches(settings, kwargs):
                    return
                _scoped(engine.push_outbound, str(task_id))
            except Exception as exc:
                logger.warning("issue-sync: %s push for %s failed: %s: %s",
                               event, kwargs.get("task_id"), type(exc).__name__, exc)

        hook.__name__ = f"on_{event}"
        return hook

    ctx.register_hook("on_kanban_dispatch_tick", on_dispatch_tick)
    for event in HOOKS[1:]:
        ctx.register_hook(event, push_hook(event))
