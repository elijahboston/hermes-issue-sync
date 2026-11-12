"""Worktree workspaces for new cards. Every git call goes through an injectable runner."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

GitRunner = Callable[[str, Sequence[str]], str]

_SLUG = re.compile(r"[^a-z0-9]+")
_NUM = re.compile(r"(\d+)")


class GitError(Exception):
    """A git command failed. ``str(exc)`` is shown to users."""


def run_git(repo: str, args: Sequence[str], *, runner: Callable[..., Any] = subprocess.run) -> str:
    argv = ["git", "-C", repo, *args]
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=60, check=False)
    except FileNotFoundError:
        raise GitError("git not found") from None
    except subprocess.TimeoutExpired:
        raise GitError(f"git {' '.join(args)} timed out") from None
    except OSError as exc:
        raise GitError(f"git {' '.join(args)} failed: {exc}") from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "no output").strip().splitlines()
        msg = detail[-1] if detail else "no output"
        raise GitError(f"git {' '.join(args)} failed: {msg}")
    return proc.stdout or ""


class Git:
    """``git -C <repo> …`` over a subprocess runner, so tests can inject a fake."""

    def __init__(self, runner: Callable[..., Any] | None = None) -> None:
        self._runner = runner or subprocess.run

    def __call__(self, repo: str, args: Sequence[str]) -> str:
        return run_git(repo, args, runner=self._runner)


def parse_workspace(value: Any) -> dict[str, str]:
    """Normalise a connection's ``workspace`` mapping. Anything but ``worktree`` is scratch."""
    if not isinstance(value, Mapping):
        return {"mode": "scratch"}
    mode = str(value.get("mode") or "scratch").strip().lower()
    if mode != "worktree":
        return {"mode": "scratch"}
    prefix = value.get("branch_prefix")
    return {
        "mode": "worktree",
        "repo": str(value.get("repo") or "").strip(),
        "base": str(value.get("base") or "").strip(),
        "root": str(value.get("root") or "").strip(),
        "branch_prefix": str(prefix).strip() if prefix not in (None, "") else "wt/",
    }


def slug_for(key: str, title: str) -> str:
    key_part = key.casefold()
    title_part = _SLUG.sub("-", title.casefold()).strip("-")[:40].rstrip("-")
    return f"{key_part}-{title_part}" if title_part else key_part


def _is_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in "*?[")


def _version_parts(text: str) -> tuple:
    return tuple(int(part) if part.isdigit() else part for part in _NUM.split(text) if part != "")


def resolve_base(repo: str, pattern: str, git: GitRunner) -> tuple[str, str]:
    """Return ``(base_name, sha)`` for ``pattern`` (a branch or a glob like ``release/*``)."""
    git(repo, ["fetch", "origin"])
    pattern = pattern.removeprefix("origin/")
    if not pattern:
        raise GitError("workspace.base is not set")
    if not _is_glob(pattern):
        try:
            sha = git(repo, ["rev-parse", "--verify", f"refs/remotes/origin/{pattern}"]).strip()
        except GitError:
            sha = git(repo, ["rev-parse", "--verify", pattern]).strip()
        return pattern, sha
    raw = git(repo, [
        "for-each-ref", "--format=%(refname:short)%09%(objectname)%09%(committerdate:unix)",
        f"refs/remotes/origin/{pattern}",
    ])
    prefix = pattern.split("*", 1)[0]
    refs: list[tuple[str, str, int, str]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name, sha = parts[0], parts[1]
        date = int(parts[2]) if len(parts) > 2 and str(parts[2]).lstrip("-").isdigit() else 0
        version = name.split(prefix, 1)[-1] if prefix and prefix in name else name.rsplit("/", 1)[-1]
        refs.append((name, sha, date, version))
    if not refs:
        raise GitError(f"no remote branch matches origin/{pattern}")
    try:
        refs.sort(key=lambda item: _version_parts(item[3]))
    except TypeError:
        refs.sort(key=lambda item: item[2])
    name, sha, _, _ = refs[-1]
    return name.removeprefix("origin/"), sha


def ensure_worktree(issue_key: str, title: str, cfg: Mapping[str, str], git: GitRunner) -> dict[str, str]:
    """Create (or reuse) a worktree for this issue. Returns placeholder values for the card template."""
    repo = str(Path(cfg["repo"]).expanduser())
    if not repo:
        raise GitError("workspace.repo is not set")
    base_name, base_sha = resolve_base(repo, cfg.get("base") or "", git)
    slug = slug_for(issue_key, title)
    branch = f"{cfg.get('branch_prefix') or 'wt/'}{slug}"
    repo_path = Path(repo).resolve()
    root = Path(cfg["root"]).expanduser() if cfg.get("root") else Path(str(repo_path) + ".worktrees")
    worktree = root / slug
    if not worktree.exists():
        worktree.parent.mkdir(parents=True, exist_ok=True)
        git(repo, ["worktree", "add", "-b", branch, str(worktree), base_sha])
    return {
        "base": base_name,
        "base_sha": base_sha,
        "worktree": str(worktree),
        "branch": branch,
    }
