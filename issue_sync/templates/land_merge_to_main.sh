#!/bin/sh
# Land by pushing this worktree's HEAD to origin/{{base_branch}}.
# Exit 0 landed, 1 not yet, 2 failed (one-line reason on stdout).
# Env: ISSUE_KEY ISSUE_URL CARD_ID WORKTREE BRANCH SUMMARY_FILE
set -eu

base="{{base_branch}}"

repo="{{repo}}"

if [ -z "${WORKTREE:-}" ]; then
	echo "WORKTREE is not set"
	exit 2
fi

# Hermes removes a done card's worktree when it is clean and every commit is on a remote,
# which can happen before this script runs. Rebuild it from the branch when it's gone.
if [ ! -d "$WORKTREE" ]; then
	branch="${BRANCH:-}"
	[ -n "$branch" ] || branch="issue/${ISSUE_KEY:-}"
	if [ -z "$repo" ]; then
		# prepare_worktree.sh puts worktrees at <repo>.worktrees/<key>.
		parent=$(dirname "$WORKTREE")
		case "$parent" in
		*.worktrees) repo="${parent%.worktrees}" ;;
		esac
	fi
	if [ -z "$repo" ] || [ ! -d "$repo" ]; then
		echo "worktree $WORKTREE is gone and the repo to rebuild it from is unknown"
		exit 2
	fi
	git -C "$repo" worktree prune >/dev/null 2>&1 || true
	git -C "$repo" fetch origin >/dev/null 2>&1 || true
	if git -C "$repo" show-ref --verify --quiet "refs/heads/$branch"; then
		git -C "$repo" worktree add "$WORKTREE" "$branch" >/dev/null 2>&1 \
			|| { echo "could not recreate worktree for $branch"; exit 2; }
	elif git -C "$repo" show-ref --verify --quiet "refs/remotes/origin/$branch"; then
		git -C "$repo" worktree add --track -b "$branch" "$WORKTREE" "origin/$branch" >/dev/null 2>&1 \
			|| { echo "could not recreate worktree for $branch"; exit 2; }
	else
		echo "worktree is gone and branch $branch exists neither locally nor on origin"
		exit 2
	fi
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
