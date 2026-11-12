#!/bin/sh
# Render (and optionally load) the Mac executor LaunchAgent. Safe to re-run: the new file is
# linted before it replaces the old one, and --load boots out this one label first if it is
# already loaded in the target domain (and refuses if it is loaded in the other one).
#
#   deploy/mac/install-executor.sh --dry-run          # print the rendered plist
#   deploy/mac/install-executor.sh                    # write ~/Library/LaunchAgents/io.33god.candybar-executor.plist
#   deploy/mac/install-executor.sh --load             # ... and bootstrap it (see --domain)
#
# Options:
#   --path VALUE      the PATH for the job (default: PATH= from ~/.config/33god/launchd-agent.env,
#                     with <repo>/.venv/bin prepended)
#   --pg-port N       registry Postgres loopback port (default 5435)
#   --nats-port N     NATS loopback port (default 4222)
#   --domain D        gui | user  (default gui; label is bootstrapped into D/<uid>, and the plist is
#                     limited to that domain's one session type: gui -> Aqua, user -> Background)
#   --out FILE        write here instead of ~/Library/LaunchAgents (tests)
#
# Never loads without --load, never touches another label, never writes a secret.
# An existing plist is copied to $CANDYBAR_BACKUP_DIR first (never into the repo).
set -eu

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
label="io.33god.candybar-executor"
template="$here/$label.plist.template"
agent_env="${CANDYBAR_LAUNCHD_AGENT_ENV:-$HOME/.config/33god/launchd-agent.env}"
out="$HOME/Library/LaunchAgents/$label.plist"
backup_dir="${CANDYBAR_BACKUP_DIR:-$HOME/33god-migration/backups/candybar-executor}"
path_value=""
pg_port=5435
nats_port=4222
domain=gui
dry=0
load=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry=1 ;;
    --load) load=1 ;;
    --path) path_value="$2"; shift ;;
    --pg-port) pg_port="$2"; shift ;;
    --nats-port) nats_port="$2"; shift ;;
    --domain) domain="$2"; shift ;;
    --out) out="$2"; shift ;;
    *) echo "install-executor: unknown option $1" >&2; exit 64 ;;
  esac
  shift
done

case "$domain" in
  gui) session_type=Aqua ;;
  user) session_type=Background ;;
  *) echo "install-executor: --domain must be gui or user" >&2; exit 64 ;;
esac
case "$pg_port$nats_port" in *[!0-9]*) echo "install-executor: ports must be numeric" >&2; exit 64 ;; esac

if [ -z "$path_value" ]; then
  [ -r "$agent_env" ] || { echo "install-executor: $agent_env not readable; pass --path" >&2; exit 66; }
  path_value=$(sed -n 's/^PATH=//p' "$agent_env" | head -n 1)
  [ -n "$path_value" ] || { echo "install-executor: no PATH= line in $agent_env; pass --path" >&2; exit 66; }
fi
path_value="$repo/.venv/bin:$path_value"

# '|' never appears in a path or port; escape sed's replacement metacharacters anyway.
esc() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }

rendered=$(sed \
  -e "s|__REPO__|$(esc "$repo")|g" \
  -e "s|__PATH__|$(esc "$path_value")|g" \
  -e "s|__HOME__|$(esc "$HOME")|g" \
  -e "s|__PG_PORT__|$(esc "$pg_port")|g" \
  -e "s|__NATS_PORT__|$(esc "$nats_port")|g" \
  -e "s|__SESSION_TYPE__|$session_type|g" \
  "$template")

case "$rendered" in *__[A-Z_]*__*) echo "install-executor: unsubstituted placeholder left in plist" >&2; exit 70 ;; esac

if [ "$dry" -eq 1 ]; then
  printf '%s\n' "$rendered"
  exit 0
fi

uid=$(id -u)
target="$domain/$uid"
loaded_here=0
if [ "$load" -eq 1 ]; then
  # Decide before writing anything: a copy in the OTHER domain would double-run jobs, so refuse.
  for other in gui user; do
    if launchctl print "$other/$uid/$label" >/dev/null 2>&1; then
      if [ "$other" = "$domain" ]; then
        loaded_here=1
      else
        echo "install-executor: $label is already loaded in $other/$uid; boot it out there first (a second copy would double-run jobs)" >&2
        exit 73
      fi
    fi
  done
fi

mkdir -p "$(dirname "$out")" "$HOME/Library/Logs/33god"
# Lint a temp file first: a plist that fails plutil must never replace a working one.
tmp="$out.new.$$"
trap 'rm -f "$tmp"' EXIT
printf '%s\n' "$rendered" > "$tmp"
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$tmp" >/dev/null || { echo "install-executor: rendered plist failed plutil -lint; $out left untouched" >&2; exit 65; }
fi
if [ -f "$out" ]; then
  mkdir -p "$backup_dir"
  cp -p "$out" "$backup_dir/$label.plist.$(date +%Y%m%dT%H%M%S)"
fi
mv "$tmp" "$out"
echo "install-executor: wrote $out"

if [ "$load" -eq 1 ]; then
  if [ "$loaded_here" -eq 1 ]; then
    # A reload of this one label: the running copy keeps its old definition until it is booted out.
    launchctl bootout "$target/$label"
    echo "install-executor: booted out the running $label from $target"
  fi
  launchctl bootstrap "$target" "$out"
  echo "install-executor: bootstrapped $label into $target"
fi
