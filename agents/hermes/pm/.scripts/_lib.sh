# shellcheck shell=bash
# Common helpers sourced by every numbered provisioning step.

set -euo pipefail

# These three are set by Copier into the rendered role.yaml; we re-derive them
# here so each script is callable in isolation (e.g. for repair runs).
ROLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROLE_YAML="$ROLE_DIR/role.yaml"
PROV_LOG="$ROLE_DIR/.scripts/.provision.log"

mkdir -p "$ROLE_DIR/.scripts"

# Logging
log()  { local msg="[$(date +%H:%M:%S)] $*"; printf '\033[36m%s\033[0m\n' "$msg" >&2; printf '%s\n' "$msg" >> "$PROV_LOG"; }
warn() { local msg="[$(date +%H:%M:%S)] $*"; printf '\033[33m%s\033[0m\n' "$msg" >&2; printf '%s\n' "$msg" >> "$PROV_LOG"; }
err()  { local msg="[$(date +%H:%M:%S)] $*"; printf '\033[31m%s\033[0m\n' "$msg" >&2; printf '%s\n' "$msg" >> "$PROV_LOG"; }
die()  { err "$*"; exit 1; }

# Read a single field from role.yaml. Requires python3 (no yaml dep).
yaml_get() {
  # yaml_get  KEY[.SUBKEY]    e.g.  yaml_get role,  yaml_get telegram.bot_username
  local key="$1"
  python3 - "$ROLE_YAML" "$key" <<'PYEOF'
import sys, re, pathlib
path, key = sys.argv[1:3]
text = pathlib.Path(path).read_text()
parts = key.split(".")
# Trivial YAML walker — handles flat and one-level nested keys.
indent = -1
prefix = ""
for part in parts[:-1]:
    indent += 2
    prefix += part + ":"
    m = re.search(rf"(?m)^{re.escape(part)}:\s*$", text)
    if not m:
        sys.exit(0)
    text = text[m.end():]
key = parts[-1]
m = re.search(rf'(?m)^\s*{re.escape(key)}:\s*"?([^"\n]*)"?\s*$', text)
if m:
    print(m.group(1).strip())
PYEOF
}

# Apply a sed substitution to role.yaml in-place. Used to record IDs after
# external provisioning steps return them.
yaml_set() {
  # yaml_set KEY VALUE   (supports flat and one-level nested keys)
  local key="$1" val="$2"
  python3 - "$ROLE_YAML" "$key" "$val" <<'PYEOF'
import json, pathlib, re, sys

path, key, val = sys.argv[1:4]
p = pathlib.Path(path)
text = p.read_text(encoding="utf-8")
parts = key.split(".")
if len(parts) not in (1, 2) or any(
    not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", part) for part in parts
):
    sys.exit(f"yaml_set: unsupported key '{key}'")

lines = text.splitlines(keepends=True)
start, end, parent_indent = 0, len(lines), -1
if len(parts) == 2:
    parent, leaf = parts
    parents = []
    for index, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        match = re.fullmatch(rf"( *){re.escape(parent)}:\s*(?:#.*)?", line)
        if match and len(match.group(1)) == 0:
            parents.append(index)
    if len(parents) != 1:
        sys.exit(f"yaml_set: parent '{parent}' not found uniquely in {path}")
    parent_index = parents[0]
    parent_indent = 0
    start = parent_index + 1
    for index in range(start, len(lines)):
        candidate = lines[index].rstrip("\r\n")
        if not candidate.strip() or candidate.lstrip().startswith("#"):
            continue
        indent = len(candidate) - len(candidate.lstrip(" "))
        if indent <= parent_indent:
            end = index
            break
else:
    leaf = parts[0]

targets = []
for index in range(start, end):
    raw = lines[index]
    line = raw.rstrip("\r\n")
    match = re.match(rf"^( *){re.escape(leaf)}:\s*", line)
    if not match:
        continue
    indent = len(match.group(1))
    if (parent_indent < 0 and indent == 0) or (parent_indent >= 0 and indent > parent_indent):
        targets.append((index, match.group(1)))
if len(targets) != 1:
    sys.exit(f"yaml_set: key '{key}' not found uniquely in {path}")

index, indent = targets[0]
ending = "\r\n" if lines[index].endswith("\r\n") else "\n" if lines[index].endswith("\n") else ""
lines[index] = f"{indent}{leaf}: {json.dumps(val, ensure_ascii=False)}{ending}"
p.write_text("".join(lines), encoding="utf-8")
PYEOF
}

