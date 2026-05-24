#!/usr/bin/env bash
# Append this agent's entry to the global fleet registry.
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

mkdir -p "$(dirname "$REGISTRY_FILE")"
if [[ ! -f "$REGISTRY_FILE" ]]; then
  cat > "$REGISTRY_FILE" <<'YAML'
# Hermes agent fleet registry.
# One entry per provisioned agent. Managed by hermes-agent-template/.scripts/80-registry.sh.
schema_version: 1
agents: {}
YAML
fi

PROJECT_PATH="$(project_repo_path)" || PROJECT_PATH=""
PLANE_PROJECT_ID="$(cat "$ROLE_DIR/.scripts/.plane-project-id" 2>/dev/null || true)"
CF_RULE_ID="$(cat "$ROLE_DIR/.scripts/.cf-rule-id" 2>/dev/null || true)"

log "[80] appending to fleet registry: $REGISTRY_FILE"

python3 - "$REGISTRY_FILE" "$AGENT_ID" "$REPO" "$ROLE" "$DISPLAY_NAME" \
  "$PROJECT_PATH" "$ROLE_DIR" "$PROFILE_NAME" \
  "$BOT_HANDLE" "$EMAIL_ADDR" "$FORWARD_TO" "$CF_RULE_ID" \
  "$PLANE_WORKSPACE" "$PLANE_PROJECT_ID" "$(yaml_get plane.identifier)" \
  "$RUNTIME_REPO" "hermes-${AGENT_ID}-gateway.service" \
  "hermes-${AGENT_ID}-consumer.service" "hermes-${AGENT_ID}-checkpoint.timer" <<'PYEOF'
import sys, pathlib, datetime
try:
    import yaml  # type: ignore
except ImportError:
    sys.exit("PyYAML required; pip install pyyaml")
(path, agent_id, repo, role, display, project, role_dir, profile, bot, email, forward,
 cf_rule, plane_ws, plane_id, plane_ident, runtime_repo, gw, csm, ckpt) = sys.argv[1:20]
p = pathlib.Path(path)
data = yaml.safe_load(p.read_text()) or {"schema_version": 1, "agents": {}}
data.setdefault("agents", {})[agent_id] = {
  "repo": repo, "role": role, "display_name": display,
  "project_path": project, "role_dir": role_dir,
  "profile_name": profile,
  "telegram": {"bot_username": bot},
  "email": {"address": email, "forward_to": forward, "rule_id": cf_rule},
  "plane": {"workspace": plane_ws, "project_id": plane_id, "identifier": plane_ident},
  "runtime_repo": runtime_repo,
  "systemd": {"gateway_unit": gw, "consumer_unit": csm, "checkpoint_timer": ckpt},
  "provisioned_at": datetime.datetime.utcnow().isoformat() + "Z",
}
p.write_text(yaml.safe_dump(data, sort_keys=False))
PYEOF

mark_done 80-registry
