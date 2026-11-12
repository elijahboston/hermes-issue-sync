"""hermes issues setup (CORE-634): connection writer and starter scripts."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import yaml

from issue_sync.setup import TEMPLATES, run_setup, write_scripts_for


def _args(**kwargs):
    defaults = dict(
        id=None, tracker=None, api_key_env=None, base_url_env=None, email_env=None,
        token_env=None, project=None, board=None, assignee=None, label=None,
        admit=None, max_active=None, leaf_only=False, exclude_label=None,
        state_map=None, landing=None, base_branch=None, prepare=None, repo=None,
        yes=False, force=False, print_only=False, enable=False,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _io(tmp_path: Path, *, initial: dict | None = None):
    config = tmp_path / "config.yaml"
    data_dir = tmp_path / "data"
    if initial is not None:
        config.write_text(yaml.safe_dump(initial, sort_keys=False), encoding="utf-8")

    def read(path: Path):
        target = Path(path)
        if not target.exists():
            return {}
        return yaml.safe_load(target.read_text(encoding="utf-8")) or {}

    def write(path: Path, data):
        Path(path).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    return config, data_dir, read, write


def _run(tmp_path: Path, args, *, initial=None, input_fn=None, check_fn=None, dry_runs=None):
    config, data_dir, read, write = _io(tmp_path, initial=initial)
    lines: list[str] = []
    calls = []

    def print_fn(*a, **_k):
        lines.append(" ".join(str(x) for x in a))

    def on_dry(entry):
        calls.append(entry)

    def boom(prompt):
        raise AssertionError(f"input() was called: {prompt!r}")

    code = run_setup(
        args,
        input_fn=input_fn or boom,
        print_fn=print_fn,
        data_dir=data_dir,
        config_path=config,
        read_config=read,
        write_config=write,
        check_fn=check_fn if check_fn is not None else (lambda *_: None),
        dry_run_fn=dry_runs if dry_runs is not None else on_dry,
    )
    data = read(config) if config.exists() else {}
    return code, data, data_dir, lines, calls


def _conn(data: dict, conn_id: str = "lin") -> dict:
    settings = data["plugins"]["entries"]["hermes-issue-sync"]["settings"]
    return next(c for c in settings["connections"] if c["id"] == conn_id)


def test_yes_with_flags_writes_connection(tmp_path):
    code, data, data_dir, lines, calls = _run(tmp_path, _args(
        yes=True, id="lin", tracker="linear", project=["Sandbox"], board="ops",
        assignee="worker", label="agent", admit="label", max_active=2,
        leaf_only=True, exclude_label="hold",
        state_map=["running=In Progress", "done=Done"],
        landing="none",
    ))
    assert code == 0
    conn = _conn(data)
    assert conn["id"] == "lin"
    assert conn["tracker"] == "linear"
    assert conn["projects"] == ["Sandbox"]
    assert conn["board"] == "ops"
    assert conn["assignee"] == "worker"
    assert conn["label"] == "agent"
    assert conn["admit"] == "label"
    assert conn["max_active"] == 2
    assert conn["leaf_only"] is True
    assert conn["exclude_label"] == "hold"
    assert conn["state_map"] == {"running": "In Progress", "done": "Done"}
    assert conn["done_gate"] == "immediate"
    assert conn["api_key_env"] == "LINEAR_API_KEY"
    assert "land_command" not in conn
    assert calls and calls[0]["id"] == "lin"
    assert not (data_dir / "hooks").exists()


def test_rerun_same_id_refuses_without_force(tmp_path):
    args = _args(yes=True, id="lin", tracker="linear", project=["A"])
    code, data, *_ = _run(tmp_path, args)
    assert code == 0 and _conn(data)["projects"] == ["A"]
    code, data, _, lines, _ = _run(tmp_path, args, initial=data)
    assert code == 1
    assert "already exists" in " ".join(lines)
    assert _conn(data)["projects"] == ["A"]
    args.force = True
    args.project = ["B"]
    code, data, *_ = _run(tmp_path, args, initial=data)
    assert code == 0 and _conn(data)["projects"] == ["B"]


def test_other_config_keys_preserved(tmp_path):
    initial = {
        "keep": True,
        "plugins": {
            "entries": {
                "other": {"settings": {"x": 1}},
                "hermes-issue-sync": {"settings": {"board": "shared", "enabled": True, "connections": []}},
            }
        },
    }
    code, data, *_ = _run(
        tmp_path, _args(yes=True, id="lin", tracker="jira", project=["TSS"]), initial=initial,
    )
    assert code == 0
    assert data["keep"] is True
    assert data["plugins"]["entries"]["other"]["settings"]["x"] == 1
    settings = data["plugins"]["entries"]["hermes-issue-sync"]["settings"]
    assert settings["board"] == "shared"
    assert settings["enabled"] is True
    assert settings["connections"][0]["tracker"] == "jira"
    assert settings["connections"][0]["base_url_env"] == "JIRA_BASE_URL"
    assert settings["connections"][0]["email_env"] == "JIRA_EMAIL"
    assert settings["connections"][0]["token_env"] == "JIRA_API_TOKEN"


def test_print_only_writes_nothing(tmp_path):
    code, data, data_dir, lines, calls = _run(tmp_path, _args(
        yes=True, id="lin", tracker="linear", landing="merge-to-main", print_only=True,
    ))
    assert code == 0
    assert data == {}
    assert not (data_dir / "hooks").exists()
    assert calls == []
    dumped = "\n".join(lines)
    assert "id: lin" in dumped and "tracker: linear" in dumped
    assert "done_gate: command" in dumped


def test_secrets_never_requested(tmp_path):
    def boom(prompt):
        raise AssertionError(f"input() requested: {prompt!r}")

    checks = []

    def check(tracker, settings):
        checks.append((tracker, dict(settings)))
        raise RuntimeError("probe failed")

    code, _, _, lines, _ = _run(
        tmp_path,
        _args(yes=True, id="lin", tracker="linear"),
        input_fn=boom,
        check_fn=check,
    )
    assert code == 0
    assert checks and checks[0][0] == "linear"
    assert "api_key_env" in checks[0][1]
    assert "warning" in " ".join(lines).lower()
    assert "probe failed" in " ".join(lines)


def test_yes_missing_id_is_error(tmp_path):
    code, data, _, lines, _ = _run(tmp_path, _args(yes=True, tracker="linear"))
    assert code == 1 and data == {}
    assert "--id" in " ".join(lines)


def test_admit_all_requires_project(tmp_path):
    code, _, _, lines, _ = _run(tmp_path, _args(yes=True, id="lin", tracker="linear", admit="all"))
    assert code == 1
    assert "project" in " ".join(lines).lower()


def test_disabled_plugin_is_left_disabled_unless_enable(tmp_path):
    initial = {"plugins": {"entries": {"hermes-issue-sync": {"settings": {"enabled": False}}}}}
    code, data, _, lines, _ = _run(
        tmp_path, _args(yes=True, id="lin", tracker="linear"), initial=initial,
    )
    assert code == 0
    settings = data["plugins"]["entries"]["hermes-issue-sync"]["settings"]
    assert settings["enabled"] is False
    assert "hermes issues setup --enable" in " ".join(lines)
    code, data, *_ = _run(
        tmp_path, _args(yes=True, id="lin", tracker="linear", enable=True, force=True), initial=data,
    )
    assert data["plugins"]["entries"]["hermes-issue-sync"]["settings"]["enabled"] is True


def test_scripts_mode_755_and_not_overwritten_without_force(tmp_path):
    args = _args(yes=True, id="lin", tracker="linear", landing="merge-to-main", prepare="custom")
    code, data, data_dir, lines, _ = _run(tmp_path, args)
    assert code == 0
    land = data_dir / "hooks" / "lin" / "land.sh"
    prep = data_dir / "hooks" / "lin" / "prepare.sh"
    assert stat.S_IMODE(land.stat().st_mode) == 0o755
    assert stat.S_IMODE(prep.stat().st_mode) == 0o755
    assert _conn(data)["land_command"] == str(land)
    assert _conn(data)["prepare_command"] == str(prep)
    land.write_text("OLD\n", encoding="utf-8")
    kept = write_scripts_for(
        {"id": "lin", "landing": "merge-to-main", "prepare": "none", "base_branch": "main", "repo": ""},
        {"land": land, "prepare": None},
        force=False,
    )
    assert kept and land.read_text(encoding="utf-8") == "OLD\n"
    write_scripts_for(
        {"id": "lin", "landing": "merge-to-main", "prepare": "none", "base_branch": "develop", "repo": ""},
        {"land": land, "prepare": None},
        force=True,
    )
    text = land.read_text(encoding="utf-8")
    assert 'base="develop"' in text and "OLD" not in text
    assert "{{" not in text


def test_templates_syntax_and_no_eval():
    for name in ("land_merge_to_main.sh", "land_pr_automerge.sh", "land_custom.sh",
                 "prepare_worktree.sh", "prepare_custom.sh"):
        path = TEMPLATES / name
        text = path.read_text(encoding="utf-8")
        assert "eval" not in text
        assert "set -eu" in text
        proc = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
    pr = (TEMPLATES / "land_pr_automerge.sh").read_text(encoding="utf-8")
    assert '--body-file "$SUMMARY_FILE"' in pr
    assert "$(cat" not in pr and "`cat" not in pr


def _git(cwd: Path, *args, check=True):
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "setup-test",
        "GIT_AUTHOR_EMAIL": "setup@test.invalid",
        "GIT_COMMITTER_NAME": "setup-test",
        "GIT_COMMITTER_EMAIL": "setup@test.invalid",
        "GIT_TERMINAL_PROMPT": "0",
    })
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check, env=env, capture_output=True, text=True,
    )


def _repo_with_origin(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "setup@test.invalid")
    _git(repo, "config", "user.name", "setup-test")
    (repo / "file.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "one")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    return origin, repo


def _land(tmp_path: Path, name: str, **replacements) -> Path:
    dest = tmp_path / "land.sh"
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace("{{" + key + "}}", value)
    dest.write_text(text, encoding="utf-8")
    dest.chmod(0o755)
    return dest


def test_merge_to_main_fast_forward(tmp_path):
    _origin, repo = _repo_with_origin(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "file.txt").write_text("one\nfeature\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "feature")
    script = _land(tmp_path, "land_merge_to_main.sh", base_branch="main")
    env = os.environ.copy()
    env.update({
        "WORKTREE": str(repo), "BRANCH": "feat", "ISSUE_KEY": "A-1",
        "ISSUE_URL": "", "CARD_ID": "t_1", "SUMMARY_FILE": "",
        "GIT_TERMINAL_PROMPT": "0",
    })
    proc = subprocess.run(["sh", str(script)], cwd=repo, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_merge_to_main_conflict_exits_2(tmp_path):
    origin, side = _repo_with_origin(tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(origin), str(other)], check=True, capture_output=True)
    _git(other, "config", "user.email", "setup@test.invalid")
    _git(other, "config", "user.name", "setup-test")
    (other / "file.txt").write_text("mainline\n", encoding="utf-8")
    _git(other, "add", "file.txt")
    _git(other, "commit", "-m", "mainline")
    _git(other, "push", "origin", "main")
    _git(side, "checkout", "-b", "feat")
    (side / "file.txt").write_text("side\n", encoding="utf-8")
    _git(side, "add", "file.txt")
    _git(side, "commit", "-m", "side")
    script = _land(tmp_path, "land_merge_to_main.sh", base_branch="main")
    env = os.environ.copy()
    env.update({
        "WORKTREE": str(side), "BRANCH": "feat", "ISSUE_KEY": "A-1",
        "ISSUE_URL": "", "CARD_ID": "t_1", "SUMMARY_FILE": "",
        "GIT_TERMINAL_PROMPT": "0",
    })
    proc = subprocess.run(["sh", str(script)], cwd=side, env=env, capture_output=True, text=True)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stdout.strip()


def test_pr_automerge_merged_and_failed_check(tmp_path):
    _origin, repo = _repo_with_origin(tmp_path)
    script = _land(tmp_path, "land_pr_automerge.sh", base_branch="main")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "open(os.environ['GH_LOG'], 'a').write(' '.join(args) + '\\n')\n"
        "mode = os.environ.get('GH_MODE', 'merged')\n"
        "if args[:2] == ['pr', 'view']:\n"
        "    if '-q' in args:\n"
        "        q = args[args.index('-q') + 1]\n"
        "        if q == '.state':\n"
        "            print('MERGED' if mode == 'merged' else 'OPEN')\n"
        "            sys.exit(0)\n"
        "        if 'FAILURE' in q:\n"
        "            print('' if mode == 'merged' else 'ci')\n"
        "            sys.exit(0)\n"
        "    json.dump({'state': 'OPEN' if mode != 'merged' else 'MERGED',\n"
        "               'statusCheckRollup': [] if mode == 'merged' else\n"
        "               [{'name': 'ci', 'conclusion': 'FAILURE'}]}, sys.stdout)\n"
        "    sys.exit(0)\n"
        "if args[:2] in (['pr', 'create'], ['pr', 'merge']):\n"
        "    sys.exit(0)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    log = tmp_path / "gh.log"
    env = os.environ.copy()
    env.update({
        "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
        "WORKTREE": str(repo), "BRANCH": "feat", "ISSUE_KEY": "A-1",
        "ISSUE_URL": "", "CARD_ID": "t_1", "SUMMARY_FILE": str(tmp_path / "summary.txt"),
        "GH_LOG": str(log), "GH_MODE": "merged", "GIT_TERMINAL_PROMPT": "0",
    })
    (tmp_path / "summary.txt").write_text("$(reboot)\n", encoding="utf-8")
    proc = subprocess.run(["sh", str(script)], cwd=repo, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    env["GH_MODE"] = "fail"
    proc = subprocess.run(["sh", str(script)], cwd=repo, env=env, capture_output=True, text=True)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "FAILURE" in proc.stdout
    logged = log.read_text(encoding="utf-8")
    assert "$(reboot)" not in logged