# ─── Distributable config (~/.config/hermes-agent-template/config.toml) ──────
# Single source of truth for environment-specific defaults so this template can
# be handed to someone else without editing any script. Ship config.example.toml
# is copied here on first provision (see .scripts/01-config.sh).
HERMES_TEMPLATE_CONFIG="${HERMES_TEMPLATE_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/hermes-agent-template/config.toml}"
export HERMES_TEMPLATE_CONFIG

# config_get <dotted.key> [default]   — print a value from config.toml (paths are
# tilde-expanded; arrays are space-joined). Falls back to [default] when the file,
# python3, or the key is missing. Always exits 0 so it's safe under `set -e`.
config_get() {
  local key="$1" def="${2:-}"
  if [[ ! -f "$HERMES_TEMPLATE_CONFIG" ]] || ! command -v python3 >/dev/null 2>&1; then
    printf '%s' "$def"; return 0
  fi
  python3 - "$HERMES_TEMPLATE_CONFIG" "$key" "$def" <<'PYEOF' || printf '%s' "$def"
import sys, os
try:
    import tomllib
except ModuleNotFoundError:  # python < 3.11
    print(sys.argv[3], end=""); sys.exit(0)
path, key, default = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, "rb") as f:
        cur = tomllib.load(f)
except Exception:
    print(default, end=""); sys.exit(0)
for part in key.split("."):
    if isinstance(cur, dict) and part in cur:
        cur = cur[part]
    else:
        print(default, end=""); sys.exit(0)
if isinstance(cur, list):
    cur = " ".join(str(x) for x in cur)
else:
    cur = str(cur)
print(os.path.expanduser(cur), end="")
PYEOF
  return 0
}

