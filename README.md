# hermes-issue-sync

A Hermes Agent plugin that syncs Hermes kanban cards two-way with issue trackers.

- Inbound: open issues matching a connection's filter become kanban cards. Finished or
  canceled issues are skipped.
- Outbound: card status changes move the tracker issue to the mapped state; when a
  card reaches `done`, the **done gate** decides when the tracker issue goes to Done
  and the completion summary is posted (`immediate` by default; `merged` / `command`
  wait until the branch has landed).
- Blocking: when a worker blocks a card with a question, the question is posted on the
  issue. The first human reply there unblocks the card with the answer.
- Several connections at once: any mix of Linear workspaces and Jira sites, each with its
  own projects, label, board and state map.
- Desktop: an Issue Sync panel (what each connection syncs, its health, questions waiting on
  you), a status-bar chip that always shows the sync state, and issue-key badges on kanban
  cards (on Desktop builds that support them).

The dispatcher-side reconciler (a periodic poll) is authoritative. Hooks only
accelerate it by pushing a card's change as soon as it happens; if a hook misses an
event, the next reconcile pass catches up.

Each synced card stores its link on the card itself: the first line of the card body
is a marker

```
<!-- issue-sync conn=<connection> tracker=<tracker> key=<issue_key> url=<url> -->
```

and the card is created with the idempotency key `issue-sync:<connection>:<tracker>:<key>`, so
repeated or concurrent passes never create duplicate cards.

## Trackers

- `linear`: Linear GraphQL API, authenticated with a personal API key.
- `jira`: Jira Cloud REST v3, authenticated with an Atlassian account email + API token.
- `fake`: in-memory tracker for tests and demos.

Credentials are read by env-var name through the active Hermes profile's secret scope
(falling back to the process environment outside Hermes), so put them in the profile's
`.env`. Settings hold only the *names*; nothing is required at install time. Missing or
wrong credentials are reported by `/issues status`.

| Tracker | Secrets (defaults) | Setting that renames them |
|---------|--------------------|---------------------------|
| linear  | `LINEAR_API_KEY`   | `api_key_env` |
| jira    | `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | `base_url_env`, `email_env`, `token_env` |

**Always include a project in `filter`.** Labels are team-wide in Linear and site-wide in
Jira, so a label-only filter pulls in every labelled issue from unrelated projects.

## Configuration

### Setting up a connection

The fastest way to add a tracker is `hermes issues setup`. It writes one connection
into the active profile's `config.yaml` and, if you choose a landing or prepare
style, starter scripts under the plugin data dir (`hooks/<id>/`). It never asks
for secret *values* — only the env-var names that hold them.

Interactive (defaults in brackets):

```sh
hermes issues setup
```

Scripted:

```sh
hermes issues setup --yes --id lin --tracker linear \
  --project "Ticket Sync Sandbox" --board my-board --assignee worker \
  --landing merge-to-main --base-branch main --prepare worktree \
  --repo /path/to/checkout
```

`--yes` never guesses a required value (`--id`, `--tracker`; `--project` when
`--admit all`; `--repo` when `--prepare worktree`). Replacing an existing `id`
or overwriting scripts requires `--force`. `--print-only` prints the YAML block
and writes nothing.

Then inspect what the next pass would do (read-only: no cards, no comments, no
land command):

```sh
hermes issues dry-run --connection lin
hermes issues dry-run --json
```

Files `setup` may create:

| path | when |
|------|------|
| `~/.hermes/config.yaml` (the active profile) | always, unless `--print-only` |
| `<plugin-data>/hooks/<id>/land.sh` | `--landing merge-to-main`, `pr-automerge`, or `custom` |
| `<plugin-data>/hooks/<id>/prepare.sh` | `--prepare worktree` or `custom` |

You can still edit `config.yaml` by hand, as below.

In `~/.hermes/config.yaml`. Several trackers or projects at once use `connections`:

```yaml
plugins:
  entries:
    hermes-issue-sync:
      settings:
        board: my-board            # defaults shared by every connection
        assignee: worker
        connections:
          - id: linear-sandbox     # lowercase, unique; stored on each card
            tracker: linear
            projects: [Ticket Sync Sandbox, Another Project]
            label: agent
            state_map: {running: In Progress, review: In Review, done: Done}
          - id: jira-tss
            tracker: jira
            projects: [TSS]
            label: agent
            state_map: {ready: Selected for Development, running: In Progress, done: Done}
