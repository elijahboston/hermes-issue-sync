"""Worktree workspaces (CORE-632): glob newest-wins, reuse, flags on create_card."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from issue_sync.adapters.fake import FakeAdapter
from issue_sync.engine import SyncEngine
from issue_sync.kanban import FakeKanban, KanbanClient
from issue_sync.state import MemoryStore, SyncState
from issue_sync.workspace import Git, GitError, ensure_worktree, resolve_base, slug_for

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=GIT_ENV)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    origin = tmp_path / "origin.git"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "README").write_text("x\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "init")
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    return repo


def _push_branch(repo: Path, name: str) -> None:
    _git(repo, "checkout", "-b", name)
    _git(repo, "commit", "--allow-empty", "-m", name)
    _git(repo, "push", "origin", name)
    _git(repo, "checkout", "main")


class FakeGit:
    """Records git -C calls; creates the worktree dir on ``worktree add``."""

    def __init__(self, refs: str = "", sha: str = "abc123") -> None:
        self.calls: list[list[str]] = []
        self.refs = refs
        self.sha = sha

    def __call__(self, repo: str, args) -> str:
        args = list(args)
        self.calls.append(args)
        if args[:1] == ["fetch"]:
            return ""
        if args[:1] == ["for-each-ref"]:
            return self.refs
        if args[:1] == ["rev-parse"]:
            return self.sha + "\n"
        if args[:2] == ["worktree", "add"]:
            Path(args[4]).mkdir(parents=True, exist_ok=True)
            return ""
        return ""


def _engine(git=None, **settings):
    adapter = FakeAdapter()
    kanban = FakeKanban()
    engine = SyncEngine(adapter, kanban, SyncState(MemoryStore()), settings, git=git)
    return engine, adapter, kanban


def test_slug_is_key_and_truncated_title():
    slug = slug_for("CORE-1", "Fix the login form " + "x" * 80)
    assert slug.startswith("core-1-")
    title = slug.split("-", 2)[2] if slug.count("-") >= 2 else slug
    assert len(title) <= 40


def test_base_glob_picks_newest_version(tmp_path):
    repo = _repo(tmp_path)
    # Commit 2026.12 first so committer-date would pick a later 2026.10 if version sort failed.
    _push_branch(repo, "release/2026.12")
    _push_branch(repo, "release/2026.10")
    _push_branch(repo, "release/2026.11")
    name, sha = resolve_base(str(repo), "release/*", Git())
    assert name == "release/2026.12"
    assert len(sha) == 40


def test_base_glob_no_match_is_an_error(tmp_path):
    repo = _repo(tmp_path)
    try:
        resolve_base(str(repo), "release/*", Git())
    except GitError as exc:
        assert "no remote branch" in str(exc)
    else:
        raise AssertionError("expected GitError")


def test_worktree_created_once_and_reused(tmp_path):
    repo = _repo(tmp_path)
    root = tmp_path / "wts"
    cfg = {"mode": "worktree", "repo": str(repo), "base": "main", "root": str(root),
           "branch_prefix": "wt/"}
    git = Git()
    first = ensure_worktree("ISS-1", "Fix login", cfg, git)
    worktree = Path(first["worktree"])
    assert worktree.is_dir()
    assert first["branch"] == "wt/iss-1-fix-login"
    marker = worktree / "already"
    marker.write_text("keep\n")
    second = ensure_worktree("ISS-1", "Fix login", cfg, git)
    assert second["worktree"] == first["worktree"]
    assert marker.read_text() == "keep\n"
    assert list(root.iterdir()) == [worktree]


def test_default_root_is_sibling_of_repo(tmp_path):
    repo = _repo(tmp_path)
    cfg = {"mode": "worktree", "repo": str(repo), "base": "main", "root": "", "branch_prefix": "wt/"}
    info = ensure_worktree("ISS-9", "X", cfg, Git())
    assert Path(info["worktree"]).parent == Path(str(repo.resolve()) + ".worktrees")
    assert not str(info["worktree"]).startswith(str(repo.resolve()) + os.sep)


def test_flags_reach_create_card(tmp_path):
    git = FakeGit(sha="deadbeef")
    root = tmp_path / "wts"
    engine, adapter, kanban = _engine(
        git,
        workspace={"mode": "worktree", "repo": str(tmp_path / "repo"), "base": "main",
                   "root": str(root), "branch_prefix": "wt/"},
        skills=["repo", "kanban"],
    )
    adapter.add_issue("ISS-1", "Fix login")
    assert engine.poll_inbound() == 1
    card = next(iter(kanban.cards.values()))
    assert card["workspace"].startswith("worktree:")
    assert card["branch"] == "wt/iss-1-fix-login"
    assert card["skills"] == ["repo", "kanban"]
    call = kanban.calls[-1]
    assert call[0] == "create_card"
    assert call[5].startswith("worktree:") and call[6] == "wt/iss-1-fix-login"
    assert call[7] == ("repo", "kanban")
    assert ["worktree", "add"] in [c[:2] for c in git.calls]


def test_scratch_passes_no_workspace_flags():
    engine, adapter, kanban = _engine()
    adapter.add_issue("ISS-1")
    engine.poll_inbound()
    call = [c for c in kanban.calls if c[0] == "create_card"][0]
    assert call[5] is None and call[6] is None and call[7] == ()


def test_worktree_failure_is_a_per_issue_error():
    engine, adapter, kanban = _engine(
        FakeGit(refs=""),
        workspace={"mode": "worktree", "repo": "/nope", "base": "release/*", "root": "",
                   "branch_prefix": "wt/"},
    )
    adapter.add_issue("ISS-1", "Fix")
    assert engine.poll_inbound() == 0
    assert kanban.cards == {}
    assert engine.state.link("fake:ISS-1")["last_error"]


def test_kanban_client_emits_workspace_skill_and_model_flags():
    class Runner:
        def __init__(self):
            self.argv = None

        def __call__(self, argv, **kwargs):
            self.argv = argv

            class R:
                returncode = 0
                stdout = '{"id": "t_1"}'
                stderr = ""
            return R()

    runner = Runner()
    KanbanClient(runner=runner).create_card(
        "T", "B", idempotency_key="k", workspace="worktree:/tmp/wt", branch="wt/x",
        skills=("repo",), model="gpt", provider="openai")
    assert runner.argv[1:] == [
        "kanban", "create", "T", "--body-file", "-", "--idempotency-key", "k",
        "--workspace", "worktree:/tmp/wt", "--branch", "wt/x", "--skill", "repo",
        "--model", "gpt", "--provider", "openai", "--json",
    ]