# Re-export role fields into the environment for the rest of the script.
load_role_env() {
  ROLE=$(yaml_get role)
  REPO=$(yaml_get repo)
  AGENT_ID=$(yaml_get agent_id)
  DISPLAY_NAME=$(yaml_get display_name)
  BOT_HANDLE=$(yaml_get telegram.bot_username)
  PROFILE_NAME=$(yaml_get profile)

  # Plane workspace: empty in role.yaml -> resolve from config.toml.
  PLANE_WORKSPACE=$(yaml_get plane.workspace)
  [[ -n "$PLANE_WORKSPACE" ]] || PLANE_WORKSPACE=$(config_get plane.workspace "33god")

  # Runtime repo: role.yaml stores the bare repo name plus an optional owner.
  # Older manifests stored "owner/name" directly in github_repo; honor both.
  RUNTIME_REPO=$(yaml_get runtime.github_repo)
  if [[ "$RUNTIME_REPO" != */* ]]; then
    local owner; owner=$(yaml_get runtime.github_owner)
    [[ -n "$owner" ]] || owner=$(config_get github.runtime_repo_owner "delorenj")
    RUNTIME_REPO="$owner/$RUNTIME_REPO"
  fi

  export ROLE REPO AGENT_ID DISPLAY_NAME BOT_HANDLE \
         PLANE_WORKSPACE RUNTIME_REPO PROFILE_NAME
}

# Skip a step if previously completed (idempotent reruns).
already_done() {
  local marker="$ROLE_DIR/.scripts/.done-$1"
  [[ -f "$marker" ]]
}
mark_done() {
  touch "$ROLE_DIR/.scripts/.done-$1"
}
clear_done() {
  # Remove only the marker for the explicitly deferred step.  This lets a
  # later activation reconcile that phase without disturbing completed,
  # unrelated provisioning state.
  rm -f -- "$ROLE_DIR/.scripts/.done-$1"
}

# The parser protocol, unsafe-family scrub, complete framing validation, and
# atomic environment apply live in one reusable library shared by provisioners,
# launchers, and maintenance commands.
FLEET_ENV_LIBRARY="$ROLE_DIR/.scripts/lib/fleet-env.sh"
FLEET_ENV_PARSER="$ROLE_DIR/.scripts/lib/parse-fleet-env.py"
if [[ ! -f "$FLEET_ENV_LIBRARY" || -L "$FLEET_ENV_LIBRARY" ]]; then
  builtin printf 'fleet environment loader rejected: trusted library is unavailable\n' >&2
  return 1
fi
# shellcheck source=lib/fleet-env.sh
builtin source "$FLEET_ENV_LIBRARY"

# Fleet source-of-truth (shared across all wrappers/provisioners).
# Every default below resolves as: env var > fleet.env > config.toml > fallback.
# Invocation authority is not fleet configuration. Capture the caller's board
# gate before importing fleet.env so that file cannot weaken an MCP/CLI
# SKIP_PLANE decision or re-enable credentials by assigning SKIP_PLANE=0.
PJANGLER_INVOCATION_SKIP_PLANE="${SKIP_PLANE:-0}"
FLEET_ENV="${HERMES_FLEET_ENV:-$(config_get fleet.fleet_env "$HOME/.hermes/fleet.env")}"

# A direct CLI caller may not have passed through the MCP parent boundary. The
# shared loader hardens both before parser startup and again after atomic import.
if ! load_fleet_environment "$FLEET_ENV" "$FLEET_ENV_PARSER"; then
  return 1
fi
SKIP_PLANE="$PJANGLER_INVOCATION_SKIP_PLANE"
unset PJANGLER_INVOCATION_SKIP_PLANE
scrub_subprocess_interpreter_injection

# fleet.env is allowed to supply provider credentials only for an explicitly
# board-authorized invocation. A no-board/deferred phase must remove every
# supported provider alias after sourcing and before any Python, Hermes,
# systemd, provider, or other child process can inherit it.
scrub_ticket_provider_authority() {
  local key
  unset PLANE_API_KEY TRELLO_KEY TRELLO_TOKEN LINEAR_API_KEY
  while IFS= read -r key; do
    case "$key" in
      PLANE_*_API_KEY) unset "$key" ;;
    esac
  done < <(compgen -A variable PLANE_)
}
if [[ "$SKIP_PLANE" == "1" ]]; then
  scrub_ticket_provider_authority
fi
# Identity-bearing chat credentials are never fleet-scoped. Platform wiring
# steps capture explicit invocation values before sourcing this library and
# restore only those values afterward; all other provisioning steps stay clean.
unset TELEGRAM_BOT_TOKEN SLACK_BOT_TOKEN SLACK_APP_TOKEN

# Tools we expect on the host
HERMES_BIN="${HERMES_BIN:-${HERMES_FLEET_BIN:-$(config_get fleet.hermes_bin "$HOME/.local/share/hermes-agent/releases/0408fec7a153e6c32c064acd2b8053917f1525f1/.venv/bin/hermes")}}"
HERMES_AGENT_REPO="${HERMES_AGENT_REPO:-${HERMES_FLEET_REPO:-$(config_get fleet.hermes_repo "$HOME/.local/share/hermes-agent/releases/0408fec7a153e6c32c064acd2b8053917f1525f1")}}"
PJANGLER_BIN="${PJANGLER_BIN:-$(config_get fleet.pjangler_bin "pj")}"
HERMES_RUNTIME_GIT_URL="${HERMES_RUNTIME_GIT_URL:-$(config_get fleet.hermes_git_url 'https://github.com/delorenj/hermes-agent.git')}"
HERMES_RUNTIME_GIT_REF="${HERMES_RUNTIME_GIT_REF:-$(config_get fleet.hermes_git_ref 'main')}"
HERMES_RUNTIME_GIT_SHA="${HERMES_RUNTIME_GIT_SHA:-$(config_get fleet.hermes_git_sha '0408fec7a153e6c32c064acd2b8053917f1525f1')}"
HERMES_OAUTH_FILE="${HERMES_OAUTH_FILE:-${HERMES_FLEET_OAUTH_FILE:-$(config_get fleet.oauth_file "$HOME/.hermes/auth.json")}}"
CODEX_HOME="${CODEX_HOME:-${HERMES_FLEET_CODEX_HOME:-$(config_get fleet.codex_home "$HOME/.codex")}}"
# Prefer a scaffold vendored into this agent directory; fall back to the configured template path.
RUNTIME_SCAFFOLD_DIR="${RUNTIME_SCAFFOLD_DIR:-$ROLE_DIR/.runtime-scaffold}"
if [[ ! -d "$RUNTIME_SCAFFOLD_DIR" ]]; then
  RUNTIME_SCAFFOLD_DIR="${HERMES_TEMPLATE_RUNTIME_SCAFFOLD:-$(config_get fleet.runtime_scaffold_dir "$HOME/code/hermes-agent-template/runtime-scaffold")}"
fi
REGISTRY_FILE="${REGISTRY_FILE:-${HERMES_FLEET_REGISTRY_FILE:-$(config_get fleet.registry_file "$HOME/.hermes/agents-registry.yaml")}}"

# Cross-process serialization for fleet identity claims and registry updates.
# The lock file may remain on disk; flock ownership is kernel-scoped, so a
# crashed provisioner cannot leave a permanently stale lock.
FLEET_LOCK_FD=""
fleet_lock_acquire() {
  command -v flock >/dev/null 2>&1 || die "flock is required for safe fleet registry updates"
  local lock_file="${REGISTRY_FILE}.lock"
  mkdir -p "$(dirname "$lock_file")"
  [[ ! -L "$lock_file" ]] || die "refusing fleet lock symlink: $lock_file"
  exec {FLEET_LOCK_FD}>"$lock_file"
  chmod 600 "$lock_file"
  flock -w "${FLEET_LOCK_TIMEOUT_SECONDS:-30}" "$FLEET_LOCK_FD" \
    || die "timed out waiting for fleet registry lock: $lock_file"
}

fleet_lock_release() {
  if [[ "${FLEET_LOCK_FD:-}" =~ ^[0-9]+$ ]]; then
    flock -u "$FLEET_LOCK_FD" 2>/dev/null || true
    exec {FLEET_LOCK_FD}>&-
  fi
  FLEET_LOCK_FD=""
}

# Bloodbank / NATS
BLOODBANK_NATS_HOST="${BLOODBANK_NATS_HOST:-$(config_get bloodbank.nats_host '127.0.0.1')}"
BLOODBANK_NATS_PORT="${BLOODBANK_NATS_PORT:-$(config_get bloodbank.nats_port '4222')}"
BLOODBANK_COMPOSE_DIR="${BLOODBANK_COMPOSE_DIR:-$(config_get bloodbank.compose_dir "$HOME/code/33GOD/bloodbank")}"

# Plane
PLANE_BASE="${PLANE_BASE:-$(config_get plane.base 'https://plane.delo.sh')}"
if [[ "$SKIP_PLANE" != "1" ]]; then
  PLANE_API_KEY="${PLANE_API_KEY:-${PLANE_33GOD_API_KEY:-}}"
fi

export FLEET_ENV HERMES_BIN HERMES_AGENT_REPO PJANGLER_BIN HERMES_RUNTIME_GIT_URL \
       HERMES_RUNTIME_GIT_REF HERMES_RUNTIME_GIT_SHA HERMES_OAUTH_FILE CODEX_HOME \
       RUNTIME_SCAFFOLD_DIR REGISTRY_FILE \
       BLOODBANK_NATS_HOST BLOODBANK_NATS_PORT BLOODBANK_COMPOSE_DIR \
       PLANE_BASE SKIP_PLANE
if [[ "$SKIP_PLANE" != "1" ]]; then
  export PLANE_API_KEY
fi

# systemd --user health check. Accept running/degraded/starting — only one
# broken unit shouldn't disqualify the rest of the user manager.
systemd_user_available() {
  command -v systemctl >/dev/null || return 1
  local state; state=$(systemctl --user is-system-running 2>&1)
  [[ "$state" =~ ^(running|degraded|starting|maintenance)$ ]]
}

# Query one systemd user-unit state without conflating every non-zero exit with
# an inactive/disabled unit. Output is `ok|<state>` only for documented
# state/exit-code pairs; D-Bus, manager, and arbitrary query failures become
# `error|<safe summary>` so retirement callers can fail closed.
systemctl_user_unit_state() {
  local query="$1" unit="$2" output rc first_line
  command -v systemctl >/dev/null 2>&1 \
    || { printf 'error|systemctl unavailable'; return 0; }
  set +e
  output="$(LC_ALL=C systemctl --user "$query" "$unit" 2>&1)"
  rc=$?
  set -e
  first_line="${output%%$'\n'*}"
  first_line="${first_line//$'\t'/ }"
  first_line="${first_line//|/}"
  case "$query:$rc:$first_line" in
    is-active:0:active|is-active:0:reloading|is-active:0:activating|is-active:0:deactivating)
      printf 'ok|%s' "$first_line" ;;
    is-active:3:inactive|is-active:3:failed)
      printf 'ok|%s' "$first_line" ;;
    is-active:4:inactive)
      printf 'ok|not-found' ;;
    is-enabled:0:enabled|is-enabled:0:enabled-runtime|is-enabled:0:linked|is-enabled:0:linked-runtime|is-enabled:0:alias|is-enabled:0:static|is-enabled:0:indirect|is-enabled:0:generated|is-enabled:0:transient)
      printf 'ok|%s' "$first_line" ;;
    is-enabled:1:disabled|is-enabled:1:masked|is-enabled:1:masked-runtime)
      printf 'ok|%s' "$first_line" ;;
    is-enabled:4:not-found)
      printf 'ok|not-found' ;;
    *)
      [[ -n "$first_line" ]] || first_line="exit $rc with no state"
      printf 'error|%s' "$first_line" ;;
  esac
}

# Resolve project repo path (the repo that holds agents/hermes/<role>/).
# Walk up from $ROLE_DIR until we find a git root that isn't us.
project_repo_path() {
  # Structured provisioners know the project root even before a fresh target
  # receives its own .git directory. Accept only a root that contains this
  # exact role path; otherwise fail closed instead of walking into a parent
  # checkout and mutating its manifest.
  if [[ -n "${PJANGLER_PROJECT_ROOT:-}" ]]; then
    local explicit role_real
    explicit="$(cd "$PJANGLER_PROJECT_ROOT" 2>/dev/null && pwd -P)" || return 1
    role_real="$(cd "$ROLE_DIR" 2>/dev/null && pwd -P)" || return 1
    case "$role_real" in
      "$explicit"/agents/hermes/*) printf '%s\n' "$explicit"; return 0 ;;
      *) return 1 ;;
    esac
  fi
  local d="$ROLE_DIR"
  [[ -d "$d/.git" || -f "$d/.git" ]] && { echo "$d"; return 0; }
  for _ in 1 2 3 4 5; do
    d="$(dirname "$d")"
    [[ -d "$d/.git" || -f "$d/.git" ]] && { echo "$d"; return 0; }
  done
  return 1
}
