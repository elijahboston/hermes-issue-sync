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
from .issue_sync.connections import Connection, parse_connections
from .issue_sync.engine import SyncEngine
from .issue_sync import pane
from .issue_sync.kanban import KanbanClient
from .issue_sync.secrets import ensure_profile_scope
from .issue_sync.state import SyncState

logger = logging.getLogger(__name__)

TRACKER_SETTING = "plugins.entries.hermes-issue-sync.settings.tracker"
CONNECTIONS_SETTING = "plugins.entries.hermes-issue-sync.settings.connections"
ENABLED_SETTING = "plugins.entries.hermes-issue-sync.settings.enabled"
HOOKS = ("on_kanban_dispatch_tick", "kanban_task_claimed", "kanban_task_completed", "kanban_task_blocked")
USAGE = "usage: /issues [status|show]"
SHOW_EVENT = "pane.show"

_DEFAULTS: dict[str, Any] = {
    "tracker": "",
    "filter": {},
    "state_map": {},
    "assignee": "",
    "board": "",
    "poll_interval_seconds": 60,
    "enabled": True,
    "connections": [],
    "tree_style": "rounded",
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
    if not isinstance(settings["connections"], list):
        _log_once(logging.WARNING, "issue-sync: setting 'connections' must be a list; ignoring it")
        settings["connections"] = []
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


class _Runner:
    """One connection's engine, its state, and its background tick thread."""

    def __init__(self, conn: Connection, engine: Any, state: SyncState, error: str | None) -> None:
        self.conn, self.engine, self.state, self.error = conn, engine, state, error
        self.worker: threading.Thread | None = None


def _connection_lines(runner: _Runner) -> list[str]:
    conn, state = runner.conn, runner.state
    settings = conn.settings
    lines = [f"tracker: {conn.tracker or '?'}"]
    if conn.projects:
        lines.append("projects: " + ", ".join(conn.projects))
    lines.append(f"board: {settings.get('board') or 'default'}")
    if runner.error:
        lines.append(f"error: {runner.error}")
        return lines
    links = state.links()
    linked = sum(1 for entry in links.values() if isinstance(entry, dict) and entry.get("card_id"))
    last_poll = state.last_poll_at
    lines += [f"linked cards: {linked}",
              f"last poll: {_iso(last_poll) if last_poll is not None else 'never'}"]
    if runner.engine is None:
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
    return lines


def status_text(runners: list[_Runner], settings: Mapping[str, Any]) -> str:
    if not runners:
        return (f"issue-sync: nothing is configured. Set `{CONNECTIONS_SETTING}` "
                f"(or `{TRACKER_SETTING}` for a single tracker) to enable sync.")
    if not _enabled(settings):
        names = ", ".join(r.conn.name for r in runners)
        return f"issue-sync: sync is disabled (`{ENABLED_SETTING}` is false). Connections: {names}"
    if len(runners) == 1 and not runners[0].conn.id:
        return "\n".join(_connection_lines(runners[0]))
    blocks = []
    for runner in runners:
        body = "\n".join("  " + line for line in _connection_lines(runner))
        blocks.append(f"connection {runner.conn.name}:\n{body}")
    return "\n".join(blocks)


def _build_runners(ctx: Any, settings: Mapping[str, Any], kanban: Any) -> list[_Runner]:
    runners: list[_Runner] = []
    store = _CtxStateStore(ctx)
    legacy_owner: set[str] = set()
    clients: dict[str, Any] = {}
    for conn in parse_connections(settings):
        state = SyncState(store, namespace=conn.id)
        if conn.error:
            _log_once(logging.WARNING, "issue-sync: connection %s skipped: %s", conn.name, conn.error)
            runners.append(_Runner(conn, None, state, conn.error))
            continue
        try:
            adapter = get_adapter(conn.tracker, conn.settings)
            board = conn.board
            client = kanban if kanban is not None else clients.setdefault(board, KanbanClient(board or None))
            # Cards made before connections existed go to the first connection on their tracker.
            owns_legacy = conn.tracker not in legacy_owner
            legacy_owner.add(conn.tracker)
            engine = SyncEngine(adapter, client, state, conn.settings, conn=conn.id, owns_legacy=owns_legacy)
            runners.append(_Runner(conn, engine, state, None))
        except Exception as exc:  # one bad connection must not stop the others or Hermes startup
            error = f"{type(exc).__name__}: {exc}"
            # Only the exception message is logged, never the settings (they may hold tokens).
            _log_once(logging.WARNING, "issue-sync: connection %s (%s) unavailable: %s",
                      conn.name, conn.tracker, error)
            runners.append(_Runner(conn, None, state, error))
    return runners


_snapshot_lock = threading.Lock()


def show_pane(broadcast: Callable[..., None] | None = None) -> str:
    """Ask every connected Desktop window to toggle the Issue Sync panel."""
    try:
        if broadcast is None:
            from hermes_cli.plugin_events import broadcast_plugin_event as broadcast  # documented
        broadcast("hermes-issue-sync", SHOW_EVENT, {})
    except Exception as exc:
        return (f"issue-sync: could not reach Desktop ({type(exc).__name__}: {exc}). "
                "Open the pane from the Issue Sync chip in the status bar.")
    return ("issue-sync: asked Desktop to toggle the Issue Sync panel. If nothing happens, check that "
            "Issue Sync is turned on in Capabilities → Plugins (the Desktop half), then click the "
            "Issue Sync chip in the status bar.")


def publish_snapshot(runners: list[_Runner], enabled: bool, directory: Any = None,
                     tree_style: Any = None) -> None:
    """Write the sync pane's snapshot. Never raises: the pane is a view, sync must not depend on it."""
    try:
        target = directory if directory is not None else pane.data_dir()
        if target is None:
            return
        blocks = [pane.connection_snapshot(r.conn, r.state, r.engine.last_cards if r.engine else [],
                                           r.error, r.engine is not None) for r in runners]
        with _snapshot_lock:
            pane.write_snapshot(target, pane.build_snapshot(blocks, enabled=enabled,
                                                                tree_style=tree_style))
    except Exception as exc:
        logger.debug("issue-sync: could not write the pane snapshot: %s: %s", type(exc).__name__, exc)


def register(ctx: Any, *, kanban: Any = None, background: bool = True, pane_dir: Any = None) -> None:
    settings = _read_settings(ctx)
    runners: list[_Runner] = []
    if not _enabled(settings):
        runners = [_Runner(conn, None, SyncState(_CtxStateStore(ctx), namespace=conn.id), None)
                   for conn in parse_connections(settings)]
        _log_once(logging.INFO, "issue-sync: sync is disabled by configuration")
    else:
        runners = _build_runners(ctx, settings, kanban)
        if not runners:
            _log_once(logging.INFO, "issue-sync: no connections configured (%s); sync is off", CONNECTIONS_SETTING)

    def issues_command(raw_args: str = "", **_kwargs: Any) -> str:
        sub = (raw_args or "").strip().lower()
        if sub == "show":
            return show_pane()
        if sub not in ("", "status"):
            return USAGE
        try:
            return status_text(runners, settings)
        except Exception as exc:
            logger.warning("issue-sync: status failed: %s: %s", type(exc).__name__, exc)
            return f"issue-sync: status unavailable ({type(exc).__name__}: {exc})"

    ctx.register_command(
        "issues",
        handler=issues_command,
        description="Issue tracker sync for kanban cards",
        args_hint="[status|show]",
    )

    enabled = _enabled(settings)
    tree_style = settings.get("tree_style")
    publish_snapshot(runners, enabled, pane_dir, tree_style)
    # Hooks are registered even with nothing configured, so what the manifest declares always
    # matches what is registered; with no live connection they simply do nothing.
    live = [r for r in runners if r.engine is not None]

    def run_and_publish(engine: Any) -> None:
        engine.tick()
        publish_snapshot(runners, enabled, pane_dir, tree_style)

    def tick(runner: _Runner) -> None:
        engine = runner.engine
        if not engine.due():
            return
        if not background:
            _scoped(run_and_publish, engine)
            return
        if runner.worker is not None and runner.worker.is_alive():
            return
        # The copied context carries the profile's secret scope (and any other ContextVar)
        # into the worker thread; a bare Thread would start with an empty context.
        context = contextvars.copy_context()
        thread = threading.Thread(target=context.run, args=(_scoped, run_and_publish, engine),
                                  name=f"issue-sync-tick-{runner.conn.name}", daemon=True)
        runner.worker = thread
        thread.start()

    def on_dispatch_tick(**kwargs: Any) -> None:
        if kwargs.get("dry_run"):
            return
        for runner in live:
            try:
                if _board_matches(runner.conn.settings, kwargs):
                    tick(runner)
            except Exception as exc:
                logger.warning("issue-sync: dispatch tick failed for %s: %s: %s",
                               runner.conn.name, type(exc).__name__, exc)

    def push_hook(event: str) -> Callable[..., None]:
        def hook(**kwargs: Any) -> None:
            task_id = kwargs.get("task_id")
            if not task_id:
                return
            for runner in live:
                try:
                    if _board_matches(runner.conn.settings, kwargs):
                        # Each engine ignores cards that belong to another connection.
                        _scoped(runner.engine.push_outbound, str(task_id))
                except Exception as exc:
                    logger.warning("issue-sync: %s push for %s (%s) failed: %s: %s", event,
                                   task_id, runner.conn.name, type(exc).__name__, exc)

        hook.__name__ = f"on_{event}"
        return hook

    ctx.register_hook("on_kanban_dispatch_tick", on_dispatch_tick)
    for event in HOOKS[1:]:
        ctx.register_hook(event, push_hook(event))
