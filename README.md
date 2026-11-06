# hermes-ticket-sync

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
<!-- ticket-sync tracker=<tracker> key=<issue_key> url=<url> -->
```

and the card is created with the idempotency key `ticket-sync:<tracker>:<key>`, so
repeated or concurrent passes never create duplicate cards.

## Status

Work in progress — only the `fake` tracker exists; Linear/Jira adapters come later.

## Configuration

In `~/.hermes/config.yaml`:

```yaml
plugins:
  entries:
    hermes-ticket-sync:
      settings:
        enabled: true
        tracker: fake
        filter: {}                 # tracker-specific issue filter
        state_map:                 # kanban status -> tracker state name
          ready: Todo
          running: In Progress
          blocked: Blocked
          review: In Review
          done: Done
        assignee: null             # Hermes profile to assign new cards to
        board: null                # kanban board slug; null = default board
        poll_interval_seconds: 60
```

Statuses left out of `state_map` are not pushed to the tracker.

## Commands

- `/tickets status` — show the tracker, last poll time, backoff, and recent errors.

## Development

```sh
uv venv .venv
uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest -q
```

The plugin uses only the Python standard library (3.11+).

## License

MIT