```

Each connection can override `board`, `assignee`, `state_map`, `poll_interval_seconds`,
`prepare_command`, `prepare_timeout_seconds`, `done_gate`, `land_command`,
`land_timeout_seconds`, `land_retry_seconds`, `merged_base`, `retry_on`, `reviewer` and the
credential env names. One failing connection never stops the others.

A single tracker can also be set with the top-level settings below (no `connections`).

Linear (team Core, project "Ticket Sync Sandbox"):

```yaml
plugins:
  entries:
    hermes-issue-sync:
      settings:
        tracker: linear
        filter:                    # ANDed
          label: agent             # default "agent"
          project: Ticket Sync Sandbox   # project NAME
          # team: CORE             # optional team key
        state_map:                 # kanban status -> Linear workflow state
          running: In Progress
          review: In Review
          done: Done
        # api_key_env: LINEAR_API_KEY
```

Linear state names are matched case-insensitively; a state *type* (`backlog`,
`unstarted`, `started`, `completed`, `canceled`) also works, e.g. `done: completed`.

Jira Cloud (project `TSS`, statuses Backlog, Selected for Development, In Progress, Done):

```yaml
plugins:
  entries:
    hermes-issue-sync:
      settings:
        tracker: jira
        filter:
          project: TSS
          label: agent             # default "agent"
          # jql: 'project = TSS AND labels = agent ORDER BY created ASC'   # overrides project/label
        state_map:                 # kanban status -> Jira status name
          ready: Selected for Development
          running: In Progress
          done: Done
        # base_url_env: JIRA_BASE_URL     # https://<site>.atlassian.net
        # email_env: JIRA_EMAIL
        # token_env: JIRA_API_TOKEN
```

Jira moves an issue by running the workflow transition that leads to the mapped status;
if the workflow has no such transition from the current status, the error names the
reachable statuses.

Common settings (all trackers):

```yaml
        enabled: true
        assignee: null             # Hermes profile to assign new cards to
        board: null                # kanban board slug; null = default board
        poll_interval_seconds: 60
```

Statuses left out of `state_map` are not pushed to the tracker.

## Choosing which issues become cards

Inbound still starts from the connection's tracker filter (projects + label). These
optional keys then decide which of those issues become cards. Set them on a connection
or at the top level as the default for every connection.

| key | values | default | meaning |
|-----|--------|---------|---------|
| `admit` | `label` or `all` | `label` | `label` keeps the label filter. `all` admits every open issue in the connection's projects (no label needed). `all` with no projects is a configuration error; it never means the whole workspace. |
| `max_active` | integer; `0` = unlimited | **1** | Cap on this connection's cards in `todo`, `ready`, `running`, `review` or `scheduled`. `blocked`, `triage`, `done` and `archived` do not count. Each pass admits at most `max_active - active` new cards. |
| `order` | `priority` or `created` | `priority` | `priority` sorts Urgent (1) … Low (4), then no priority last; ties break by smaller estimate, then oldest created. `created` sorts oldest first. |
| `leaf_only` | bool | false | Skip issues that have any still-open child or sub-issue. |
| `skip_blocked` | bool | **true** | Skip issues blocked by another issue that is still open. |
| `exclude_label` | string | unset | Never admit an issue with this label (case-insensitive), or whose parent / grandparent / great-grandparent carries it. |

When a key is omitted, behaviour is unchanged except two safety defaults: `max_active` is 1 (one card at a time per connection; raise it, or set `0` for unlimited) and `skip_blocked` is on.

```yaml
        admit: label
        max_active: 8
        order: priority
        leaf_only: true
        skip_blocked: true
        exclude_label: hold
