#!/usr/bin/env bash
# Install (or refresh) the host executor unit (AD-17, Story 7.2) and the unit
# holder the systemd runner depends on (AD-18, Story 7.3). Idempotent.
#
#   deploy/host/install.sh            # render env, link unit, enable, (re)start
#   deploy/host/install.sh --no-start # everything but the (re)start
#
# Pair-deploy order matters (the executor's schema guard waits out a mismatch,
# but there is no reason to make it): rebuild the container first, then
# restart this unit — see deploy/README.md.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
conf="$HOME/.config/candybar"
env_file="$conf/executor.env"
start=1
[[ "${1:-}" == "--no-start" ]] && start=0

[[ -x "$repo/.venv/bin/candybar" ]] || {
  echo "install: $repo/.venv/bin/candybar missing — run 'uv sync --frozen' in $repo first" >&2
  exit 1
}

mkdir -p "$conf"
chmod 700 "$conf"
"$HOME/docker/stacks/_lib/op-inject.sh" "$here/executor.env.template" "$env_file"
# The vault DSN is container-facing; on the host Postgres is at loopback.
sed -i 's#@host\.docker\.internal:#@localhost:#' "$env_file"
chmod 600 "$env_file"

# The holder keeps each candybar-started systemd unit loaded after it finishes,
# so the runner can read its MemoryPeak / CPUUsageNSec (a successful oneshot is
# otherwise garbage-collected, accounting and all). It is a plain `sleep`: it
# never needs a restart, and the runner maintains its drop-in.
systemctl --user enable "$here/candybar-executor.service" "$here/candybar-unit-holder.service"
# The systemd-runner canary's unit: linked, never enabled (no install section).
systemctl --user link "$here/candybar-smoke.service" 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user start candybar-unit-holder.service
if (( start )); then
  systemctl --user restart candybar-executor.service
  sleep 2
  systemctl --user --no-pager --lines=15 status candybar-executor.service || true
fi
