"""``hermes issues setup``: write a connection and starter land/prepare scripts."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

from .adapters import get_adapter
from .connections import parse_connections
from .pane import data_dir as plugin_data_dir

TEMPLATES = Path(__file__).resolve().parent / "templates"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_BASE_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]+$")

LINEAR_STATE_MAP = {"running": "In Progress", "review": "In Review", "done": "Done"}
JIRA_STATE_MAP = {"running": "In Progress", "done": "Done"}
LANDING_CHOICES = ("none", "merge-to-main", "pr-automerge", "custom")
PREPARE_CHOICES = ("none", "worktree", "custom")
TRACKERS = ("linear", "jira")
DEFAULT_LABEL = "agent"
DEFAULT_MAX_ACTIVE = 1
DEFAULT_BASE = "main"
ENABLE_HINT = "hermes issues setup --enable"

LAND_TEMPLATES = {
    "merge-to-main": "land_merge_to_main.sh",
    "pr-automerge": "land_pr_automerge.sh",
    "custom": "land_custom.sh",
}
PREPARE_TEMPLATES = {
    "worktree": "prepare_worktree.sh",
    "custom": "prepare_custom.sh",
}

PrintFn = Callable[..., None]
InputFn = Callable[[str], str]
CheckFn = Callable[[str, Mapping[str, Any]], None]
DryRunFn = Callable[[dict[str, Any]], None]


class SetupError(Exception):
    """A user-facing setup failure. ``str(exc)`` is printed as-is."""


def add_parser(subs: Any) -> Any:
    p = subs.add_parser("setup", help="Write a connection and starter land/prepare scripts")
    p.add_argument("--id", help="Connection id (lowercase letters, digits, '-' or '_')")
    p.add_argument("--tracker", choices=TRACKERS, help="Issue tracker")
    p.add_argument("--api-key-env", dest="api_key_env", default=None,
                   help="Env var name for the Linear API key (default LINEAR_API_KEY)")
    p.add_argument("--base-url-env", dest="base_url_env", default=None,
                   help="Env var name for the Jira site URL (default JIRA_BASE_URL)")
    p.add_argument("--email-env", dest="email_env", default=None,
                   help="Env var name for the Jira account email (default JIRA_EMAIL)")
    p.add_argument("--token-env", dest="token_env", default=None,
                   help="Env var name for the Jira API token (default JIRA_API_TOKEN)")
    p.add_argument("--project", action="append", dest="project", default=None,
                   help="Tracker project (repeatable)")
    p.add_argument("--board", default=None, help="Kanban board slug (empty = default board)")
    p.add_argument("--assignee", default=None, help="Hermes profile assigned to new cards")
    p.add_argument("--label", default=None, help="Admission label (default agent)")
    p.add_argument("--admit", choices=("label", "all"), default=None,
                   help="Admit labelled issues only, or every open issue in the projects")
    p.add_argument("--max-active", dest="max_active", type=int, default=None,
                   help="Active-card cap (default 1; 0 = unlimited)")
    p.add_argument("--leaf-only", dest="leaf_only", action="store_true", default=False,
                   help="Skip issues that still have open children")
    p.add_argument("--exclude-label", dest="exclude_label", default=None,
                   help="Never admit issues carrying this label")
    p.add_argument("--state-map", action="append", dest="state_map", default=None,
                   metavar="STATUS=NAME", help="Kanban status to tracker state (repeatable)")
    p.add_argument("--landing", choices=LANDING_CHOICES, default=None,
                   help="How a done card lands (default none = immediate Done)")
    p.add_argument("--base-branch", dest="base_branch", default=None,
                   help="Base branch for land/prepare scripts (default main)")
    p.add_argument("--prepare", choices=PREPARE_CHOICES, default=None,
                   help="Prepare hook (default none)")
    p.add_argument("--repo", default=None, help="Git checkout used by --prepare worktree")
    p.add_argument("--yes", action="store_true", default=False,
                   help="Non-interactive; missing required values are errors")
    p.add_argument("--force", action="store_true", default=False,
                   help="Replace an existing connection id and overwrite existing scripts")
    p.add_argument("--print-only", dest="print_only", action="store_true", default=False,
                   help="Print the YAML that would be written; write nothing")
    p.add_argument("--enable", action="store_true", default=False,
                   help="Set the plugin's enabled flag to true")
    return p


def run_setup(
    args: Any,
    *,
    input_fn: InputFn = input,
    print_fn: PrintFn = print,
    stderr_fn: PrintFn | None = None,
    data_dir: Path | None = None,
    config_path: Path | None = None,
    read_config: Callable[[Path], Any] | None = None,
    write_config: Callable[[Path, Any], None] | None = None,
    check_fn: CheckFn | None = None,
    dry_run_fn: DryRunFn | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Gather a connection, write scripts + config, then dry-run. Never stores secrets."""
    err = stderr_fn or (lambda *a, **k: print_fn(*a, **k))
    try:
        answers = gather(args, input_fn=input_fn, print_fn=print_fn)
        _check_credentials(answers, check_fn=check_fn, print_fn=print_fn, input_fn=input_fn,
                           yes=bool(getattr(args, "yes", False)))
        root = _data_root(data_dir)
        if (
            not getattr(args, "print_only", False)
            and (answers["landing"] != "none" or answers["prepare"] != "none")
            and root is None
        ):
            raise SetupError("plugin data dir is unavailable; cannot write starter scripts")
        scripts = _plan_scripts(answers, root)
        entry = _connection_entry(answers, scripts)
        if getattr(args, "print_only", False):
            print_fn(_dump_yaml({"connections": [entry]}))
            return 0
        kept = write_scripts_for(answers, scripts, force=bool(getattr(args, "force", False)))
        for name in kept:
            print_fn(f"kept existing {name} (pass --force to overwrite)")
        path, reader, writer = _config_io(config_path, read_config, write_config)
        enabled_after = _write_config(
            path, reader, writer, entry,
            force=bool(getattr(args, "force", False)),
            enable=bool(getattr(args, "enable", False)),
            yes=bool(getattr(args, "yes", False)),
            input_fn=input_fn,
        )
        print_fn(f"wrote connection {entry['id']} to {path}")
        if enabled_after is False:
            print_fn(
                f"the plugin is disabled (`plugins.entries.hermes-issue-sync.settings.enabled` "
                f"is false). Enable it with: {ENABLE_HINT}"
            )
        _run_followup_dry_run(entry, dry_run_fn=dry_run_fn, print_fn=print_fn, err=err)
        return 0
    except SetupError as exc:
        err(f"issue-sync setup: {exc}")
        return 1


