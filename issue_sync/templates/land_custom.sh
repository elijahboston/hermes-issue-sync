#!/bin/sh
# Skeleton land command. Edit this file before relying on it.
#
# Env: ISSUE_KEY ISSUE_URL CARD_ID WORKTREE BRANCH SUMMARY_FILE
# SUMMARY_FILE is a path; never interpolate its contents into a command.
#
# Exit:
#   0  landed — the tracker issue may go to Done
#   1  not yet — retry after land_retry_seconds
#   2  failed — print a one-line human reason on stdout
set -eu

echo "edit me"
exit 2
