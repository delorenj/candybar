#!/usr/bin/env bash
# Install only the native manifest watcher. Does not restart other workers.
# Existing configuration is preserved. An absent configuration is created empty
# and left for enrollment; no discovered manifest is silently made authoritative.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
conf="${XDG_CONFIG_HOME:-$HOME/.config}/candybar/watch.toml"
cli="$repo/.venv/bin/candybar"
case "${1:-}" in
  ""|--dry-run) ;;
  *) printf 'Usage: %s [--dry-run]\n' "$0" >&2; exit 2 ;;
esac
[[ -x "$cli" ]] || { printf '%s\n' "Run uv sync --frozen in the checkout first." >&2; exit 1; }
mkdir -p "$(dirname "$conf")"
if [[ ! -e "$conf" ]]; then
  install -m 600 "$here/watch.toml.example" "$conf"
  printf 'Created %s. Enroll sources, then rerun this installer.\n' "$conf"
  exit 0
fi
# Refuse errors/conflicts before installing a worker that can change definitions.
"$cli" watch --config "$conf" --dry-run
if [[ "${1:-}" == "--dry-run" ]]; then
  exit 0
fi
# A drop-in preserves XDG config selection even if the user manager lacks it.
dropin="$HOME/.config/systemd/user/candybar-watch.service.d"
mkdir -p "$dropin"
# systemd quotes its own argument syntax, not shell syntax. Python escapes paths.
CANDYBAR_INSTALL_CLI="$cli" CANDYBAR_INSTALL_CONFIG="$conf" \
  "$repo/.venv/bin/python" - "$dropin/10-config.conf" <<'PY'
import os
import pathlib
import sys

def quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'

cli = quote(os.environ['CANDYBAR_INSTALL_CLI'])
config = quote(os.environ['CANDYBAR_INSTALL_CONFIG'])
pathlib.Path(sys.argv[1]).write_text(f'[Service]\nExecStart=\nExecStart={cli} watch --config {config}\n')
PY
systemctl --user enable "$here/candybar-watch.service"
systemctl --user daemon-reload
systemctl --user restart candybar-watch.service
systemctl --user --no-pager --lines=10 status candybar-watch.service
