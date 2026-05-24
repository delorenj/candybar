#!/usr/bin/env bash
# Print the final summary and next steps.
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

GW_UNIT="hermes-${AGENT_ID}-gateway.service"
CSM_UNIT="hermes-${AGENT_ID}-consumer.service"
CKPT_TIMER="hermes-${AGENT_ID}-checkpoint.timer"

cat >&2 <<EOF

╭─ Provisioned: $AGENT_ID ───────────────────────────────────────────────╮
│
│  Talk:        @$BOT_HANDLE  (Telegram DM)
│  Shell:       $ROLE_DIR/hermes chat "status"
│  Board:       $PLANE_BASE/$PLANE_WORKSPACE/projects/$(cat "$ROLE_DIR/.scripts/.plane-project-id" 2>/dev/null || echo "(skipped)")
│  Mail:        $EMAIL_ADDR  →  $FORWARD_TO
│  Runtime:     gh:$RUNTIME_REPO   (auto-checkpointed hourly)
│
│  Start fleet daemons:
│    systemctl --user start $GW_UNIT
│    systemctl --user start $CSM_UNIT
│    systemctl --user start $CKPT_TIMER
│
│  Tail logs:
│    journalctl --user -fu $GW_UNIT
│    journalctl --user -fu $CSM_UNIT
│
│  Fleet status:
│    python3 -c "import yaml,pathlib; print(yaml.safe_dump(yaml.safe_load(pathlib.Path('$REGISTRY_FILE').read_text())['agents']))"
│
╰────────────────────────────────────────────────────────────────────────╯

EOF
mark_done 99-summary
