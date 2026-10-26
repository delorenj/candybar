#!/bin/sh
# ExecStart of candybar-executor.service: resolve this unit's delegated cgroup,
# then exec the executor role from the repo checkout's .venv.
#
# With DelegateSubgroup=main the daemon lives in <unit cgroup>/main, so the
# per-invocation scopes (AD-7) go in the unit cgroup itself, its parent.
# CANDYBAR_CGROUP_ROOT set by the environment wins (an empty value runs
# unscoped); without cgroup v2 delegation the executor degrades to unscoped
# and memory/CPU meter as 0 — execution never depends on it.
set -eu

root=$(cd "$(dirname "$0")/../.." && pwd)

if [ -z "${CANDYBAR_CGROUP_ROOT+set}" ]; then
  rel=$(sed -n 's/^0:://p' /proc/self/cgroup)
  if [ "$(basename "$rel")" = main ] && [ -w "/sys/fs/cgroup$(dirname "$rel")/cgroup.procs" ]; then
    CANDYBAR_CGROUP_ROOT="/sys/fs/cgroup$(dirname "$rel")"
  else
    echo "run-executor: no delegated cgroup at $rel; invocations run unscoped" >&2
    CANDYBAR_CGROUP_ROOT=""
  fi
  export CANDYBAR_CGROUP_ROOT
fi

exec "$root/.venv/bin/candybar" daemon --roles executor
