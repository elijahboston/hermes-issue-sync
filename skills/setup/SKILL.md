---
name: setup
description: "Use when connecting Linear or Jira to Hermes kanban with issue-sync."
---

# Set up an issue-sync connection

Walk the user through `hermes issues setup`. The CLI is the source of truth: you ask the
questions in plain language, then run it with flags. Never edit `config.yaml` by hand.

## Rules

- **Never ask for, print or store a secret.** Ask only for the *name* of the env var or
  profile secret that holds it (defaults: `LINEAR_API_KEY`; `JIRA_BASE_URL`, `JIRA_EMAIL`,
  `JIRA_API_TOKEN`). If it isn't set, tell the user to add it to the profile's `.env` themselves.
- **Preview before writing.** Run with `--print-only` first, show the YAML, get a yes, then run
  it again without `--print-only`.
- **Don't enable sync unasked.** Pass `--enable` only when the user says to start syncing.
- **Never overwrite a script the user edited.** Only pass `--force` if they say so.
- Setup runs `hermes kanban` and tracker reads only; it never moves issues or creates cards.

## Questions to ask (one message, recommended answer first)

1. **Tracker and projects.** Linear or Jira, which project(s), which kanban board, which Hermes
   profile works the cards (`--assignee`). Always name at least one project: labels are
   workspace-wide, so a label alone pulls in unrelated work.
2. **What makes an issue agent work?** Recommend the label `agent` (`--label`): nothing is picked
   up until someone adds it, so issues can be filed freely. `--admit all` takes every open issue
   in the project; only suggest it if they really want that.
3. **How many at once?** `--max-active` (default 1, the safe choice). Offer `--leaf-only` to skip
   issues that still have open sub-issues, and `--exclude-label` for human-only work.
4. **When is the issue Done?** This is the choice people get wrong, so explain it:
   - `none`: the issue is marked Done when the agent finishes. Fine for non-code work.
   - `merge-to-main`: the agent's branch is rebased and pushed to the base branch first.
   - `pr-automerge`: a pull request is opened with auto-merge; Done only once it merges.
     Needs the `gh` CLI logged in.
   - `custom`: writes a skeleton land script for them to fill in.
   For code work recommend `pr-automerge` if the repo uses PRs, else `merge-to-main`.
5. **Separate worktree per issue?** `--prepare worktree --repo <checkout> --base-branch <b>`
   gives each card its own git worktree beside the repo (`<repo>.worktrees`). Recommend it for
   code. `custom` writes a skeleton prepare script.

## Run it

```bash
hermes issues setup --yes --print-only --id <id> --tracker linear \
  --project "<Project>" --board <board> --assignee <profile> \
  --label agent --max-active 1 --landing merge-to-main --base-branch main \
  --prepare worktree --repo ~/code/<repo>
```

Then the same command without `--print-only` (add `--enable` if they want to start now).
Setup finishes with a dry run. Show it and explain each line: `would-admit`, or the reason an
issue is skipped (`closed`, `linked`, `excluded`, `parent`, `blocked`, `cap`).

## After setup

- `hermes issues dry-run` shows what would happen at any time, without changing anything.
- Scripts live in `<hermes home>/plugin-data/hermes-issue-sync/hooks/<id>/`. The land script
  must exit 0 (landed), 1 (not yet, retried later) or 2 (failed: its stdout is posted on the
  issue, and a reply there retries). Land scripts must never use the user's own working
  checkout: work in the card's worktree (`$WORKTREE`).
- The gateway must be restarted for config changes to take effect. Tell the user; don't
  restart it yourself if agents might be mid-turn.
- First run: suggest labelling one small, safe issue and watching it go through.
