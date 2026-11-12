"""Detached runner for an owner-set land command.

Started as::

    python land_runner.py <result_file> --timeout N -- <argv...>

Runs argv with no shell, enforces the timeout, and writes a JSON result file
atomically (tmp + rename). Never prints env. Safe to import; ``main`` is the CLI.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

SCRIPT = Path(__file__).resolve()
DEFAULT_TIMEOUT = 1800.0
RESULT_TAIL = 8000


def parse_command(command: str | Sequence[str]) -> list[str]:
    if isinstance(command, str):
        argv = shlex.split(command)
    else:
        argv = [str(part) for part in command]
    if not argv:
        raise ValueError("land command is empty")
    argv[0] = os.path.expanduser(argv[0])
    return argv


def launch_argv(result_file: str | Path, command: str | Sequence[str], timeout: float) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        str(result_file),
        "--timeout",
        str(timeout),
        "--",
        *parse_command(command),
    ]


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def tail(text: str | None, limit: int = RESULT_TAIL) -> str:
    value = _text(text)
    return value if len(value) <= limit else value[-limit:]


def write_result(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, target)


def read_result(path: str | Path) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _timeout_value(value: Any, default: float = DEFAULT_TIMEOUT) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    return seconds if seconds > 0 else default


def _result(exit_code: int, stdout: str | None, stderr: str | None) -> dict[str, Any]:
    return {
        "exit": int(exit_code),
        "stdout_tail": tail(stdout),
        "stderr_tail": tail(stderr),
        "finished_at": time.time(),
    }


def run_and_write(result_file: str | Path, argv: Sequence[str], timeout: float) -> dict[str, Any]:
    if not argv:
        payload = _result(2, "", "land command is empty")
        write_result(result_file, payload)
        return payload
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=_timeout_value(timeout),
            check=False,
            shell=False,
        )
        payload = _result(proc.returncode, proc.stdout, proc.stderr)
    except FileNotFoundError:
        payload = _result(2, "", f"{argv[0]!r} not found")
    except subprocess.TimeoutExpired as exc:
        payload = _result(2, _text(exc.stdout), _text(exc.stderr) or "land command timed out")
    except OSError as exc:
        payload = _result(2, "", str(exc) or type(exc).__name__)
    write_result(result_file, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return 2
    result_file = args.pop(0)
    timeout = DEFAULT_TIMEOUT
    command: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--":
            command = args[i + 1:]
            break
        if token == "--timeout" and i + 1 < len(args):
            timeout = _timeout_value(args[i + 1], timeout)
            i += 2
            continue
        i += 1
    try:
        run_and_write(result_file, command, timeout)
    except Exception:
        try:
            write_result(result_file, _result(2, "", "land runner failed"))
        except Exception:
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
