#!/usr/bin/env bash
# Ensure the distributable config exists before any other step reads it.
# Seeds ~/.config/hermes-agent-template/config.toml from the shipped example.

# MCP render transactions establish repo-local lifecycle eligibility before
# touching any host-global state. This guard must precede _lib.sh because that
# library creates the role log and may read fleet configuration.
if [[ "${SKIP_HOST_STATE:-0}" == "1" ]]; then
  printf '%s\n' '[01] host config — DEFERRED (SKIP_HOST_STATE=1)' >&2
  exit 0
fi

# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"

already_done 01-config && { log "[01] config already checked — skipping"; exit 0; }

CONFIG="$HERMES_TEMPLATE_CONFIG"
EXAMPLE="$ROLE_DIR/.scripts/config.example.toml"

if [[ -f "$CONFIG" ]]; then
  log "[01] config present: $CONFIG"
else
  mkdir -p "$(dirname "$CONFIG")"
  if [[ -f "$EXAMPLE" ]]; then
    cp "$EXAMPLE" "$CONFIG"
    log "[01] seeded $CONFIG from shipped example"
  else
    # Last-resort minimal config if the example didn't ship with this agent.
    cat > "$CONFIG" <<'TOML'
# hermes-agent-template config — edit for your environment.
[fleet]
hermes_bin = "~/.local/share/hermes-agent/releases/0408fec7a153e6c32c064acd2b8053917f1525f1/.venv/bin/hermes"
hermes_repo = "~/.local/share/hermes-agent/releases/0408fec7a153e6c32c064acd2b8053917f1525f1"
hermes_git_url = "https://github.com/delorenj/hermes-agent.git"
hermes_git_ref = "main"
hermes_git_sha = "0408fec7a153e6c32c064acd2b8053917f1525f1"
fleet_env = "~/.hermes/fleet.env"
registry_file = "~/.hermes/agents-registry.yaml"
canonical_skills_dir = "/home/delorenj/.agents/skills"

[github]
runtime_repo_owner = "delorenj"

[plane]
base = "https://plane.delo.sh"
workspace = "33god"

[bloodbank]
nats_host = "127.0.0.1"
nats_port = 4222
compose_dir = "~/code/33GOD/bloodbank"
TOML
    log "[01] wrote built-in default config to $CONFIG"
  fi
  warn "[01] review $CONFIG and set values for YOUR environment"
  warn "     (hermes_bin, runtime_repo_owner, plane.base/workspace, …) before relying on defaults"
fi

mark_done 01-config
