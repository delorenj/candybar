#!/usr/bin/env bash
# check-deploy-hygiene.sh — the AD-15 grep gate (Story 1.9).
#
# Deploy surfaces must never carry LAN IPs or plaintext secrets: service
# references are hostnames/docker aliases, secrets are op://DeLoSecrets/...
# references resolved at deploy time. Runs in CI on every push and locally
# via tests/test_deploy_hygiene.py.
#
# Scope is the deployment envelope (Dockerfile, deploy/, src/, env/docker
# ignore files): test fixtures and CI service definitions legitimately use
# throwaway loopback credentials (postgres candybar:candybar) and are NOT
# deploy surfaces — keep them out or the gate cries wolf.
#
# Usage: check-deploy-hygiene.sh [repo-root]   (default: git toplevel)
# Exit 0 clean; exit 1 with the offending lines on any hit.

set -uo pipefail

ROOT="${1:-$(git rev-parse --show-toplevel)}"
cd "$ROOT"

TARGETS=()
for path in Dockerfile .dockerignore deploy src; do
    [[ -e "$path" ]] && TARGETS+=("$path")
done
# Root-level env templates only; the resolved .env is gitignored and would
# (correctly) fail this gate — if it is ever committed, that IS the fire.
for path in .env.template compose.yml compose.yaml; do
    [[ -f "$path" ]] && TARGETS+=("$path")
done

if [[ ${#TARGETS[@]} -eq 0 ]]; then
    echo "check-deploy-hygiene: no deploy surfaces found under $ROOT" >&2
    exit 1
fi

FAIL=0

report() {
    local label="$1" pattern="$2"
    local hits
    hits=$(rg --no-heading -n -e "$pattern" -- "${TARGETS[@]}" 2>/dev/null)
    if [[ -n "$hits" ]]; then
        echo "check-deploy-hygiene: FAIL ($label)" >&2
        echo "$hits" >&2
        FAIL=1
    fi
}

# RFC1918 LAN addresses (loopback 127.x is fine; docker-internal aliases and
# host.docker.internal are names, not IPs).
report "LAN IP" \
    '192\.168\.[0-9]{1,3}\.[0-9]{1,3}|10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}'

# Private key material.
report "private key block" \
    '\-BEGIN [A-Z ]*PRIVATE KEY\-'

# Secret-shaped assignments, line-filtered: op:// references, shell ${...}
# interpolation and empty values are the sanctioned shapes.
while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    case "$line" in
        *op://*|*'${'*) continue ;;
    esac
    echo "check-deploy-hygiene: FAIL (plaintext secret)" >&2
    echo "$line" >&2
    FAIL=1
done < <(rg --no-heading -n -i \
    '(password|passwd|secret|api[_-]?key|access[_-]?token)["'"'"']?\s*[:=]\s*["'"'"']?[A-Za-z0-9_/+.@-]{8,}' \
    -- "${TARGETS[@]}" 2>/dev/null)

# DSN userinfo passwords (scheme://user:PASSWORD@host). op:// references are
# stripped before the re-check so `...:op://...@...` is sanctioned.
while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    stripped=$(printf '%s' "$line" | sed 's|op://[^@/ "]*||g')
    if printf '%s' "$stripped" | rg -q '[a-zA-Z][a-zA-Z0-9+.-]*://[^/@\s:]+:[^/@\s]+@'; then
        echo "check-deploy-hygiene: FAIL (DSN userinfo password)" >&2
        echo "$line" >&2
        FAIL=1
    fi
done < <(rg --no-heading -n '[a-zA-Z][a-zA-Z0-9+.-]*://[^/@\s:]+:[^/@\s]+@' \
    -- "${TARGETS[@]}" 2>/dev/null)

if [[ "$FAIL" -eq 0 ]]; then
    echo "check-deploy-hygiene: OK (${#TARGETS[@]} surfaces scanned)"
fi
exit "$FAIL"
