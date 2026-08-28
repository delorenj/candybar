#!/usr/bin/env bash
# Compatibility checkpoint for the fleet-shared Bloodbank gateway contract.
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

already_done 60-bloodbank && { log "[60] bloodbank fleet-routing checkpoint already complete — skipping"; exit 0; }

# SKIP_BLOODBANK used to suppress per-profile consumer installation. Keep the
# flag harmless for existing unattended provisioning commands; there is no
# profile-local process, dependency, broker probe, or file to skip anymore.
if [[ "${SKIP_BLOODBANK:-0}" == "1" ]]; then
  log "[60] bloodbank — SKIP_BLOODBANK accepted as a compatibility no-op"
else
  log "[60] bloodbank — fleet-shared gateway routes target_agent_id=$AGENT_ID"
fi

mark_done 60-bloodbank