```

## Shaping cards with a prepare script

Set `prepare_command` on a connection (or at the top level) to run an owner-set command
once before each card is created. Use it to pick a worktree, choose a model, or replace
the card body. With the key unset, inbound create is unchanged.

A string is split with `shlex` (no shell). A list of strings is argv. `~` in the first
element is expanded. Issue data is untrusted (anyone who can file a ticket writes it) and
arrives **only** as JSON on stdin — never interpolated into argv.

| key | type | default | meaning |
|-----|------|---------|---------|
| `prepare_command` | string or list of strings | unset | Command run once per new card. |
| `prepare_timeout_seconds` | int | 120 | Kill the command after this many seconds; that counts as a failure. |

```yaml
        prepare_command: ~/bin/issue-sync-prepare
        prepare_timeout_seconds: 120
```

Stdin is one JSON object:

```json
{"connection": "lin", "tracker": "linear", "key": "ABC-1", "title": "Fix login",
 "url": "https://linear.app/...", "body": "...", "labels": ["agent"],
 "priority": 2, "estimate": 1, "created_at": "2026-01-01T00:00:00Z", "state": "Todo"}
```

Stdout must be one JSON object. All keys are optional:

| key | type | effect |
|-----|------|--------|
| `body` | string | Card body after the issue-sync marker (the marker stays line 1). |
| `title` | string | Card title. |
| `assignee` | string | Hermes profile assigned to the card. |
| `workspace` | string | Passed to `kanban create --workspace` (`worktree:/abs/path`, `dir:/abs/path`, `scratch`). |
| `branch` | string | `--branch`. |
| `skills` | list of strings | Repeated `--skill`. |
| `model` | string | `--model`. |
| `provider` | string | `--provider`; only valid together with `model`. |

The command should create nothing itself — only print JSON. Example:

```python
#!/usr/bin/env python3
import json, sys

issue = json.load(sys.stdin)
print(json.dumps({
    "workspace": "scratch",
    "branch": f"issue/{issue['key']}",
    "skills": ["kanban"],
    "title": f"[{issue['key']}] {issue['title']}",
}))
```

Exit 0 with `{"skip": true, "reason": "disk is low"}` to leave the issue for a later pass
(no card, no error). Any other failure — non-zero exit, timeout, missing executable, stdout
that is not a JSON object, wrong types, `provider` without `model` — creates no card, records
the error, and retries next pass. Failures and skips do not consume a `max_active` slot, so
later candidates in the same pass can still be admitted.

Make the script **safe to run twice** for the same issue. It runs again on the next pass when
anything after it fails (for example `kanban create`), so reuse an existing worktree or branch
instead of failing on it.

`prepare_command` is owner-only, in `config.yaml`. `hermes issues setup --prepare`
writes a starter script and this setting; nothing else in the plugin sets it.



## When is an issue Done? (done gate)

When a kanban card reaches `done`, `done_gate` decides when the tracker issue may move to
Done and when the completion summary is posted. Unset, it is `immediate` — behaviour is
exactly as before. Other statuses (`running`, `review`, …) still push as soon as they change.

| key | values | default | meaning |
|-----|--------|---------|---------|
| `done_gate` | `immediate` \| `merged` \| `command` | `immediate` | When the tracker issue may go to Done. |
| `land_command` | string or list of strings | unset | Owner command for `command` mode (required there). |
| `land_timeout_seconds` | int | 1800 | Kill a land run after this long; counts as exit 2. |
| `land_retry_seconds` | int | 300 | Wait between runs after exit 1 ("not yet"), and between `merged` checks. |
| `merged_base` | string | unset | `merged` mode: branch the card branch must reach, e.g. `main`. Required in `merged` mode. |
| `retry_on` | list of regex strings | unset | Auto-unblock a matching block once (see below). |
| `reviewer` | profile name | unset | `merged` only: create a review card before the merge check. |

### `immediate`

The card reaching `done` pushes `state_map["done"]` and posts the summary. Same as today.

### `command`

The card reaching `done` does **not** push Done or post the summary yet. The plugin starts
`land_command` **detached** (a child process in its own session) and returns immediately so
the dispatcher tick is never blocked. A land command can take many minutes (push, PR, CI).

A string is split with `shlex` (no shell). A list of strings is argv. `~` in the first
element is expanded. The worker's summary is untrusted text: it is written to a file and
passed only as `SUMMARY_FILE` — never interpolated into argv or env.

Land command environment (plus the inherited process env):

| env | value |
|-----|--------|
| `ISSUE_SYNC_CONNECTION` | connection id |
| `ISSUE_KEY` | tracker issue key |
| `ISSUE_URL` | issue URL |
| `CARD_ID` | kanban card id |
| `WORKTREE` | card `workspace_path` |
| `BRANCH` | card `branch_name` |
| `SUMMARY_FILE` | path of a file holding the worker's summary |

Exit codes:

| exit | meaning |
|------|---------|
| **0** | Landed. The plugin pushes Done and posts the summary. |
| **1** | Not yet. Wait `land_retry_seconds` and run again. |
| **2** (or anything else, timeout, launch failure) | Failed. See below. |

The run is killed after `land_timeout_seconds` (default 1800) and treated as exit 2. A
gateway restart does not kill a detached land process; the next pass reads the result file
under the plugin data folder (`land/<connection>/<card_id>-<attempt>.json`).

`land_command` is required when `done_gate` is `command` (a connection error if missing).
It is owner-only, in `config.yaml`. `hermes issues setup --landing` writes a starter
`land.sh` and this setting; nothing else in the plugin sets it.


Example land script:

```python
#!/usr/bin/env python3
import os
import subprocess
import sys

