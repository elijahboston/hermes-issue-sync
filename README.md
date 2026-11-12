# hermes-issue-sync

A Hermes Agent plugin that syncs Hermes kanban cards two-way with issue trackers.

- Inbound: open issues matching a connection's filter become kanban cards. Finished or
  canceled issues are skipped.
- Outbound: card status changes move the tracker issue to the mapped state; when a
  card reaches `done`, its completion summary is posted to the issue as a comment.
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

Each connection can override `board`, `assignee`, `state_map`, `poll_interval_seconds` and the
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
| `max_active` | integer ≥ 1 | unlimited | Cap on this connection's cards in `todo`, `ready`, `running`, `review` or `scheduled`. `blocked`, `triage`, `done` and `archived` do not count. Each pass admits at most `max_active - active` new cards. |
| `order` | `priority` or `created` | `priority` | `priority` sorts Urgent (1) … Low (4), then no priority last; ties break by smaller estimate, then oldest created. `created` sorts oldest first. |
| `leaf_only` | bool | false | Skip issues that have any still-open child or sub-issue. |
| `skip_blocked` | bool | **true** | Skip issues blocked by another issue that is still open. |
| `exclude_label` | string | unset | Never admit an issue with this label (case-insensitive), or whose parent / grandparent / great-grandparent carries it. |

When a key is omitted, behaviour is unchanged except `skip_blocked`, which defaults to on.

```yaml
        admit: label
        max_active: 8
        order: priority
        leaf_only: true
        skip_blocked: true
        exclude_label: hold
```

## Card templates, worktrees and models

Optional per-connection keys (or top-level defaults) that shape the kanban card, not which
issues are admitted. When they are absent, cards are created exactly as they are today.

- `card_template`: path (`~` expanded) to a text file. Rendered with `str.format_map`; unknown
  `{placeholders}` are left intact. Placeholders: `{key} {title} {url} {body} {base} {base_sha}
  {worktree} {branch}` (the last four are empty for scratch workspaces). The result is the card
  body **after** the marker line — the marker is always first, so `link_from_card` still works.
  A missing or unreadable file is a per-issue error, not a crash.
- `workspace`: `{mode: scratch}` (default, no flags) or `{mode: worktree, repo, base, root,
  branch_prefix}`. `base` may be a glob such as `release/*`: the plugin fetches `origin`, lists
  matching `refs/remotes/origin/…`, and picks the newest by version-aware sort of the part after
  the prefix (falling back to committer date). `root` defaults to `<repo>.worktrees` beside the
  repo, not inside it. The worktree is `<root>/<slug>` on branch `<branch_prefix><slug>`, created
  with `git worktree add` before the card (reused if the directory already exists) and passed as
  `--workspace worktree:<path> --branch <branch>`.
- `skills`: list of skill names, each passed as `--skill`.
- `model_rules`: evaluated top to bottom, first match wins. Each rule is
  `{when: {label: name} | {estimate_max: n} | {estimate_min: n} | {}, model: id, provider: name}`
  or uses `role` instead of `model` to resolve through `roles_file`. A match passes `--model`
  (and `--provider` when given); no match passes nothing.
- `roles_file`: YAML shaped `roles: {role: model-key}` and `models: {model-key: {provider: id}}`.
  A role rule resolves `models[roles[role]][provider]`. Unreadable file or unknown role: no model
  flags and a log warning, never a crash.
- `reviewer`: profile name stored on the engine for later landing work; not passed to kanban yet.

```yaml
plugins:
  entries:
    hermes-issue-sync:
      settings:
        board: my-board
        connections:
          - id: lin
            tracker: linear
            projects: [Sandbox]
            label: agent
            card_template: ~/agent/card.md
            workspace:
              mode: worktree
              repo: ~/src/app
              base: release/*
              # root defaults to ~/src/app.worktrees
              branch_prefix: wt/
            skills: [repo, kanban]
            roles_file: ~/agent/roles.yaml
            model_rules:
              - when: {label: hard}
                role: strong
                provider: anthropic
              - when: {estimate_max: 2}
                model: gpt-4.1-mini
                provider: openai
              - when: {}
                model: gpt-4.1
                provider: openai
            reviewer: reviewer
```

## Commands

- `/issues status`: each connection's tracker, projects, board, last poll, backoff and errors.
- `/issues show`: toggle the Issue Sync panel in the connected Desktop app.

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
  (completion summaries and blocked-card questions) using your own API key. Every comment it
  writes ends with `— via Hermes issue-sync`.
- **Credentials:** read by name through Hermes's profile secret scope. The plugin stores no
  credentials and never reads `.env` or any credential file itself.
- **Background work:** on each kanban dispatcher tick it runs one sync pass per connection in
  a short-lived background thread, at most every `poll_interval_seconds`.
- **Shell commands:** it runs `hermes kanban …` (list, show, create, comment, unblock) as a
  subprocess to read and update the board. When a connection uses a worktree workspace it also
  runs `git` in that connection's repo (`fetch`, `for-each-ref` / `rev-parse`, `worktree add`).
- **Files:** writes plugin state through `ctx.state` and a status snapshot
  `pane.json` in its own data folder (`<hermes home>/plugin-data/hermes-issue-sync/`), which
  the Desktop panel reads through the plugin's own API route. Worktree mode also creates git
  worktrees under the configured workspace root (default `<repo>.worktrees` beside the repo).
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
