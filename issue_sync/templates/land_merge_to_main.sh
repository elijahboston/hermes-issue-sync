#!/bin/sh
# Land by pushing this worktree's HEAD to origin/{{base_branch}}.
# Exit 0 landed, 1 not yet, 2 failed (one-line reason on stdout).
# Env: ISSUE_KEY ISSUE_URL CARD_ID WORKTREE BRANCH SUMMARY_FILE
set -eu

base="{{base_branch}}"

if [ -z "${WORKTREE:-}" ] || [ ! -d "$WORKTREE" ]; then
	echo "WORKTREE is missing or not a directory"
	exit 2
fi

cd "$WORKTREE"

if ! git fetch origin; then
	echo "git fetch origin failed"
	exit 2
fi

if ! git rebase "origin/$base"; then
	git rebase --abort >/dev/null 2>&1 || true
	echo "rebase onto origin/$base conflicted or failed"
	exit 2
fi

if ! git push origin "HEAD:$base"; then
	echo "push to origin/$base was rejected (not a fast-forward?)"
	exit 2
fi

if ! git fetch origin; then
	echo "git fetch origin failed after push"
	exit 2
fi

if git merge-base --is-ancestor HEAD "origin/$base"; then
	exit 0
fi

echo "HEAD is not an ancestor of origin/$base after push"
exit 2
