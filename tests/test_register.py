"""register(ctx): hooks, the /issues command, and status text, against a stub ctx."""

from __future__ import annotations

import contextvars
import importlib
import threading

import pytest

PACKAGE = "hermes_issue_sync"

HOOKS = {"on_kanban_dispatch_tick", "kanban_task_claimed", "kanban_task_completed", "kanban_task_blocked"}
TRACKER_SETTING = "plugins.entries.hermes-issue-sync.settings.tracker"


def _mod(name: str):
    return importlib.import_module(f"{PACKAGE}.issue_sync.{name}")


@pytest.fixture
def kanban(plugin):
    return _mod("kanban").FakeKanban()


@pytest.fixture
def make_ctx(plugin, kanban, stub_ctx):
    """Register the plugin (inline ticks) on a fresh stub ctx with the given settings."""

    def make(**config):
        ctx = stub_ctx(config)
        plugin.register(ctx, kanban=kanban, background=False)
        return ctx

    return make


def _status(ctx, args: str = "status") -> str:
    handler, _ = ctx.commands["issues"]
    return handler(args)


def _methods(kanban) -> list[str]:
    return [call[0] for call in kanban.calls]


def _linked_card(kanban, key: str = "FAKE-1", status: str = "running") -> str:
    links = _mod("links")
    body = links.card_body("fake", key, f"https://fake.invalid/{key}", "")
    return kanban.add_card(links.card_title(key, "Linked"), body, status=status)


def test_fake_tracker_registers_hooks_and_command(make_ctx, kanban):
    ctx = make_ctx(tracker="fake")
    assert set(ctx.hooks) == HOOKS
    handler, kwargs = ctx.commands["issues"]
    assert callable(handler)
    assert kwargs["args_hint"] == "status"
    assert kwargs["description"]


def test_dispatch_tick_runs_sync_and_status_reports_it(make_ctx, kanban):
    ctx = make_ctx(tracker="fake")
    assert "never" in _status(ctx)

    ctx.hooks["on_kanban_dispatch_tick"](board="default", extra="ignored")

    assert "list_cards" in _methods(kanban)
    text = _status(ctx)
    assert "tracker: fake" in text
    assert "board: default" in text
    assert "linked cards: 0" in text
    last_poll = next(line for line in text.splitlines() if line.startswith("last poll:"))
    assert "never" not in last_poll
    assert "no errors" in text
    assert _status(ctx, "") == text


def test_dry_run_tick_does_nothing(make_ctx, kanban):
    ctx = make_ctx(tracker="fake")
    ctx.hooks["on_kanban_dispatch_tick"](dry_run=True)
    assert kanban.calls == []
    assert "last poll: never" in _status(ctx)


def test_tick_for_other_board_is_skipped(make_ctx, kanban):
    ctx = make_ctx(tracker="fake", board="ops")
    ctx.hooks["on_kanban_dispatch_tick"](board="other")
    assert kanban.calls == []
    ctx.hooks["on_kanban_dispatch_tick"](board="ops")
    assert "list_cards" in _methods(kanban)
    assert "board: ops" in _status(ctx)


def test_second_tick_within_interval_is_skipped(make_ctx, kanban):
    ctx = make_ctx(tracker="fake", poll_interval_seconds=3600)
    ctx.hooks["on_kanban_dispatch_tick"]()
    calls = len(kanban.calls)
    ctx.hooks["on_kanban_dispatch_tick"]()
    assert len(kanban.calls) == calls


def test_claimed_hook_pushes_that_card(make_ctx, kanban):
    ctx = make_ctx(tracker="fake")
    card_id = _linked_card(kanban)
    kanban.calls.clear()

    ctx.hooks["kanban_task_claimed"](task_id=card_id, profile="default", unexpected=1)

    assert any(call[0] == "show_card" and card_id in call for call in kanban.calls)
    assert "list_cards" not in _methods(kanban)


@pytest.mark.parametrize("hook", ["kanban_task_claimed", "kanban_task_completed", "kanban_task_blocked"])
def test_push_hooks_never_raise(make_ctx, kanban, hook):
    ctx = make_ctx(tracker="fake")
    card_id = _linked_card(kanban)
    kanban.fail_next("show_card", _mod("kanban").KanbanError("kanban exploded"))
    ctx.hooks[hook](task_id=card_id)        # swallowed + logged
    ctx.hooks[hook](task_id="t_missing")    # unknown card: swallowed too
    ctx.hooks[hook]()                       # no task_id: skipped silently


