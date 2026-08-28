#!/usr/bin/env bash
# Secret-free systemd entrypoint. Decrypted values are read only from the
# service manager's volatile credential directory and never printed or written.
set -euo pipefail

ROLE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ROLE_YAML="$ROLE_DIR/role.yaml"

yaml_get() {
  python3 - "$ROLE_YAML" "$1" <<'PYEOF'
import re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
parts = sys.argv[2].split(".")
if len(parts) == 2:
    match = re.search(
        rf"(?m)^{re.escape(parts[0])}:[ \t]*\n((?:[ \t]+\S.*\n?)*)", text
    )
    text = match.group(1) if match else ""
key = parts[-1]
match = re.search(rf'(?m)^\s*{re.escape(key)}:\s*"?([^"\n]*)"?\s*$', text)
print(match.group(1).strip() if match else "")
PYEOF
}

AGENT_ID="$(yaml_get agent_id)"
PROFILE_NAME="$(yaml_get profile)"
[[ -n "$AGENT_ID" && -n "$PROFILE_NAME" ]] \
  || { printf 'credential-launch: incomplete role identity\n' >&2; exit 2; }
[[ -n "${HERMES_BIN:-}" && -x "$HERMES_BIN" ]] \
  || { printf 'credential-launch: HERMES_BIN is not executable\n' >&2; exit 1; }

mode="${1:-}"
[[ "$mode" == "gateway" || "$mode" == "heartbeat" ]] \
  || { printf 'credential-launch: expected gateway or heartbeat\n' >&2; exit 2; }

FLEET_HOME="${HERMES_FLEET_HOME:-$HOME/.hermes}"
PROFILE_HOME="$FLEET_HOME/profiles/${PROFILE_NAME:-$AGENT_ID}"
[[ -d "$PROFILE_HOME" && ! -L "$PROFILE_HOME" ]] \
  || { printf 'credential-launch: named profile is not a real directory\n' >&2; exit 1; }
export HERMES_HOME="$PROFILE_HOME"

if ! REPO_ROOT="$(git -C "$ROLE_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
  REPO_ROOT="$ROLE_DIR"
fi
export TERMINAL_CWD="$REPO_ROOT"

load_credential() {
  local credential_id="$1" env_name="$2" credential_file value
  [[ "$env_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
    || { printf 'credential-launch: invalid credential environment name\n' >&2; exit 2; }
  credential_file="${CREDENTIALS_DIRECTORY:-}/$credential_id"
  [[ -n "${CREDENTIALS_DIRECTORY:-}" && -r "$credential_file" ]] || return 0
  value="$(<"$credential_file")"
  printf -v "$env_name" '%s' "$value"
  export "$env_name"
  unset value
}

load_credential telegram_bot_token TELEGRAM_BOT_TOKEN
MODEL_KEY_ENV="$(yaml_get model.key_env)"
if [[ -n "$MODEL_KEY_ENV" ]]; then
  load_credential model_api_key "$MODEL_KEY_ENV"
fi

if [[ "$mode" == "heartbeat" ]]; then
  exec "$ROLE_DIR/.scripts/heartbeat.sh"
fi

gateway_args=(gateway run --replace)
MODEL_NAME="$(yaml_get model.name)"
MODEL_PROVIDER="$(yaml_get model.provider)"
MODEL_BASE_URL="$(yaml_get model.base_url)"
MODEL_API_MODE="$(yaml_get model.api_mode)"
[[ -z "$MODEL_NAME" ]] || gateway_args+=(--model "$MODEL_NAME")
[[ -z "$MODEL_PROVIDER" ]] || gateway_args+=(--provider "$MODEL_PROVIDER")
[[ -z "$MODEL_BASE_URL" ]] || gateway_args+=(--base-url "$MODEL_BASE_URL")
[[ -z "$MODEL_API_MODE" ]] || gateway_args+=(--api-mode "$MODEL_API_MODE")
[[ -z "$MODEL_KEY_ENV" ]] || gateway_args+=(--key-env "$MODEL_KEY_ENV")

exec "$HERMES_BIN" "${gateway_args[@]}"
