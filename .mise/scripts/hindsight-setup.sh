#!/usr/bin/env bash
# Provision this dev's Hindsight credentials for this project's bank.
#
# Strategy (chosen 2026-06-12): ONE shared CAF-scoped API key lives in the
# DeLoSecrets 1Password vault. This task resolves it via `op` and writes
# HINDSIGHT_API_URL + HINDSIGHT_API_KEY into the gitignored .env, which mise
# already loads — so every agent hook that shells out to `hindsight` picks the
# key up automatically (env vars outrank ~/.hindsight/config).
#
# Idempotent: updates the two HINDSIGHT_ lines in place, leaves the rest of
# .env untouched. Never prints the secret.
#
# Override the 1Password reference if the item path differs:
#   HINDSIGHT_OP_KEY_REF="op://DeLoSecrets/<item>/<field>" mise run hindsight:setup
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
API_URL="${HINDSIGHT_API_URL:-https://api.hs.delo.sh}"
OP_KEY_REF="${HINDSIGHT_OP_KEY_REF:-op://DeLoSecrets/hindsight-PROJECT/credential}"

note() { printf '  %s\n' "$*"; }

echo "Hindsight setup for this project"
note "API URL : ${API_URL}"
note "op ref  : ${OP_KEY_REF}"

if ! command -v op >/dev/null 2>&1; then
  echo "ERROR: 1Password CLI (op) not found." >&2
  note "Install it, then re-run, OR set the key manually:" >&2
  note "  hindsight configure --api-url ${API_URL} --api-key <key>" >&2
  exit 1
fi

# Resolve the secret (op handles its own auth / biometric unlock).
if ! API_KEY="$(op read "${OP_KEY_REF}" 2>/dev/null)" || [[ -z "${API_KEY}" ]]; then
  echo "ERROR: could not read ${OP_KEY_REF} from 1Password." >&2
  note "Sign in (\`op signin\`) or fix HINDSIGHT_OP_KEY_REF, then re-run." >&2
  exit 1
fi

touch "${ENV_FILE}"

# upsert KEY=VALUE in .env without echoing the value to the terminal.
upsert() {
  local key="$1" val="$2" tmp
  tmp="$(mktemp)"
  grep -v -E "^${key}=" "${ENV_FILE}" > "${tmp}" 2>/dev/null || true
  printf '%s=%s\n' "${key}" "${val}" >> "${tmp}"
  # keep file perms tight — it now holds a secret
  cat "${tmp}" > "${ENV_FILE}"
  rm -f "${tmp}"
}

chmod 600 "${ENV_FILE}" 2>/dev/null || true
upsert "HINDSIGHT_API_URL" "${API_URL}"
upsert "HINDSIGHT_API_KEY" "${API_KEY}"
chmod 600 "${ENV_FILE}" 2>/dev/null || true

echo "✓ Wrote HINDSIGHT_API_URL + HINDSIGHT_API_KEY to .env (gitignored)."
note "New shells in this repo will load them via mise. Verify now with:"
note "  HINDSIGHT_API_KEY=\"\$(op read ${OP_KEY_REF})\" hindsight health"