def test_push_hook_without_task_id_does_nothing(make_ctx, kanban):
    ctx = make_ctx(tracker="fake")
    ctx.hooks["kanban_task_completed"](board="default")
    assert kanban.calls == []


def test_tick_never_raises_when_kanban_fails(make_ctx, kanban):
    ctx = make_ctx(tracker="fake")
    kanban.fail_next("list_cards", _mod("kanban").KanbanError("board unavailable"))
    ctx.hooks["on_kanban_dispatch_tick"]()
    assert "board unavailable" in _status(ctx)


def test_unset_tracker_registers_only_command(make_ctx, kanban):
    ctx = make_ctx()
    assert ctx.hooks == {}
    assert set(ctx.commands) == {"issues"}
    text = _status(ctx)
    assert "nothing is configured" in text
    assert TRACKER_SETTING in text


def test_blank_tracker_counts_as_unset(make_ctx, kanban):
    ctx = make_ctx(tracker="   ")
    assert ctx.hooks == {}
    assert TRACKER_SETTING in _status(ctx)


def test_disabled_registers_only_command(make_ctx, kanban):
    ctx = make_ctx(tracker="fake", enabled=False)
    assert ctx.hooks == {}
    assert set(ctx.commands) == {"issues"}
    assert "disabled" in _status(ctx)


def test_unknown_tracker_reports_error(make_ctx, kanban):
    ctx = make_ctx(tracker="nope", state_map={"done": "Done"})
    assert ctx.hooks == {}
    text = _status(ctx)
    assert "unknown tracker 'nope'" in text
    assert "fake" in text  # lists available adapters


def test_status_lists_per_link_errors(plugin, kanban, stub_ctx):
    ctx = stub_ctx({"tracker": "fake"})
    sync_state = _mod("state").SyncState(ctx.state)
    sync_state.update_link("fake:FAKE-1", card_id="t_aaaa1111", last_pushed_state="Todo")
    sync_state.update_link("fake:FAKE-2", card_id="t_bbbb2222", last_error="transition not allowed")
    sync_state.update_link("fake:FAKE-3", last_error="card create failed")
    sync_state.last_error = "AdapterError: tracker down"
    plugin.register(ctx, kanban=kanban, background=False)

    text = _status(ctx)
    assert "linked cards: 2" in text
    assert "last error: AdapterError: tracker down" in text
    assert "  fake:FAKE-2 (card t_bbbb2222): transition not allowed" in text
    assert "fake:FAKE-3" in text and "card create failed" in text
    assert "no errors" not in text


def test_status_shows_future_backoff(plugin, kanban, stub_ctx):
    ctx = stub_ctx({"tracker": "fake"})
    _mod("state").SyncState(ctx.state).backoff_until = 4102444800.0  # 2100-01-01
    plugin.register(ctx, kanban=kanban, background=False)
    assert "backoff until: 2100-01-01T00:00:00+00:00" in _status(ctx)


def test_unknown_subcommand_returns_usage(make_ctx, kanban):
    ctx = make_ctx(tracker="fake")
    assert _status(ctx, "frobnicate").startswith("usage:")


_PROFILE_SCOPE: contextvars.ContextVar[str | None] = contextvars.ContextVar("_PROFILE_SCOPE", default=None)


def test_background_tick_keeps_the_callers_context(plugin, kanban, stub_ctx):
    """The tick thread sees ContextVars (e.g. Hermes' profile secret scope) set by the hook caller."""
    seen: list[tuple[str | None, str]] = []
    ran = threading.Event()
    list_cards = kanban.list_cards

    def recording_list_cards(*args, **kwargs):
        seen.append((_PROFILE_SCOPE.get(), threading.current_thread().name))
        ran.set()
        return list_cards(*args, **kwargs)

    kanban.list_cards = recording_list_cards
    ctx = stub_ctx({"tracker": "fake"})
    plugin.register(ctx, kanban=kanban, background=True)

    def dispatcher() -> None:
        _PROFILE_SCOPE.set("profile-a")
        ctx.hooks["on_kanban_dispatch_tick"](board="default")

    contextvars.Context().run(dispatcher)  # set only in the dispatcher's own context
    assert ran.wait(5)
    assert seen == [("profile-a", "issue-sync-tick-fake")]
    assert _PROFILE_SCOPE.get() is None
