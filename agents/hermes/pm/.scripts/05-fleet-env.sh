#!/usr/bin/env bash
# Ensure a single shared fleet source-of-truth file exists.

if [[ "${SKIP_HOST_STATE:-0}" == "1" ]]; then
  printf '%s\n' '[05] fleet env — DEFERRED (SKIP_HOST_STATE=1)' >&2
  exit 0
fi

# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

already_done 05-fleet-env && log "[05] fleet env already checked — refreshing shared keys"

mkdir -p "$(dirname "$FLEET_ENV")"

new_fleet_env=0
if [[ ! -f "$FLEET_ENV" ]]; then
  log "[05] creating fleet source-of-truth: $FLEET_ENV"
  new_fleet_env=1
else
  log "[05] fleet env exists: $FLEET_ENV"
fi

fleet_env_parser="$ROLE_DIR/.scripts/lib/parse-fleet-env.py"
fleet_env_python="$(builtin type -P python3)" \
  || die "python3 is required to update the fleet source-of-truth"

upsert_fleet_env() {
  local key="$1" value="$2"
  "$fleet_env_python" -I "$fleet_env_parser" --upsert "$FLEET_ENV" "$key" "$value"
}

if [[ "$new_fleet_env" == "1" ]]; then
  upsert_fleet_env HERMES_FLEET_BIN "$HERMES_BIN"
  upsert_fleet_env HERMES_FLEET_REPO "$HERMES_AGENT_REPO"
  upsert_fleet_env HERMES_FLEET_REGISTRY_FILE "$REGISTRY_FILE"
fi
upsert_fleet_env HERMES_FLEET_OAUTH_FILE "$HERMES_OAUTH_FILE"
upsert_fleet_env HERMES_FLEET_CODEX_HOME "$CODEX_HOME"

mark_done 05-fleet-env
