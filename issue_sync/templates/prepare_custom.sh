#!/bin/sh
# Skeleton prepare command. Edit this file before relying on it.
# Issue JSON arrives on stdin. Print one JSON object on stdout.
# {"skip": true, "reason": "..."} leaves the issue for a later pass.
set -eu

cat >/dev/null
printf '%s\n' '{"skip": true, "reason": "edit me"}'
exit 0