def gather(args: Any, *, input_fn: InputFn, print_fn: PrintFn) -> dict[str, Any]:
    yes = bool(getattr(args, "yes", False))
    conn_id = _ask(args, "id", "Connection id (lowercase)", None, yes=yes, input_fn=input_fn,
                   required=True, transform=lambda s: s.strip().lower())
    if not _ID_RE.match(conn_id):
        raise SetupError("`id` must be lowercase letters, digits, '-' or '_'")
    tracker = _ask(args, "tracker", "Tracker (linear/jira)", None, yes=yes, input_fn=input_fn,
                   required=True, transform=lambda s: s.strip().lower(), choices=TRACKERS)

    creds = _ask_credentials(args, tracker, yes=yes, input_fn=input_fn)
    projects = _ask_projects(args, yes=yes, input_fn=input_fn)
    board = _ask(args, "board", "Kanban board slug (empty = default)", "", yes=yes,
                 input_fn=input_fn, required=False)
    assignee = _ask(args, "assignee", "Assignee profile", "", yes=yes, input_fn=input_fn,
                    required=False)
    label = _ask(args, "label", "Admission label", DEFAULT_LABEL, yes=yes, input_fn=input_fn,
                 required=False) or DEFAULT_LABEL
    admit = _ask(args, "admit", "Admit (label/all)", "label", yes=yes, input_fn=input_fn,
                 required=False, transform=lambda s: s.strip().lower(),
                 choices=("label", "all")) or "label"
    if admit == "all" and not projects:
        raise SetupError("admit: all requires at least one project")
    max_active = _ask_int(args, "max_active", "Max active cards", DEFAULT_MAX_ACTIVE,
                          yes=yes, input_fn=input_fn)
    leaf_only = bool(getattr(args, "leaf_only", False))
    if not yes and not leaf_only:
        leaf_only = _ask_bool("Skip issues that still have open children?", False, input_fn)
    exclude_label = _ask(args, "exclude_label", "Exclude label", "", yes=yes, input_fn=input_fn,
                         required=False)
    state_map = _ask_state_map(args, tracker, yes=yes, input_fn=input_fn)
    landing = _ask(args, "landing", "Landing (none/merge-to-main/pr-automerge/custom)", "none",
                   yes=yes, input_fn=input_fn, required=False,
                   transform=lambda s: s.strip().lower(), choices=LANDING_CHOICES) or "none"
    base_branch = _ask(args, "base_branch", "Base branch", DEFAULT_BASE, yes=yes,
                       input_fn=input_fn, required=False) or DEFAULT_BASE
    if not _BASE_RE.match(base_branch):
        raise SetupError("base branch must be a simple git ref (letters, digits, '.', '_', '/', '-')")
    prepare = _ask(args, "prepare", "Prepare (none/worktree/custom)", "none", yes=yes,
                   input_fn=input_fn, required=False, transform=lambda s: s.strip().lower(),
                   choices=PREPARE_CHOICES) or "none"
    repo = ""
    if prepare == "worktree":
        repo = _ask(args, "repo", "Git repo path for worktrees", None, yes=yes,
                    input_fn=input_fn, required=True)
        repo = str(Path(repo).expanduser())
        if "\n" in repo:
            raise SetupError("repo path must be a single line")
    return {
        "id": conn_id, "tracker": tracker, **creds,
        "projects": projects, "board": board or "", "assignee": assignee or "",
        "label": label, "admit": admit, "max_active": max_active,
        "leaf_only": leaf_only, "exclude_label": exclude_label or "",
        "state_map": state_map, "landing": landing, "base_branch": base_branch,
        "prepare": prepare, "repo": repo,
    }


