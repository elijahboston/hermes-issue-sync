"""Owner-set command that shapes a kanban card before it is created.

Issue text is untrusted: it is passed as JSON on stdin, never interpolated into
argv or a shell. ``prepare_command`` is read only from plugin settings.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

logger = logging.getLogger(__name__)

_STDERR_LIMIT = 300
_STDOUT_LIMIT = 120
_STR_FIELDS = ("body", "workspace", "branch", "model", "provider", "title", "assignee")
_ALLOWED = frozenset(_STR_FIELDS + ("skills",))
_SPECIAL = frozenset({"skip", "reason"})


class PrepareError(Exception):
    """The prepare command failed. ``str(exc)`` is shown to users: keep it human, never include env."""


@dataclass(frozen=True)
class PrepareResult:
    skip: bool = False
    reason: str = ""
    body: str | None = None
    workspace: str | None = None
    branch: str | None = None
    model: str | None = None
    provider: str | None = None
    skills: tuple[str, ...] = field(default_factory=tuple)
    title: str | None = None
    assignee: str | None = None


def _tail(text: str | None, limit: int = _STDERR_LIMIT) -> str:
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return "…" + flat[-(limit - 1):]


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _argv(command: str | Sequence[str]) -> list[str]:
    if isinstance(command, str):
        argv = shlex.split(command)
    else:
        argv = [str(part) for part in command]
    if not argv:
        raise PrepareError("prepare command is empty")
    argv[0] = os.path.expanduser(argv[0])
    return argv


def run_prepare(
    command: str | Sequence[str],
    payload: dict[str, Any],
    *,
    timeout: float,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> PrepareResult:
    """Run ``command`` with ``payload`` as JSON on stdin. Never uses a shell."""
    argv = _argv(command)
    env = os.environ.copy()
    env["ISSUE_SYNC_CONNECTION"] = str(payload.get("connection") or "")
    env["ISSUE_SYNC_KEY"] = str(payload.get("key") or "")
    try:
        proc = runner(
            argv,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
            env=env,
        )
    except FileNotFoundError:
        raise PrepareError(f"prepare command failed: {argv[0]!r} not found") from None
    except subprocess.TimeoutExpired as exc:
        detail = _tail(_text(exc.stderr))
        message = f"prepare command timed out after {timeout:g}s"
        raise PrepareError(f"{message}: {detail}" if detail else message) from None
    except OSError as exc:
        raise PrepareError(f"prepare command failed: {_tail(str(exc))}") from None

    stderr = _text(proc.stderr)
    if proc.returncode != 0:
        detail = _tail(stderr) or "no output"
        raise PrepareError(f"prepare command exited with status {proc.returncode}: {detail}")

    raw = proc.stdout or ""
    try:
        data = json.loads(raw)
    except ValueError:
        snippet = _tail(raw, _STDOUT_LIMIT) or "empty output"
        raise PrepareError(
            f"prepare command returned invalid JSON (exit {proc.returncode}): {snippet}"
        ) from None
    if not isinstance(data, dict):
        raise PrepareError(
            f"prepare command returned {type(data).__name__}, expected a JSON object "
            f"(exit {proc.returncode})"
        )
    return _parse_output(data, returncode=proc.returncode, stderr=stderr)


def _parse_output(data: Mapping[str, Any], *, returncode: int, stderr: str) -> PrepareResult:
    if "skip" in data and not isinstance(data["skip"], bool):
        raise PrepareError(
            f"prepare command field 'skip' must be a boolean (exit {returncode}): {_tail(stderr)}"
        )
    if "reason" in data and not isinstance(data["reason"], str):
        raise PrepareError(
            f"prepare command field 'reason' must be a string (exit {returncode}): {_tail(stderr)}"
        )
    if data.get("skip") is True:
        return PrepareResult(skip=True, reason=str(data.get("reason") or ""))

    unknown = sorted(str(key) for key in data if key not in _ALLOWED and key not in _SPECIAL)
    if unknown:
        logger.warning("issue-sync: prepare command returned unknown keys: %s", ", ".join(unknown))

    fields: dict[str, Any] = {}
    for key in _STR_FIELDS:
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, str):
            raise PrepareError(
                f"prepare command field {key!r} must be a string (exit {returncode}): {_tail(stderr)}"
            )
        fields[key] = value
    if "skills" in data:
        skills = data["skills"]
        if not isinstance(skills, list) or any(not isinstance(item, str) for item in skills):
            raise PrepareError(
                f"prepare command field 'skills' must be a list of strings (exit {returncode}): "
                f"{_tail(stderr)}"
            )
        fields["skills"] = tuple(skills)
    if fields.get("provider") and not fields.get("model"):
        raise PrepareError(
            f"prepare command field 'provider' requires 'model' (exit {returncode}): {_tail(stderr)}"
        )
    return PrepareResult(**fields)
