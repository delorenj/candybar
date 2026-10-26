#!/usr/bin/env bash
# Install (or refresh) the off-candybar watchdog units (AD-22, Story 7.6).
# Idempotent. Links (not copies) the unit pair so this repo stays the source
# of truth, matching deploy/host/install.sh.
#
#   deploy/watchdog/install.sh            # link, enable, start the timer
#   deploy/watchdog/install.sh --no-start # everything but the start
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
start=1
[[ "${1:-}" == "--no-start" ]] && start=0

systemctl --user link "$here/candybar-watchdog.service" "$here/candybar-watchdog.timer" 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable "$here/candybar-watchdog.timer"
if (( start )); then
  systemctl --user restart candybar-watchdog.timer
  systemctl --user --no-pager list-timers candybar-watchdog.timer
fi
