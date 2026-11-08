# hermes-issue-sync

A Hermes Agent plugin that syncs Hermes kanban cards two-way with issue trackers.

- Inbound: issues matching the configured filter become kanban cards.
- Outbound: card status changes move the tracker issue to the mapped state; when a
  card reaches `done`, its completion summary is posted to the issue as a comment.

The dispatcher-side reconciler (a periodic poll) is authoritative. Hooks only
accelerate it by pushing a card's change as soon as it happens; if a hook misses an
event, the next reconcile pass catches up.

Each synced card stores its link on the card itself: the first line of the card body
is a marker

```
<!-- issue-sync tracker=<tracker> key=<issue_key> url=<url> -->
```

and the card is created with the idempotency key `issue-sync:<tracker>:<key>`, so
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

In `~/.hermes/config.yaml`.

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

## Commands

- `/issues status` — show the tracker, last poll time, backoff, and recent errors.

## Known Hermes issue: multi-profile gateways

On a gateway that serves several profiles, Hermes fires `on_kanban_dispatch_tick` without a
profile secret scope, so a plain `get_secret` read raises `UnscopedSecretError`. Until Hermes
binds the scope itself, the plugin binds its own profile's scope around its hooks
(`issue_sync/secrets.py:ensure_profile_scope`). It mirrors what Hermes's own auto-decompose tick
does, and it does nothing when Hermes already bound a scope or the gateway serves one profile.
Upstream report: https://github.com/NousResearch/hermes-agent/issues/151126

## Development

```sh
uv venv .venv
uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest -q
```

Tests never touch the network. To confirm real API response shapes read-only (only
`check()` and `fetch_issues()` are called; counts and field names are printed):

```sh
set -a; source ~/.hermes/.env; set +a
.venv/bin/python scripts/live_check.py --tracker linear   # or --tracker jira
```

The plugin uses only the Python standard library (3.11+).

## License

MIT