branch = os.environ["BRANCH"]
worktree = os.environ["WORKTREE"]
# exit 1 = not yet (CI still running); 0 = merged; 2 = failed
try:
    subprocess.check_call(["git", "push", "-u", "origin", branch], cwd=worktree)
except subprocess.CalledProcessError:
    sys.exit(2)
state = subprocess.check_output(
    ["gh", "pr", "view", branch, "--json", "state", "-q", ".state"],
    cwd=worktree, text=True,
).strip()
if state == "MERGED":
    sys.exit(0)
if state in ("OPEN", "DRAFT"):
    sys.exit(1)
sys.exit(2)
```

A `done` card cannot be blocked, so a landing failure does **not** try to block the card.
Instead the plugin posts a signed comment on the tracker issue:

> Hermes finished the work but could not land it:
>
> *(quoted stdout tail, or stderr if stdout is empty, max ~1500 characters)*
>
> Reply here when it's fixed and Hermes will try again.

The failure is posted once per attempt. A human reply (same "not a bot" rule as blocked-card
answers — comments the plugin signed are ignored) re-queues the land command and is copied
onto the card as a kanban comment.

### `merged`

No command. Each pass (rate-limited by `land_retry_seconds`) checks whether the card's
branch has reached `merged_base`, in the card's `workspace_path`:

1. `git fetch origin`
2. `git merge-base --is-ancestor <branch> origin/<merged_base>` — exit 0 means landed.

**Squash merges** do not keep ancestry, so if `gh` is on `PATH` the plugin also runs
`gh pr view <branch> --json state -q .state` and treats `MERGED` as landed. Failures of
`gh` are ignored. Nothing else is run. There is no failure state: it just waits.

`merged_base` is required in `merged` mode (a connection error if missing).

### `reviewer` (merged mode only)

When `done_gate` is `merged` and `reviewer` is set, the plugin creates one review card
before the merge check:

- title `[<key>] review: <title>`, assignee `reviewer`
- same worktree (`worktree:<workspace_path>`) and branch
- idempotency key `issue-sync:<connection>:<tracker>:<key>:review`
- body is the link marker (so it resolves to the same issue) plus a short instruction to
  review the branch and complete or block

The merged check starts only after that review card is `done`. Admission and `max_active`
ignore it, and its own `done` does not trigger landing. A blocked review card asks its
question on the issue like any card. In other `done_gate` modes `reviewer` is ignored
(with one warning).

### `retry_on`

A list of case-insensitive regexes. When a card blocks and the latest block reason matches
any pattern, and this card has not been auto-retried yet, the plugin unblocks it once with
`issue-sync: automatic retry (block reason matched <pattern>)` and posts nothing on the
issue. A later matching block on the same card goes through the normal question path.
Unset means never auto-retry. An invalid regex is a connection error.

```yaml
        retry_on:
          - "token.*expired"
          - "max(imum)? output tokens"
