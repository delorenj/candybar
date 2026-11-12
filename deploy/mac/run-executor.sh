#!/bin/sh
# ExecStart-equivalent of the Mac candybar executor (launchd ProgramArguments).
#
# POSIX sh only: launchd's PATH puts /bin/sh (bash 3.2 in posix mode) first.
# Never reads /proc: macOS has none, and CANDYBAR_CGROUP_ROOT is forced empty
# so the executor never even looks for a cgroup tree (contrast
# deploy/host/run-executor.sh, which resolves one from /proc/self/cgroup on Linux).
#
# The DSN (no password) comes from the plist; the password is injected here from
# the interim store, never from a file in this repo and never from the plist.
# Overrides, used by tests and by W5-2 dry runs:
#   CANDYBAR_ENV_FILE      secret-ref file (default: executor.env.op beside this script)
#   CANDYBAR_ENV_RESOLVER  resolver binary (default: env-op-resolve on PATH)
set -eu

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)

CANDYBAR_CGROUP_ROOT=""
export CANDYBAR_CGROUP_ROOT

# psycopg (pure python) dlopens libpq, and Homebrew's libpq is keg-only (not on
# dyld's default search path). Exported here, after sh has started: SIP strips
# DYLD_* from anything launched THROUGH /bin/sh, so the plist cannot carry it.
DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/opt/libpq/lib:/opt/homebrew/lib:${DYLD_FALLBACK_LIBRARY_PATH:-$HOME/lib:/usr/local/lib:/usr/lib}"
export DYLD_FALLBACK_LIBRARY_PATH

bin="$root/.venv/bin/candybar"
if [ ! -x "$bin" ]; then
  echo "run-executor: $bin missing; run 'uv sync --frozen' in $root first" >&2
  exit 78
fi

env_file="${CANDYBAR_ENV_FILE:-$here/executor.env.op}"
resolver="${CANDYBAR_ENV_RESOLVER:-env-op-resolve}"
if ! command -v "$resolver" >/dev/null 2>&1; then
  echo "run-executor: $resolver not found on PATH ($PATH)" >&2
  exit 78
fi

# exec: launchd's SIGTERM must reach the asyncio roles, not a shell. The
# executor itself retries NATS forever and holds at the schema guard until the
# registry is migrated, so no readiness loop is needed here (a KeepAlive thrash
# against a missing dependency is what such a loop would only disguise).
exec "$resolver" exec --env-file "$env_file" -- "$bin" daemon --roles executor
