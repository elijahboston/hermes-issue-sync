"""Read-only live check of a tracker adapter against the real API.

Calls ONLY ``check()`` and ``fetch_issues()``; never moves issues, posts comments, or creates
anything. Prints counts and field names only, never secret values or issue content.

    set -a; source ~/.hermes/.env; set +a; export LINEAR_API_KEY="$MCP_LINEAR_API_KEY"
    .venv/bin/python scripts/live_check.py --tracker linear|jira
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from issue_sync.adapters import AdapterError, get_adapter  # noqa: E402

DEFAULT_FILTERS = {
    "linear": {"label": "agent", "project": "Ticket Sync Sandbox"},
    "jira": {"project": "TSS", "label": "agent"},
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tracker", required=True, choices=sorted(DEFAULT_FILTERS))
    parser.add_argument("--project", help="override the default project filter")
    parser.add_argument("--label", help="override the default label filter")
    args = parser.parse_args(argv)

    filter_ = dict(DEFAULT_FILTERS[args.tracker])
    if args.project:
        filter_["project"] = args.project
    if args.label:
        filter_["label"] = args.label

    adapter = get_adapter(args.tracker, {"tracker": args.tracker, "filter": filter_})
    try:
        adapter.check()
        print(f"{args.tracker}: check ok")
        issues = adapter.fetch_issues(filter_)
    except AdapterError as exc:
        print(f"{args.tracker}: FAILED: {type(exc).__name__}: {exc}")
        return 1
    print(f"{args.tracker}: fetch_issues returned {len(issues)} issue(s) for filter keys {sorted(filter_)}")
    names = [f.name for f in dataclasses.fields(issues[0])] if issues else []
    for name in names:
        filled = sum(1 for issue in issues if getattr(issue, name))
        print(f"  {name}: non-empty in {filled}/{len(issues)}")
    states = sorted({issue.state for issue in issues})
    print(f"  distinct states: {len(states)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
