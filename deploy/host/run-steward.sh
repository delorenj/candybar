#!/bin/sh
# ExecStart of candybar-steward.service: exec the steward role from the repo
# checkout's .venv (the same checkout the templates are read from).
set -eu

root=$(cd "$(dirname "$0")/../.." && pwd)
exec "$root/.venv/bin/candybar" daemon --roles steward
