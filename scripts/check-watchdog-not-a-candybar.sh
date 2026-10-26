#!/usr/bin/env bash
# check-watchdog-not-a-candybar.sh — the AD-22 gate (Story 7.6).
#
# candybar-watchdog must NEVER be registered as a candybar: a scheduler
# cannot be its own watchdog. This gate fails the build if a candybar
# manifest or registration anywhere in the repo names the watchdog.
#
# Fails LOUDLY when ripgrep is missing (an empty scan printing OK is a green
# CI lying), matching scripts/check-deploy-hygiene.sh.
set -euo pipefail

if ! command -v rg >/dev/null 2>&1; then
    echo "check-watchdog-not-a-candybar: ERROR: ripgrep (rg) not on PATH — cannot scan" >&2
    exit 2
fi

# A candybar manifest's name is its registration. The watchdog script, unit
# names, state dir and docs legitimately mention "candybar-watchdog"; only
# manifest name fields and registration payloads are gated.
if rg -n --glob 'candybar.toml' '^\s*name\s*=\s*"candybar-watchdog' . ; then
    echo "check-watchdog-not-a-candybar: FAIL — candybar-watchdog appears as a candybar manifest name (AD-22: never a candybar)" >&2
    exit 1
fi
if rg -n '"name"\s*:\s*"candybar-watchdog' --glob '*.json' . ; then
    echo "check-watchdog-not-a-candybar: FAIL — candybar-watchdog appears in a registration JSON (AD-22: never a candybar)" >&2
    exit 1
fi

echo "check-watchdog-not-a-candybar: OK (watchdog is not a candybar)"
