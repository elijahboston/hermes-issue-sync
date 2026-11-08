"""Credential lookup that honours the Hermes per-profile secret scope.

Inside Hermes, ``agent.secret_scope.get_secret`` resolves a name against the active profile's
scope (and fails closed when a multiplexing process has no scope bound). Outside Hermes (tests,
``scripts/live_check.py``) that module is absent and the process environment is used instead.
Values are never logged.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)
_warned: list[bool] = []


def get_secret(name: str) -> str | None:
    try:
        from agent.secret_scope import get_secret as scoped_get_secret
    except ImportError:  # running outside Hermes
        return os.environ.get(name)
    return scoped_get_secret(name)


@contextlib.contextmanager
def ensure_profile_scope() -> Iterator[None]:
    """Bind this profile's secret scope when Hermes fired a hook without one.

    On a multi-profile gateway the kanban dispatcher fires ``on_kanban_dispatch_tick`` from a
    fresh thread with no secret scope, so every ``get_secret`` raises ``UnscopedSecretError``.
    Hermes's own auto-decompose tick binds the gateway launch profile's scope for the same reason
    (``gateway/kanban_watchers_dispatcher.py::_default_profile_secret_scope``); this mirrors it.

    No-op when a scope is already bound (Hermes did its job), when the gateway serves a single
    profile, or outside Hermes. Remove once the dispatcher binds the scope itself.
    """
    try:
        from agent.secret_scope import (current_secret_scope, is_multiplex_active,
                                        reset_secret_scope, set_secret_scope)
        from hermes_constants import get_hermes_home
        from tui_gateway.launch_profile_policy import owner_secret_scope
    except ImportError:  # outside Hermes, or a Hermes without these seams
        yield
        return
    if current_secret_scope() is not None or not is_multiplex_active():
        yield
        return
    home = Path(get_hermes_home())
    if not _warned:
        _warned.append(True)
        logger.info("issue-sync: hook fired without a profile secret scope; binding %s's own scope "
                    "(workaround for the kanban dispatcher, see README)", home.name or "default")
    token = set_secret_scope(owner_secret_scope(home), profile_home=str(home))
    try:
        yield
    finally:
        reset_secret_scope(token)
