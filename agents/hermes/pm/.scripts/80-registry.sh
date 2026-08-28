#!/usr/bin/env bash
# Append this agent's entry to the global fleet registry.

if [[ "${SKIP_HOST_STATE:-0}" == "1" ]]; then
  printf '%s\n' '[80] fleet registry — DEFERRED (SKIP_HOST_STATE=1)' >&2
  exit 0
fi

# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

fleet_lock_acquire
trap 'fleet_lock_release' EXIT

[[ ! -L "$REGISTRY_FILE" ]] || die "refusing to update registry symlink: $REGISTRY_FILE"
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

log "[80] appending to fleet registry: $REGISTRY_FILE"

python3 - "$REGISTRY_FILE" "$AGENT_ID" "$REPO" "$ROLE" "$DISPLAY_NAME" \
  "$PROJECT_PATH" "$ROLE_DIR" "$PROFILE_NAME" \
  "$(yaml_get telegram.provisioning_status)" "$BOT_HANDLE" "$(yaml_get telegram.bot_id)" \
  "$(yaml_get slack.provisioning_status)" "$(yaml_get slack.team_id)" \
  "$(yaml_get slack.team_name)" "$(yaml_get slack.bot_user_id)" \
  "$(yaml_get slack.bot_id)" "$(yaml_get slack.bot_username)" \
  "$(yaml_get bloodbank.enabled)" "$(yaml_get bloodbank.gateway_scope)" \
  "$(yaml_get bloodbank.target_agent_id)" \
  "$PLANE_WORKSPACE" "$PLANE_PROJECT_ID" "$(yaml_get plane.identifier)" \
  "$RUNTIME_REPO" "$HERMES_BIN" "$HERMES_AGENT_REPO" "$HERMES_RUNTIME_GIT_URL" \
  "$HERMES_RUNTIME_GIT_REF" "$HERMES_RUNTIME_GIT_SHA" "$FLEET_ENV" \
  "hermes-${AGENT_ID}-gateway.service" "hermes-${AGENT_ID}-heartbeat.timer" <<'PYEOF'
import datetime
import errno
import os
import pathlib
import sys
import tempfile
try:
    import yaml  # type: ignore
except ImportError:
    sys.exit("PyYAML required; pip install pyyaml")
(path, agent_id, repo, role, display, project, role_dir, profile,
 telegram_status, bot, telegram_bot_id,
 slack_status, slack_team_id, slack_team_name, slack_user_id, slack_bot_id,
 slack_username, bloodbank_enabled, bloodbank_scope, bloodbank_target, plane_ws, plane_id,
 plane_ident, runtime_repo, hermes_bin, hermes_repo, hermes_git_url,
 hermes_git_ref, hermes_git_sha, fleet_env, gw, heartbeat) = sys.argv[1:33]
p = pathlib.Path(path)
if p.is_symlink():
    raise SystemExit(f"refusing to update registry symlink: {p}")
data = yaml.safe_load(p.read_text()) or {"schema_version": 1, "agents": {}}
if bloodbank_enabled == "":
    bloodbank_enabled_value = False
elif bloodbank_enabled == "true":
    bloodbank_enabled_value = True
elif bloodbank_enabled == "false":
    bloodbank_enabled_value = False
else:
    raise SystemExit("bloodbank.enabled must be the strict YAML boolean true or false")
data.setdefault("agents", {})[agent_id] = {
  "repo": repo, "role": role, "display_name": display,
  "project_path": project, "role_dir": role_dir,
  "profile_name": profile,
  "telegram": {
    "provisioning_status": telegram_status,
    "bot_username": bot,
    "bot_id": telegram_bot_id,
  },
  "slack": {
    "provisioning_status": slack_status,
    "team_id": slack_team_id,
    "team_name": slack_team_name,
    "bot_user_id": slack_user_id,
    "bot_id": slack_bot_id,
    "bot_username": slack_username,
  },
  "bloodbank": {
    "enabled": bloodbank_enabled_value,
    "gateway_scope": bloodbank_scope,
    "target_agent_id": bloodbank_target,
  },
  "plane": {"workspace": plane_ws, "project_id": plane_id, "identifier": plane_ident},
  "runtime_repo": runtime_repo,
  "hermes": {
    "bin": hermes_bin,
    "repo": hermes_repo,
    "git_url": hermes_git_url,
    "git_ref": hermes_git_ref,
    "git_sha": hermes_git_sha,
    "fleet_env": fleet_env,
  },
  "systemd": {"gateway_unit": gw, "heartbeat_timer": heartbeat},
  "provisioned_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
}
rendered = yaml.safe_dump(data, sort_keys=False)
p.parent.mkdir(parents=True, exist_ok=True)

def fsync_parent(target):
    unsupported = {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL), errno.ENOSYS}
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_fd = os.open(target.parent, flags)
    except OSError as exc:
        if exc.errno in unsupported:
            return
        raise
    try:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno not in unsupported:
                raise
    finally:
        os.close(directory_fd)

fd, temporary = tempfile.mkstemp(prefix=f".{p.name}.registry-", dir=p.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, p)
    os.chmod(p, 0o600)
    fsync_parent(p)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PYEOF

fleet_lock_release
trap - EXIT

mark_done 80-registry