def _ask_credentials(args: Any, tracker: str, *, yes: bool, input_fn: InputFn) -> dict[str, str]:
    if tracker == "linear":
        name = _ask(args, "api_key_env", "Linear API key env var name", "LINEAR_API_KEY",
                    yes=yes, input_fn=input_fn, required=False) or "LINEAR_API_KEY"
        return {"api_key_env": name}
    base = _ask(args, "base_url_env", "Jira site URL env var name", "JIRA_BASE_URL",
                yes=yes, input_fn=input_fn, required=False) or "JIRA_BASE_URL"
    email = _ask(args, "email_env", "Jira email env var name", "JIRA_EMAIL",
                 yes=yes, input_fn=input_fn, required=False) or "JIRA_EMAIL"
    token = _ask(args, "token_env", "Jira API token env var name", "JIRA_API_TOKEN",
                 yes=yes, input_fn=input_fn, required=False) or "JIRA_API_TOKEN"
    return {"base_url_env": base, "email_env": email, "token_env": token}


def _ask_projects(args: Any, *, yes: bool, input_fn: InputFn) -> list[str]:
    raw = getattr(args, "project", None)
    if raw:
        items = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        return _dedupe([str(p).strip() for p in items])
    if yes:
        return []
    text = input_fn("Projects (comma-separated) []: ").strip()
    return _dedupe([p.strip() for p in text.split(",")]) if text else []


def _ask_state_map(args: Any, tracker: str, *, yes: bool, input_fn: InputFn) -> dict[str, str]:
    default = dict(LINEAR_STATE_MAP if tracker == "linear" else JIRA_STATE_MAP)
    raw = getattr(args, "state_map", None)
    if raw:
        return _parse_state_map(raw)
    if yes:
        return default
    shown = ", ".join(f"{k}={v}" for k, v in default.items())
    text = input_fn(f"State map (status=Name, comma-separated) [{shown}]: ").strip()
    return _parse_state_map(text.split(",")) if text else default


