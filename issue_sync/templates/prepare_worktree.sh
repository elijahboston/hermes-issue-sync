#!/bin/sh
# Create (or reuse) a git worktree for the issue from {{repo}} at {{base_branch}}.
# Issue JSON arrives on stdin and is discarded; ISSUE_SYNC_KEY is set by the plugin.
# Prints {"workspace": "worktree:<path>", "branch": "<branch>"}.
set -eu

repo="{{repo}}"
base="{{base_branch}}"
key="${ISSUE_SYNC_KEY:-}"

cat >/dev/null

if [ -z "$key" ]; then
	printf '%s\n' '{"skip": true, "reason": "ISSUE_SYNC_KEY is not set"}'
	exit 0
fi

case "$key" in
*[!A-Za-z0-9._-]*)
	printf '%s\n' '{"skip": true, "reason": "issue key is not a safe path component"}'
	exit 0
	;;
esac

if [ ! -d "$repo" ]; then
	printf '%s\n' '{"skip": true, "reason": "repo is not a directory"}'
	exit 0
fi

branch="issue/${key}"
root="$repo/.issue-sync-worktrees"
path="$root/$key"

cd "$repo"
mkdir -p "$root"
git fetch origin "$base" >/dev/null 2>&1 || git fetch origin >/dev/null 2>&1 || true

if [ ! -d "$path" ]; then
	if git show-ref --verify --quiet "refs/heads/$branch"; then
		git worktree add "$path" "$branch" >/dev/null
	elif git show-ref --verify --quiet "refs/remotes/origin/$branch"; then
		git worktree add --track -b "$branch" "$path" "origin/$branch" >/dev/null
	elif git show-ref --verify --quiet "refs/remotes/origin/$base"; then
		git worktree add -b "$branch" "$path" "origin/$base" >/dev/null
	else
		git worktree add -b "$branch" "$path" "$base" >/dev/null
	fi
fi

printf '{"workspace": "worktree:%s", "branch": "%s"}\n' "$path" "$branch"
