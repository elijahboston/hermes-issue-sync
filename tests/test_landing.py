"""Done gate and background landing (CORE-633)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from issue_sync.connections import parse_connections
from issue_sync.engine import SyncEngine
from issue_sync.land_runner import SCRIPT, read_result, write_result
from issue_sync.links import Link, done_note
from issue_sync.signature import is_signed, sign
from issue_sync import pane

from test_engine import STATE_MAP, Env, linked


NASTY = "$(rm -rf ~) `reboot`; echo pwned"


class FakeLauncher:
    """Stands in for ``subprocess.Popen``: records argv/kwargs and never waits."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.waited = False

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        launcher = self

        class Proc:
            def wait(self, *a, **k):
                launcher.waited = True
                return 0

            def communicate(self, *a, **k):
                launcher.waited = True
                return "", ""

            def poll(self):
                return None

        return Proc()


class FakeGit:
    def __init__(self, *, ancestry: bool = False, gh: str | None = None) -> None:
        self.ancestry = ancestry
        self.gh = gh
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs.get("cwd")))
        if argv[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if len(argv) >= 3 and argv[:3] == ["git", "merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(argv, 0 if self.ancestry else 1, "", "")
        if argv and argv[0] == "gh":
            if self.gh is None:
                return subprocess.CompletedProcess(argv, 1, "", "gh failed")
            return subprocess.CompletedProcess(argv, 0, self.gh, "")
        return subprocess.CompletedProcess(argv, 1, "", "unexpected")


def _env(tmp_path, *, launcher=None, git_runner=None, which=None, adapter=None, **settings):
    env = Env(adapter, **settings)
    launcher = launcher or FakeLauncher()
    env.launcher = launcher
    env.engine = SyncEngine(
        env.adapter, env.kanban, env.state,
        {"tracker": "fake", "state_map": STATE_MAP, "max_active": 0, **settings},
        clock=env.clock,
        land_launcher=launcher,
        git_runner=git_runner,
        land_dir=tmp_path,
        which=which,
    )
    return env


def primed(env, key="FAKE-1", *, status="done", workspace=".", branch="feat/x", summary="All green"):
    (card,) = linked(env, key, status=status)
    env.kanban.cards[card]["workspace_path"] = workspace
    env.kanban.cards[card]["branch_name"] = branch
    if summary is not None:
        env.kanban.set_summary(card, summary)
    return card


def landing(env, key="FAKE-1"):
    return env.state.link(f"fake:{key}").get("landing") or {}


def write_exit(env, code, stdout="", stderr="", key="FAKE-1"):
    path = Path(landing(env, key)["result_file"])
    write_result(path, {
        "exit": code, "stdout_tail": stdout, "stderr_tail": stderr, "finished_at": env.clock(),
    })


def land_comments(env, key="FAKE-1"):
    return [c for c in env.calls("post_comment") if c[1] == key and "could not land it" in c[2]]


def done_moves(env, key="FAKE-1"):
    return [c for c in env.calls("move_issue") if c[1] == key and c[2] == "Done"]


# ── immediate ─────────────────────────────────────────────────────────────


def test_immediate_done_gate_pushes_and_summarises_now():
    env = Env()
    (card_id,) = linked(env, "FAKE-1", status="running")
    env.engine.tick(force=True)
    env.kanban.set_status(card_id, "done")
    env.kanban.set_summary(card_id, "  Fixed the login redirect.  ")

    env.engine.tick(force=True)
    env.engine.tick(force=True)

    assert env.calls("post_comment") == [("post_comment", "FAKE-1", sign("Fixed the login redirect."))]
    assert env.calls("move_issue") == [("move_issue", "FAKE-1", "In Progress"), ("move_issue", "FAKE-1", "Done")]
    note = done_note(Link("fake", "FAKE-1"))
    assert [(c["author"], c["body"]) for c in env.kanban.comments[card_id]] == [("issue-sync", note)]
    assert env.state.link("fake:FAKE-1")["done_comment_posted"] is True
    assert env.engine.done_gate == "immediate"


def test_unset_done_gate_defaults_to_immediate():
    assert Env().engine.done_gate == "immediate"
    assert Env(done_gate="immediate").engine.done_gate == "immediate"
    assert Env(done_gate="nope").engine.done_gate == "immediate"


def test_running_still_pushes_in_command_mode(tmp_path):
    env = _env(tmp_path, done_gate="command", land_command=["/bin/land"])
    (card,) = linked(env, "FAKE-1", status="running")
    env.engine.push_outbound()
    assert env.calls("move_issue") == [("move_issue", "FAKE-1", "In Progress")]
    assert env.launcher.calls == []


# ── command mode ──────────────────────────────────────────────────────────


def test_command_exit_0_pushes_done_and_summary_only_after_landing(tmp_path):
    env = _env(tmp_path, done_gate="command", land_command=["/bin/land"])
    card = primed(env)
    env.engine.push_outbound()
    assert done_moves(env) == []
    assert env.calls("post_comment") == []
    assert landing(env)["status"] == "running"
    assert env.engine.last_landing["FAKE-1"] == "running"
    assert env.launcher.calls and not env.launcher.waited

    write_exit(env, 0, stdout="landed")
    env.engine.push_outbound()
    assert done_moves(env) == [("move_issue", "FAKE-1", "Done")]
    assert env.calls("post_comment") == [("post_comment", "FAKE-1", sign("All green"))]
    assert landing(env)["status"] == "landed"
    note = done_note(Link("fake", "FAKE-1"))
    assert any(c["body"] == note for c in env.kanban.comments[card])


def test_command_exit_1_waits_retry_seconds(tmp_path):
    env = _env(tmp_path, done_gate="command", land_command=["/bin/land"], land_retry_seconds=10)
    primed(env)
    env.engine.push_outbound()
    write_exit(env, 1)
    env.engine.push_outbound()
    assert landing(env)["status"] == "waiting"
    assert done_moves(env) == []
    assert len(env.launcher.calls) == 1

    env.clock.advance(9)
    env.engine.push_outbound()
    assert len(env.launcher.calls) == 1
    env.clock.advance(2)
    env.engine.push_outbound()
    assert len(env.launcher.calls) == 2
    assert landing(env)["status"] == "running"
    assert landing(env)["attempt"] == 2


def test_command_exit_2_posts_one_signed_comment(tmp_path):
    env = _env(tmp_path, done_gate="command", land_command=["/bin/land"])
    primed(env)
    env.engine.push_outbound()
    write_exit(env, 2, stdout="push rejected\nremote hung up", stderr="unused")
    env.engine.push_outbound()
    env.engine.push_outbound()
    comments = land_comments(env)
    assert len(comments) == 1
    body = comments[0][2]
    assert is_signed(body)
    assert "> push rejected" in body and "> remote hung up" in body
    assert "unused" not in body
    assert "Reply here when it's fixed and Hermes will try again." in body
    assert landing(env)["status"] == "failed"
    assert done_moves(env) == []


def test_command_exit_2_uses_stderr_when_stdout_empty(tmp_path):
    env = _env(tmp_path, done_gate="command", land_command=["/bin/land"])
    primed(env)
    env.engine.push_outbound()
    write_exit(env, 2, stdout="  ", stderr="fatal: no remote")
    env.engine.push_outbound()
    body = land_comments(env)[0][2]
    assert "> fatal: no remote" in body


def test_human_reply_requeues_failed_landing_bot_does_not(tmp_path):
    env = _env(tmp_path, done_gate="command", land_command=["/bin/land"])
    card = primed(env)
    env.engine.push_outbound()
    write_exit(env, 2, stdout="boom")
    env.engine.push_outbound()
    assert len(env.launcher.calls) == 1

    env.adapter.post_comment("FAKE-1", sign("still broken"))
    env.engine.push_outbound()
    assert len(env.launcher.calls) == 1
    assert landing(env)["status"] == "failed"

    env.adapter.post_comment("FAKE-1", "I fixed the remote.")
    env.engine.push_outbound()
    assert landing(env)["status"] == "running"
    assert len(env.launcher.calls) == 2
    assert any("I fixed the remote." in c["body"] for c in env.kanban.comments[card])


def test_restart_picks_up_finished_result_file(tmp_path):
    env = _env(tmp_path, done_gate="command", land_command=["/bin/land"])
    primed(env)
    env.engine.push_outbound()
    write_exit(env, 0, stdout="ok")

    other = SyncEngine(
        env.adapter, env.kanban, env.state,
        {"tracker": "fake", "state_map": STATE_MAP, "max_active": 0,
         "done_gate": "command", "land_command": ["/bin/land"]},
        clock=env.clock, land_dir=tmp_path, land_launcher=FakeLauncher(),
    )
    other.push_outbound()
    assert ("move_issue", "FAKE-1", "Done") in env.adapter.calls
    assert env.state.link("fake:FAKE-1")["landing"]["status"] == "landed"
    assert env.state.link("fake:FAKE-1")["done_comment_posted"] is True


def test_timeout_with_no_file_counts_as_failed(tmp_path):
    env = _env(tmp_path, done_gate="command", land_command=["/bin/land"], land_timeout_seconds=10)
    primed(env)
    env.engine.push_outbound()
    assert landing(env)["status"] == "running"
    env.clock.advance(70)
    env.engine.push_outbound()
    assert landing(env)["status"] == "failed"
    comments = land_comments(env)
    assert len(comments) == 1 and "timed out" in comments[0][2]
    assert done_moves(env) == []


def test_missing_land_command_in_command_mode_is_connection_error():
    (conn,) = parse_connections({"tracker": "linear", "done_gate": "command"})
    assert conn.error and "land_command" in conn.error


def test_summary_reaches_file_never_argv_or_env(tmp_path):
    env = _env(tmp_path, done_gate="command", land_command=["/bin/land", "--flag"])
    primed(env, summary=NASTY)
    env.engine.push_outbound()
    argv, kwargs = env.launcher.calls[0]
    env_vars = kwargs["env"]
    summary_path = Path(env_vars["SUMMARY_FILE"])
    assert summary_path.read_text(encoding="utf-8") == NASTY
    assert NASTY not in argv
    assert NASTY not in env_vars.values()
    assert env_vars["ISSUE_KEY"] == "FAKE-1"
    assert env_vars["CARD_ID"]
    assert env_vars["BRANCH"] == "feat/x"
    assert env_vars["WORKTREE"] == "."
    assert "--" in argv and argv[argv.index("--") + 1:] == ["/bin/land", "--flag"]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["close_fds"] is True


def test_tick_returns_without_waiting_for_land_process(tmp_path):
    env = _env(tmp_path, done_gate="command", land_command=["/bin/land"], poll_interval_seconds=0)
    primed(env)
    assert env.engine.tick(force=True) is True
    assert env.launcher.calls and not env.launcher.waited
    assert done_moves(env) == []


def test_other_exit_codes_are_failures(tmp_path):
    env = _env(tmp_path, done_gate="command", land_command=["/bin/land"])
    primed(env)
    env.engine.push_outbound()
    write_exit(env, 99, stdout="weird")
    env.engine.push_outbound()
    assert landing(env)["status"] == "failed"
    assert "> weird" in land_comments(env)[0][2]


def test_failed_landing_counts_as_waiting_on_you(tmp_path):
    env = _env(tmp_path, done_gate="command", land_command=["/bin/land"])
    card = primed(env)
    env.engine.push_outbound()
    write_exit(env, 2, stdout="nope")
    env.engine.push_outbound()
    (conn,) = parse_connections({
        "tracker": "fake", "done_gate": "command", "land_command": ["/bin/land"],
    })
    block = pane.connection_snapshot(conn, env.state, [env.kanban.cards[card]], None, True)
    item = block["items"][0]
    assert item["landing"] == "failed" and item["waiting"] is True
    assert item["landing_error"]
    assert block["counts"]["waiting"] == 1


# ── merged mode ───────────────────────────────────────────────────────────


def test_merged_via_ancestry(tmp_path):
    git = FakeGit(ancestry=True)
    env = _env(tmp_path, done_gate="merged", merged_base="main", git_runner=git, which=lambda n: None)
    primed(env, workspace=str(tmp_path), branch="feat/x")
    env.engine.push_outbound()
    assert ["git", "fetch", "origin"] in [a for a, _ in git.calls]
    assert ["git", "merge-base", "--is-ancestor", "feat/x", "origin/main"] in [a for a, _ in git.calls]
    assert not any(a and a[0] == "gh" for a, _ in git.calls)
    assert done_moves(env) == [("move_issue", "FAKE-1", "Done")]
    assert env.calls("post_comment") == [("post_comment", "FAKE-1", sign("All green"))]
    assert landing(env)["status"] == "landed"


def test_merged_via_gh_merged(tmp_path):
    git = FakeGit(ancestry=False, gh="MERGED")
    env = _env(
        tmp_path, done_gate="merged", merged_base="main", git_runner=git,
        which=lambda n: "/usr/bin/gh" if n == "gh" else None,
    )
    primed(env, workspace=str(tmp_path), branch="feat/x")
    env.engine.push_outbound()
    assert any(a[:4] == ["gh", "pr", "view", "feat/x"] for a, _ in git.calls)
    assert done_moves(env) == [("move_issue", "FAKE-1", "Done")]
    assert landing(env)["status"] == "landed"


def test_merged_waits_when_not_reached(tmp_path):
    git = FakeGit(ancestry=False)
    env = _env(
        tmp_path, done_gate="merged", merged_base="main", land_retry_seconds=10,
        git_runner=git, which=lambda n: None,
    )
    primed(env, workspace=str(tmp_path), branch="feat/x")
    env.engine.push_outbound()
    assert landing(env)["status"] == "waiting"
    assert done_moves(env) == []
    n = len(git.calls)
    env.engine.push_outbound()
    assert len(git.calls) == n
    env.clock.advance(10)
    env.engine.push_outbound()
    assert len(git.calls) > n


def test_merged_without_merged_base_is_connection_error():
    (conn,) = parse_connections({"tracker": "linear", "done_gate": "merged"})
    assert conn.error and "merged_base" in conn.error


def test_done_gate_settings_inherit_and_override():
    conns = parse_connections({
        "done_gate": "command", "land_command": "./land", "land_timeout_seconds": 30,
        "land_retry_seconds": 9,
        "connections": [
            {"id": "a", "tracker": "linear", "projects": ["P"]},
            {"id": "b", "tracker": "linear", "projects": ["Q"],
             "done_gate": "merged", "merged_base": "main"},
        ],
    })
    assert conns[0].error is None and conns[0].settings["land_command"] == "./land"
    assert conns[0].settings["land_timeout_seconds"] == 30
    assert conns[1].error is None and conns[1].settings["done_gate"] == "merged"
    assert conns[1].settings["merged_base"] == "main"


def test_land_timeout_default_and_invalid():
    env = Env()
    assert env.engine.land_timeout == 1800
    assert env.engine.land_retry == 300
    env = Env(land_timeout_seconds="nope", land_retry_seconds=-1)
    assert env.engine.land_timeout == 1800
    assert env.engine.land_retry == 300


# ── land_runner ───────────────────────────────────────────────────────────


def test_land_runner_subprocess_writes_result(tmp_path):
    script = tmp_path / "cmd.py"
    script.write_text("import sys\nprint('out-line')\nprint('err-line', file=sys.stderr)\nsys.exit(0)\n")
    result = tmp_path / "r.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(result), "--timeout", "30", "--",
         sys.executable, str(script)],
        check=False, capture_output=True, text=True,
    )
    assert proc.returncode == 0
    data = json.loads(result.read_text(encoding="utf-8"))
    assert data["exit"] == 0
    assert "out-line" in data["stdout_tail"]
    assert "err-line" in data["stderr_tail"]
    assert "finished_at" in data
    assert read_result(result)["exit"] == 0


def test_land_runner_records_nonzero_exit(tmp_path):
    script = tmp_path / "cmd.py"
    script.write_text("import sys\nprint('nope')\nsys.exit(1)\n")
    result = tmp_path / "r.json"
    subprocess.run(
        [sys.executable, str(SCRIPT), str(result), "--timeout", "30", "--",
         sys.executable, str(script)],
        check=False, capture_output=True, text=True,
    )
    assert json.loads(result.read_text(encoding="utf-8"))["exit"] == 1
