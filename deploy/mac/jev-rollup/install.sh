#!/bin/sh
# install.sh -- install the jev-rollup sidecar on a peer host (macOS). Idempotent.
# Copies the five tool files (from $JEV_ROLLUP_SRC, delivered out of band: they are private) to ~/.local/share/33god/jev-rollup/ and links the two
# commands into ~/.local/bin. Touches nothing else: no credential, no host.env, no scheduler.
# Registering the clock is `candybar register` on this directory (see README.md).
set -eu
here="${JEV_ROLLUP_SRC:-$(cd "$(dirname "$0")" && pwd)}"
dest="${JEV_ROLLUP_HOME:-$HOME/.local/share/33god/jev-rollup}"
( cd "$here" && shasum -a 256 -c VENDORED.sha256 ) >/dev/null || { echo "install: VENDORED.sha256 mismatch, refusing" >&2; exit 1; }
mkdir -p "$dest" "$HOME/.local/bin"
for f in jev-rollup jev-rollup-run jev-env.sh jev_hostenv.py jev-rollup-cadence.sh; do
  install -m "$(stat -f %Lp "$here/$f" 2>/dev/null || stat -c %a "$here/$f")" "$here/$f" "$dest/$f"
done
for f in jev-rollup jev-rollup-run; do ln -sfn "$dest/$f" "$HOME/.local/bin/$f"; done
echo "installed to $dest; linked jev-rollup, jev-rollup-run into $HOME/.local/bin"