def _parse_state_map(items: Sequence[Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        if "=" not in text:
            raise SetupError(f"state-map {text!r} must be STATUS=Name")
        status, name = text.split("=", 1)
        status, name = status.strip(), name.strip()
        if not status or not name or not _STATE_RE.match(status):
            raise SetupError(f"state-map {text!r} is not a valid STATUS=Name pair")
        out[status] = name
    return out


def _ask(
    args: Any, attr: str, prompt: str, default: str | None, *, yes: bool, input_fn: InputFn,
    required: bool, transform: Callable[[str], str] | None = None, choices: Sequence[str] | None = None,
) -> str:
    value = getattr(args, attr, None)
    if value not in (None, ""):
        text = str(value).strip()
    elif yes:
        if required and default is None:
            raise SetupError(f"--{attr.replace('_', '-')} is required with --yes")
        text = "" if default is None else str(default)
    else:
        suffix = f" [{default}]" if default not in (None, "") else ""
        text = input_fn(f"{prompt}{suffix}: ").strip()
        if not text:
            text = "" if default is None else str(default)
    if transform:
        text = transform(text)
    if required and not text:
        raise SetupError(f"{attr.replace('_', '-')} is required")
    if choices and text and text not in choices:
        raise SetupError(f"{attr.replace('_', '-')} must be one of {', '.join(choices)}")
    return text


def _ask_int(args: Any, attr: str, prompt: str, default: int, *, yes: bool, input_fn: InputFn) -> int:
    value = getattr(args, attr, None)
    if value not in (None, ""):
        text = str(value).strip()
    elif yes:
        return default
    else:
        text = input_fn(f"{prompt} [{default}]: ").strip()
        if not text:
            return default
    try:
        number = int(text)
    except (TypeError, ValueError) as exc:
        raise SetupError(f"{attr.replace('_', '-')} must be an integer") from exc
    if number < 0:
        raise SetupError(f"{attr.replace('_', '-')} must be >= 0")
    return number


def _ask_bool(prompt: str, default: bool, input_fn: InputFn) -> bool:
    hint = "Y/n" if default else "y/N"
    text = input_fn(f"{prompt} [{hint}]: ").strip().lower()
    if not text:
        return default
    return text in ("y", "yes")


def _check_credentials(
    answers: Mapping[str, Any], *, check_fn: CheckFn | None, print_fn: PrintFn,
    input_fn: InputFn, yes: bool,
) -> None:
    settings = _adapter_settings(answers)
    tracker = answers["tracker"]
    print_fn(f"checking {tracker} credentials via env var names (values are never printed)...")
    try:
        if check_fn is not None:
            check_fn(tracker, settings)
        else:
            get_adapter(tracker, settings).check()
    except Exception as exc:
        print_fn(f"warning: {tracker} check failed: {exc}")
        if not yes and not _ask_bool("Continue anyway?", True, input_fn):
            raise SetupError("aborted after a failed credential check") from exc
        return
    print_fn("check: ok")


def _adapter_settings(answers: Mapping[str, Any]) -> dict[str, Any]:
    settings: dict[str, Any] = {"tracker": answers["tracker"], "filter": {}}
    if answers.get("projects"):
        settings["filter"]["projects"] = list(answers["projects"])
    if answers.get("label"):
        settings["filter"]["label"] = answers["label"]
    for key in ("api_key_env", "base_url_env", "email_env", "token_env"):
        if answers.get(key):
            settings[key] = answers[key]
    return settings


def _data_root(injected: Path | None) -> Path | None:
    if injected is not None:
        return Path(injected)
    return plugin_data_dir()


def _plan_scripts(answers: Mapping[str, Any], root: Path | None) -> dict[str, Path | None]:
    landing, prepare = answers["landing"], answers["prepare"]
    if landing == "none" and prepare == "none":
        return {"land": None, "prepare": None}
    if root is None:
        return {"land": None, "prepare": None}
    folder = root / "hooks" / answers["id"]
    land = folder / "land.sh" if landing != "none" else None
    prep = folder / "prepare.sh" if prepare != "none" else None
    return {"land": land, "prepare": prep}


def _connection_entry(answers: Mapping[str, Any], scripts: Mapping[str, Path | None]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": answers["id"],
        "tracker": answers["tracker"],
        "label": answers["label"],
        "admit": answers["admit"],
        "max_active": answers["max_active"],
    }
    if answers["projects"]:
        entry["projects"] = list(answers["projects"])
    if answers["board"]:
        entry["board"] = answers["board"]
    if answers["assignee"]:
        entry["assignee"] = answers["assignee"]
    if answers["leaf_only"]:
        entry["leaf_only"] = True
    if answers["exclude_label"]:
        entry["exclude_label"] = answers["exclude_label"]
    if answers["state_map"]:
        entry["state_map"] = dict(answers["state_map"])
    for key in ("api_key_env", "base_url_env", "email_env", "token_env"):
        if answers.get(key):
            entry[key] = answers[key]
    landing = answers["landing"]
    if landing == "none":
        entry["done_gate"] = "immediate"
    else:
        entry["done_gate"] = "command"
        land = scripts.get("land")
        entry["land_command"] = str(land) if land is not None else f"<plugin-data>/hooks/{answers['id']}/land.sh"
    prepare = answers["prepare"]
    if prepare != "none":
        prep = scripts.get("prepare")
        entry["prepare_command"] = (
            str(prep) if prep is not None else f"<plugin-data>/hooks/{answers['id']}/prepare.sh"
        )
    return entry



def write_scripts_for(answers: Mapping[str, Any], scripts: Mapping[str, Path | None], *,
                      force: bool) -> list[str]:
    kept: list[str] = []
    replacements = {
        "base_branch": answers["base_branch"],
        "repo": answers.get("repo") or "",
    }
    mapping = (
        ("land", answers["landing"], LAND_TEMPLATES),
        ("prepare", answers["prepare"], PREPARE_TEMPLATES),
    )
    for kind, choice, templates in mapping:
        path = scripts.get(kind)
        template = templates.get(choice)
        if path is None or template is None:
            continue
        if path.exists() and not force:
            kept.append(str(path))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render(template, replacements), encoding="utf-8")
        os.chmod(path, 0o755)
    return kept


def _render(name: str, replacements: Mapping[str, str]) -> str:
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def _write_config(
    path: Path,
    reader: Callable[[Path], Any],
    writer: Callable[[Path, Any], None],
    entry: Mapping[str, Any],
    *,
    force: bool,
    enable: bool,
    yes: bool,
    input_fn: InputFn,
) -> bool | None:
    data = reader(path)
    if data in (None, ""):
        data = {}
    if not isinstance(data, dict):
        raise SetupError(f"{path} is not a YAML mapping")
    settings = _ensure_settings(data)
    conns = settings.get("connections")
    if conns is None:
        settings["connections"] = [dict(entry)]
    elif not isinstance(conns, list):
        raise SetupError("`connections` must be a list")
    else:
        needle = str(entry["id"]).lower()
        index = next(
            (i for i, raw in enumerate(conns)
             if isinstance(raw, Mapping) and str(raw.get("id") or "").strip().lower() == needle),
            None,
        )
        if index is None:
            conns.append(dict(entry))
        else:
            if not force:
                if yes or not _ask_bool(
                    f"Connection {entry['id']!r} already exists. Replace it?", False, input_fn,
                ):
                    raise SetupError(
                        f"connection {entry['id']!r} already exists (pass --force to replace)"
                    )
            conns[index] = dict(entry)
    if enable:
        settings["enabled"] = True
    writer(path, data)
    value = settings.get("enabled", True)
    if value in (None, ""):
        return True
    return bool(value)


def _ensure_settings(data: dict[str, Any]) -> dict[str, Any]:
    plugins = data.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise SetupError("`plugins` must be a mapping")
    entries = plugins.setdefault("entries", {})
    if not isinstance(entries, dict):
        raise SetupError("`plugins.entries` must be a mapping")
    plugin = entries.setdefault("hermes-issue-sync", {})
    if not isinstance(plugin, dict):
        raise SetupError("`plugins.entries.hermes-issue-sync` must be a mapping")
    settings = plugin.setdefault("settings", {})
    if not isinstance(settings, dict):
        raise SetupError("`plugins.entries.hermes-issue-sync.settings` must be a mapping")
    return settings


def _config_io(
    path: Path | None,
    reader: Callable[[Path], Any] | None,
    writer: Callable[[Path, Any], None] | None,
) -> tuple[Path, Callable[[Path], Any], Callable[[Path, Any], None]]:
    if path is not None and reader is not None and writer is not None:
        return Path(path), reader, writer
    from hermes_cli.config import atomic_config_write, get_config_path, read_user_config_raw
    return Path(get_config_path()), read_user_config_raw, atomic_config_write


def _dump_yaml(block: Mapping[str, Any]) -> str:
    import yaml
    return yaml.safe_dump(dict(block), sort_keys=False).rstrip() + "\n"


def _run_followup_dry_run(
    entry: Mapping[str, Any], *, dry_run_fn: DryRunFn | None, print_fn: PrintFn, err: PrintFn,
) -> None:
    if dry_run_fn is not None:
        dry_run_fn(dict(entry))
        return
    try:
        from .dry_run import run as dry_run_run
        from .engine import SyncEngine
        from .kanban import KanbanClient
        from .state import MemoryStore, SyncState
        (conn,) = parse_connections({"connections": [dict(entry)]})
        if conn.error:
            print_fn(f"dry-run skipped: {conn.error}")
            return
        adapter = get_adapter(conn.tracker, conn.settings)
        engine = SyncEngine(
            adapter, KanbanClient(conn.board or None), SyncState(MemoryStore()),
            conn.settings, conn=conn.id,
        )
        text, _code = dry_run_run([(engine, conn, None)], connection=conn.id)
        print_fn(text)
    except Exception as exc:
        err(f"issue-sync: setup wrote the connection but dry-run failed: {type(exc).__name__}: {exc}")


def _dedupe(items: Sequence[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


