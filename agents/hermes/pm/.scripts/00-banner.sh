#!/usr/bin/env bash
# Print the welcome banner and the role we're provisioning.
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

cat >&2 <<EOF

╭─ Hermes Agent Provisioning ────────────────────────────────────────────╮
│  agent_id    $AGENT_ID
│  repo        $REPO
│  role        $ROLE
│  display     $DISPLAY_NAME
│  telegram    @$BOT_HANDLE   (one bot per agent)
│  email       $EMAIL_ADDR  → $FORWARD_TO
│  plane       $PLANE_WORKSPACE workspace
│  runtime     gh:$RUNTIME_REPO   (auto-checkpointed)
│  profile     ~/.hermes/profiles/$PROFILE_NAME
╰────────────────────────────────────────────────────────────────────────╯

EOF
mark_done 00-banner
