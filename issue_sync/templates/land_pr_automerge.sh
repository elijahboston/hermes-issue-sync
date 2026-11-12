#!/bin/sh
# Open (or reuse) a PR for $BRANCH onto {{base_branch}} and enable auto-squash merge.
# Exit 0 landed, 1 not yet, 2 failed (one-line reason on stdout).
# Env: ISSUE_KEY ISSUE_URL CARD_ID WORKTREE BRANCH SUMMARY_FILE
# SUMMARY_FILE is passed only as --body-file; its contents are never interpolated.
set -eu

base="{{base_branch}}"

if [ -z "${BRANCH:-}" ]; then
	echo "BRANCH is not set"
	exit 2
fi

if [ -n "${WORKTREE:-}" ] && [ -d "$WORKTREE" ]; then
	cd "$WORKTREE"
fi

if ! gh pr view "$BRANCH" --json state >/dev/null 2>&1; then
	if ! git push -u origin "$BRANCH"; then
		echo "git push origin $BRANCH failed"
		exit 2
	fi
	if [ -n "${SUMMARY_FILE:-}" ] && [ -f "$SUMMARY_FILE" ]; then
		if ! gh pr create --base "$base" --fill --body-file "$SUMMARY_FILE"; then
			echo "gh pr create failed"
			exit 2
		fi
	else
		if ! gh pr create --base "$base" --fill; then
			echo "gh pr create failed"
			exit 2
		fi
	fi
	gh pr merge "$BRANCH" --auto --squash >/dev/null 2>&1 || true
else
	gh pr merge "$BRANCH" --auto --squash >/dev/null 2>&1 || true
fi

state=$(gh pr view "$BRANCH" --json state -q .state)
if [ "$state" = "MERGED" ]; then
	exit 0
fi
if [ "$state" = "CLOSED" ]; then
	echo "PR is CLOSED"
	exit 2
fi

failures=$(gh pr view "$BRANCH" --json statusCheckRollup -q '[.statusCheckRollup[]? | select(.conclusion == "FAILURE") | (.name // .context // "check")] | join(", ")')
if [ -n "$failures" ] && [ "$failures" != "null" ]; then
	echo "check FAILURE: $failures"
	exit 2
fi

exit 1
