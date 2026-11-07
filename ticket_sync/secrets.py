"""Credential lookup that honours the Hermes per-profile secret scope.

Inside Hermes, ``agent.secret_scope.get_secret`` resolves a name against the active profile's
scope (and fails closed when a multiplexing process has no scope bound). Outside Hermes (tests,
``scripts/live_check.py``) that module is absent and the process environment is used instead.
Values are never logged.
"""

from __future__ import annotations

import os


def get_secret(name: str) -> str | None:
    try:
        from agent.secret_scope import get_secret as scoped_get_secret
    except ImportError:  # running outside Hermes
        return os.environ.get(name)
    return scoped_get_secret(name)
