#!/usr/bin/env bash
# Print the final summary and next steps.
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

GW_UNIT="hermes-${AGENT_ID}-gateway.service"
HB_TIMER="hermes-${AGENT_ID}-heartbeat.timer"

cat >&2 <<EOF

╭─ Provisioned: $AGENT_ID ───────────────────────────────────────────────╮
│
│  Talk:        @$BOT_HANDLE  (Telegram DM)
│  Shell:       $ROLE_DIR/hermes chat "status"
│  Board:       $PLANE_BASE/$PLANE_WORKSPACE/projects/$(cat "$ROLE_DIR/.scripts/.plane-project-id" 2>/dev/null || echo "(skipped)")
│  Runtime:     $ROLE_DIR/runtime   (pure-local, ignored)
│  Hermes bin:  $HERMES_BIN
│  Fleet env:   $FLEET_ENV
│
│  Start fleet daemons:
│    systemctl --user start $GW_UNIT
│    systemctl --user start $HB_TIMER
│
│  Tail logs:
│    journalctl --user -fu $GW_UNIT
│
│  Fleet status:
│    python3 -c "import yaml,pathlib; print(yaml.safe_dump(yaml.safe_load(pathlib.Path('$REGISTRY_FILE').read_text())['agents']))"
│
╰────────────────────────────────────────────────────────────────────────╯

EOF
mark_done 99-summary