```


## Commands

- `/issues status`: each connection's tracker, projects, board, last poll, backoff and errors.
- `/issues show`: toggle the Issue Sync panel in the connected Desktop app.
- `/issues dry-run`: what the next inbound pass would admit and what the done gate
  would do next, without creating, moving, or commenting. Same report as
  `hermes issues dry-run` (`--connection`, `--json`; `--with-prepare` is CLI-only).
- `hermes issues setup`: write a connection and optional starter scripts (CLI only).

In Desktop, the status-bar chip also toggles the panel. Closing the panel keeps sync running.

The panel shows a tree: tracker → board → issues, each level collapsible. Its connector style is
the **Panel tree style** plugin setting (Capabilities → Plugins → Issue Sync → settings, or
`tree_style` in `config.yaml`): `rounded` (default; tinted per tracker) or `lines`.
Each board header names the projects that feed it and the connection they come through
(`Ticket Sync Sandbox · via linear-sandbox`).

## Known Hermes issue: multi-profile gateways

On a gateway that serves several profiles, Hermes fires `on_kanban_dispatch_tick` without a
profile secret scope, so a plain `get_secret` read raises `UnscopedSecretError`. Until Hermes
binds the scope itself, the plugin binds its own profile's scope around its hooks
(`issue_sync/secrets.py:ensure_profile_scope`) using only the public `agent.secret_scope` API.
It does nothing when Hermes already bound a scope or the gateway serves one profile.

The scope is built from the profile's `.env`, its configured secret sources and any
admin-managed `.env`. A key set **only** as an environment variable of the gateway process
(Docker `-e`, systemd `Environment=`, `op run`) is not included on a multi-profile gateway:
put tracker keys in the profile's `.env`.
Upstream report: https://github.com/NousResearch/hermes-agent/issues/151126

## What this plugin does on your machine

- **Network:** calls only the trackers you configure: `https://api.linear.app/graphql` and
  your Jira Cloud site (`https://<site>.atlassian.net`). No telemetry or other services.
- **Writes to your trackers, as you:** it moves issues between states and posts comments
  (completion summaries, blocked-card questions, and land-failure notes) using your own API
  key. Every comment it writes ends with `— via Hermes issue-sync`.
- **Credentials:** read by name through Hermes's profile secret scope. The plugin stores no
  credentials and never reads `.env` or any credential file itself.
- **Background work:** on each kanban dispatcher tick it runs one sync pass per connection in
  a short-lived background thread, at most every `poll_interval_seconds`. When `done_gate` is
  `command`, a land run is started detached (new session) so a gateway restart does not kill
  it; the result file is read on a later pass.
- **Shell commands:** it runs `hermes kanban …` (list, show, create, comment, unblock) as a
  subprocess to read and update the board. If `prepare_command` is set, that owner-set
  command is also run (no shell, issue text on stdin only). If `land_command` is set, that
  owner-set command is started detached (no shell). In `merged` mode it may run
  `git fetch` / `git merge-base --is-ancestor` and, if `gh` is on `PATH`, `gh pr view` in
  the card's worktree. `hermes issues setup` may generate `git`/`gh` starter scripts; they
  run only when configured as `prepare_command` / `land_command`. `hermes issues dry-run`
  is read-only (board list + tracker fetch/`check`; `prepare_command` only with
  `--with-prepare`). No other commands.
- **Files:** writes plugin state through `ctx.state`, land result/summary files under
  `land/` in its data folder, and a status snapshot `pane.json` in
  `<hermes home>/plugin-data/hermes-issue-sync/`, which the Desktop panel reads through the
  plugin's own API route. `hermes issues setup` merges one connection into the active
  profile's `config.yaml` and may write `hooks/<id>/*.sh` under the plugin data dir
  (never overwrites an existing script unless `--force`). Dry-run writes nothing.

- **Desktop:** the panel, chip and card badges use only the Desktop plugin SDK.

## Development

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Tests never touch the network. To confirm real API response shapes read-only (only
`check()` and `fetch_issues()` are called; counts and field names are printed):

```sh
set -a; source ~/.hermes/.env; set +a
.venv/bin/python scripts/live_check.py --tracker linear   # or --tracker jira
```

The plugin uses only the Python standard library (3.11+); the panel's API route uses the
FastAPI that ships with Hermes. It needs Hermes 0.21.6 or newer.

## License

MIT
