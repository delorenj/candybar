#!/usr/bin/env bash
# Capture a BotFather token and wire it into the runtime profile.
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

already_done 30-telegram && { log "[30] telegram already wired — skipping"; exit 0; }

if [[ "${SKIP_TELEGRAM:-0}" == "1" ]]; then
  log "[30] telegram — SKIPPED (SKIP_TELEGRAM=1)"
  mark_done 30-telegram
  exit 0
fi

RUNTIME="$ROLE_DIR/runtime"

cat >&2 <<EOF

╭─ BotFather steps for @$BOT_HANDLE ─────────────────────────────────────╮
│ 1. Open Telegram, message @BotFather                                   │
│ 2. /newbot                                                             │
│ 3. Display name:   $DISPLAY_NAME                                       │
│ 4. Username:       $BOT_HANDLE  (must end in _bot)                     │
│ 5. Copy the HTTP API token from the reply.                             │
│ 6. /setjoingroups → Disable                                            │
│ 7. /setprivacy    → Disable                                            │
╰────────────────────────────────────────────────────────────────────────╯

EOF

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  read -r -s -p "Paste the bot token for @$BOT_HANDLE (or empty to skip): " TELEGRAM_BOT_TOKEN
  echo >&2
fi

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  warn "    no token provided; Telegram step deferred"
  warn "    re-run later:  cd $ROLE_DIR && SKIP_TELEGRAM=0 ./.scripts/30-telegram.sh"
  exit 0
fi

# Sanity check
if [[ ! "$TELEGRAM_BOT_TOKEN" =~ ^[0-9]+:.+ ]]; then
  die "token doesn't look like a Telegram bot token"
fi
log "    verifying token..."
info=$(curl -sS "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe")
echo "$info" | grep -q '"ok":true' || die "Telegram rejected the token: $info"
bot_username=$(echo "$info" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['username'])")
bot_id=$(echo "$info"       | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['id'])")
log "    verified: @$bot_username (id=$bot_id)"

# Resolve operator's allowed user id
if [[ -z "${TELEGRAM_ALLOWED_USERS:-}" && -f "$HOME/.hermes/.env" ]]; then
  TELEGRAM_ALLOWED_USERS=$(grep -E '^[[:space:]]*#?[[:space:]]*TELEGRAM_ALLOWED_USERS=' "$HOME/.hermes/.env" \
    | tail -1 | sed -E 's/^[[:space:]]*#?[[:space:]]*TELEGRAM_ALLOWED_USERS=//; s/^"//; s/"$//')
fi
if [[ -z "${TELEGRAM_ALLOWED_USERS:-}" ]]; then
  read -r -p "Your Telegram user id (allow-list for this bot): " TELEGRAM_ALLOWED_USERS
fi

# Write into runtime/.env (this is HERMES_HOME for the agent)
ENVF="$RUNTIME/.env"
python3 - "$ENVF" "$TELEGRAM_BOT_TOKEN" "$TELEGRAM_ALLOWED_USERS" <<'PYEOF'
import sys, pathlib, re
path, token, allowed = sys.argv[1:4]
p = pathlib.Path(path); p.parent.mkdir(parents=True, exist_ok=True)
text = p.read_text() if p.exists() else ""
for var in ("TELEGRAM_BOT_TOKEN","TELEGRAM_ALLOWED_USERS"):
    text = re.sub(rf"(?m)^\s*#?\s*{var}=.*\n", "", text)
text = text.rstrip() + "\n" + f'TELEGRAM_BOT_TOKEN="{token}"\nTELEGRAM_ALLOWED_USERS="{allowed}"\n'
p.write_text(text)
PYEOF
chmod 600 "$ENVF"

# Update role.yaml with the real bot username (in case it differs from the slug)
yaml_set telegram.bot_username "$bot_username"

# Enable telegram toolset for the profile
env HERMES_HOME="$RUNTIME" "$HERMES_BIN" tools enable telegram hermes-telegram 2>/dev/null \
  || warn "    couldn't auto-enable telegram toolset; run: $ROLE_DIR/hermes tools"

log "    wired @$bot_username (id=$bot_id)"
mark_done 30-telegram
