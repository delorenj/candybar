#!/bin/sh
# Container boot (Story 1.9; cgroup delegation Story 3.1):
#   1. as root, delegate a cgroup v2 subtree for per-invocation scopes (AD-7)
#      so the executor can meter memory.peak / cpu.stat (AD-9);
#   2. drop to the unprivileged `candybar` user (uid 10001);
#   3. apply registry migrations, then exec the daemon so it receives
#      docker stop's SIGTERM directly (no shell in between).
#
# Delegation needs a writable cgroupfs, i.e. a privileged container with a
# private cgroup namespace (deploy/compose.yml). Without it the daemon runs
# unscoped and memory/CPU meter as 0 — execution never fails on it.
# CANDYBAR_CGROUP_ROOT set to "" disables scoping outright.
set -e

APP_USER=candybar
CG=/sys/fs/cgroup

log() { echo "container-entrypoint: $*" >&2; }

delegate_cgroups() {
  root="${CANDYBAR_CGROUP_ROOT:-$CG/candybar}"
  [ -f "$CG/cgroup.controllers" ] || { log "no cgroup v2 at $CG"; return 1; }
  # No-internal-processes rule: the namespace root can only delegate
  # controllers once its processes live in a leaf. Move everything there.
  mkdir -p "$CG/daemon" 2>/dev/null || { log "cgroupfs read-only (not privileged?)"; return 1; }
  for pid in $(cat "$CG/cgroup.procs"); do
    echo "$pid" > "$CG/daemon/cgroup.procs" 2>/dev/null || true
  done
  for c in memory cpu; do
    echo "+$c" > "$CG/cgroup.subtree_control" 2>/dev/null || log "controller $c unavailable"
  done
  mkdir -p "$root" || return 1
  for c in memory cpu; do
    echo "+$c" > "$root/cgroup.subtree_control" 2>/dev/null || true
  done
  # The unprivileged executor creates scopes under $root and moves children
  # from daemon/ into them: that needs ownership of $root and write access to
  # cgroup.procs of the common ancestor (the namespace root) and the source.
  # (set -e is suspended inside a condition: every step must fail the function.)
  chown -R "$APP_USER:$APP_USER" "$root" || return 1
  chown "$APP_USER" "$CG/cgroup.procs" "$CG/daemon/cgroup.procs" || return 1
  CANDYBAR_CGROUP_ROOT="$root"
  export CANDYBAR_CGROUP_ROOT
  log "cgroup scoping enabled at $root ($(cat "$root/cgroup.subtree_control"))"
}

if [ "$(id -u)" = 0 ]; then
  if [ "${CANDYBAR_CGROUP_ROOT+set}" = set ] && [ -z "$CANDYBAR_CGROUP_ROOT" ]; then
    log "cgroup scoping disabled (CANDYBAR_CGROUP_ROOT is empty)"
  elif ! delegate_cgroups; then
    log "cgroup scoping unavailable; invocations run unscoped (memory/CPU meter as 0)"
    CANDYBAR_CGROUP_ROOT=""
    export CANDYBAR_CGROUP_ROOT
  fi
  export HOME="/home/$APP_USER"
  exec setpriv --reuid="$APP_USER" --regid="$APP_USER" --init-groups \
    "$0" "$@"
fi

alembic -c deploy/alembic.ini upgrade head
exec candybar daemon "$@"
