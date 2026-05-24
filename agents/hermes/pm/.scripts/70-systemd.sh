#!/usr/bin/env bash
# Install systemd --user units: gateway, consumer, and hourly checkpoint timer.
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

already_done 70-systemd && { log "[70] systemd already installed — skipping"; exit 0; }
[[ "${SKIP_SYSTEMD:-0}" == "1" ]] && { log "[70] systemd — SKIPPED"; mark_done 70-systemd; exit 0; }

RUNTIME="$ROLE_DIR/runtime"
SYS_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYS_DIR"

# Resolve absolute path to the runtime checkpoint helper
CHECKPOINT_BIN="$ROLE_DIR/.scripts/checkpoint.sh"
cp "$(dirname "$0")/checkpoint.sh" "$CHECKPOINT_BIN" 2>/dev/null || cat > "$CHECKPOINT_BIN" <<'CKPT'
#!/usr/bin/env bash
set -euo pipefail
RUNTIME_DIR="$(cd "$(dirname "$0")/../runtime" && pwd)"
cd "$RUNTIME_DIR"
git add -A
if git diff --cached --quiet; then exit 0; fi
git -c commit.gpgsign=false commit -m "checkpoint $(date -Iseconds)" >/dev/null
git push origin HEAD 2>&1 | tail -1 || true
CKPT
chmod +x "$CHECKPOINT_BIN"

# Gateway unit
GW_UNIT="hermes-${AGENT_ID}-gateway.service"
cat > "$SYS_DIR/$GW_UNIT" <<UNIT
[Unit]
Description=Hermes Gateway — $DISPLAY_NAME
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=HERMES_HOME=$RUNTIME
ExecStart=$HERMES_BIN gateway run --replace
Restart=on-failure
RestartSec=10
StandardOutput=append:$RUNTIME/logs/gateway.systemd.log
StandardError=append:$RUNTIME/logs/gateway.systemd.log

[Install]
WantedBy=default.target
UNIT

# Consumer unit
CSM_UNIT="hermes-${AGENT_ID}-consumer.service"
cat > "$SYS_DIR/$CSM_UNIT" <<UNIT
[Unit]
Description=Bloodbank Consumer — $DISPLAY_NAME
After=network-online.target $GW_UNIT
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$RUNTIME
Environment=HERMES_HOME=$RUNTIME
ExecStart=$HERMES_AGENT_REPO/.venv/bin/python $RUNTIME/bloodbank-consumer.py
Restart=on-failure
RestartSec=5
StandardOutput=append:$RUNTIME/logs/consumer.log
StandardError=append:$RUNTIME/logs/consumer.log

[Install]
WantedBy=default.target
UNIT

# Hourly checkpoint timer
CKPT_SVC="hermes-${AGENT_ID}-checkpoint.service"
CKPT_TIMER="hermes-${AGENT_ID}-checkpoint.timer"
cat > "$SYS_DIR/$CKPT_SVC" <<UNIT
[Unit]
Description=Hermes Runtime Checkpoint — $DISPLAY_NAME

[Service]
Type=oneshot
ExecStart=$CHECKPOINT_BIN
StandardOutput=append:$RUNTIME/logs/checkpoint.log
StandardError=append:$RUNTIME/logs/checkpoint.log
UNIT
cat > "$SYS_DIR/$CKPT_TIMER" <<UNIT
[Unit]
Description=Hourly checkpoint for $AGENT_ID

[Timer]
OnBootSec=15min
OnUnitActiveSec=1h
Unit=$CKPT_SVC
Persistent=true

[Install]
WantedBy=timers.target
UNIT

mkdir -p "$RUNTIME/logs"

if systemd_user_available; then
  systemctl --user daemon-reload
  for u in "$GW_UNIT" "$CSM_UNIT" "$CKPT_TIMER"; do
    systemctl --user enable "$u" >/dev/null 2>&1 && log "    enabled: $u" || warn "    failed to enable: $u"
  done
else
  warn "    systemd --user not available; units installed at $SYS_DIR but not enabled"
fi

mark_done 70-systemd
