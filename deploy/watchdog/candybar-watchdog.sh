#!/usr/bin/env bash
# candybar-watchdog — the off-candybar self-watchdog (AD-22, Story 7.6).
#
# Runs every 2 minutes from candybar-watchdog.timer (systemd --user). It is
# deliberately NOT a candybar: a scheduler must never be its own watchdog.
#
# Three checks, all must pass:
#   1. https://candybar.delo.sh/healthz answers 200 (heartbeat/dispatcher/api
#      container through the whole edge path).
#   2. The every-minute canary candybar (default name: watchdog-canary) has a
#      terminal invocation within the last 3 minutes, counted by START time
#      (startedAt): a run wedged mid-execution is as dead as one never fired.
#   3. candybar-executor.service is active (the host executor, AD-17).
#
# Paging: exactly one ntfy page on the OK->FAIL transition and one on
# recovery (state file), via ntfy-alert. Never re-pages while an outage
# persists; page_repeat is not wanted at a 2-minute cadence.
#
# Ok-stamp: touched only when all three checks pass. infra-doctor watches the
# stamp with a file_age probe, so a dead watchdog timer goes red there.
#
# Exit code: always 0 (check failures are normal operation for a watchdog).
# A page-send failure is logged but does not change the exit code either:
# the unit runs every 2 minutes and must never flap.
set -uo pipefail

BASE_URL="${CANDYBAR_WATCHDOG_URL:-https://candybar.delo.sh}"
CANARY_NAME="${CANDYBAR_WATCHDOG_CANARY:-watchdog-canary}"
EXECUTOR_UNIT="${CANDYBAR_WATCHDOG_UNIT:-candybar-executor.service}"
MAX_CANARY_AGE_S="${CANDYBAR_WATCHDOG_MAX_CANARY_AGE_S:-180}"
STATE_DIR="${CANDYBAR_WATCHDOG_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/candybar-watchdog}"
STAMP="$STATE_DIR/ok"
STATE="$STATE_DIR/state"        # "ok" | "fail"
STATE_TMP="$STATE_DIR/.state.tmp.$$"
FAILS="$STATE_DIR/failing"      # newline-separated failing check names
FAILS_TMP="$STATE_DIR/.failing.tmp.$$"
NTFY_ALERT="${CANDYBAR_WATCHDOG_NTFY_ALERT:-$HOME/.local/bin/ntfy-alert}"

# A torn state file (killed mid-write) must never read as "fail" on the next
# run: that would suppress the first real outage page. Writes are tmp+rename.
trap 'rm -f "$STATE_TMP" "$FAILS_TMP"' EXIT
write_state() { printf '%s\n' "$1" > "$STATE_TMP" && mv "$STATE_TMP" "$STATE"; }

# Test seams (default real tools):
CURL="${CANDYBAR_WATCHDOG_CURL:-curl}"
JQ="${CANDYBAR_WATCHDOG_JQ:-jq}"
SYSTEMCTL="${CANDYBAR_WATCHDOG_SYSTEMCTL:-systemctl --user}"

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

now() { date +%s; }

failures=()

# --- check 1: healthz -------------------------------------------------------
code="$("$CURL" -fsS -m 10 -o /dev/null -w '%{http_code}' "$BASE_URL/healthz" 2>/dev/null || echo 000)"
if [[ "$code" != "200" ]]; then
  failures+=("healthz (HTTP $code)")
fi

# --- check 2: canary freshness ----------------------------------------------
# Only meaningful when the API answered; an unreachable API already fails
# check 1 and a canary read would double-report the same root cause.
if [[ "$code" == "200" ]]; then
  canary_json="$("$CURL" -fsS -m 10 "$BASE_URL/candybars" 2>/dev/null || echo '')"
  canary_id="$(printf '%s' "$canary_json" | "$JQ" -r --arg n "$CANARY_NAME" '.[] | select(.name == $n) | .id' 2>/dev/null | head -n1)"
  if [[ -z "$canary_id" ]]; then
    failures+=("canary ($CANARY_NAME not registered)")
  else
    inv_json="$("$CURL" -fsS -m 10 "$BASE_URL/candybars/$canary_id/invocations?limit=1" 2>/dev/null || echo '')"
    # Freshness is counted from startedAt: a claimed-but-wedged run (result
    # null, finishedAt null) ages out exactly like a fire that never ran.
    started="$(printf '%s' "$inv_json" | "$JQ" -r '.invocations[0].startedAt // empty' 2>/dev/null)"
    result="$(printf '%s' "$inv_json" | "$JQ" -r '.invocations[0].result // "none"' 2>/dev/null)"
    if [[ -z "$started" ]]; then
      failures+=("canary (no invocation)")
    else
      started_epoch="$(date -d "$started" +%s 2>/dev/null || echo 0)"
      age=$(( $(now) - started_epoch ))
      if [[ "$started_epoch" == "0" || "$age" -gt "$MAX_CANARY_AGE_S" ]]; then
        failures+=("canary (last invocation started ${age}s ago, limit ${MAX_CANARY_AGE_S}s)")
      elif [[ "$result" != "success" && "$result" != "null" && "$result" != "none" ]]; then
        # result null = the newest fire is still running: fresh, no verdict yet.
        failures+=("canary (last invocation result: $result)")
      fi
    fi
  fi
fi

# --- check 3: host executor unit ---------------------------------------------
if ! $SYSTEMCTL is-active --quiet "$EXECUTOR_UNIT" 2>/dev/null; then
  failures+=("executor ($EXECUTOR_UNIT not active)")
fi

# --- state transition + paging ------------------------------------------------
prev="$(cat "$STATE" 2>/dev/null || echo ok)"

page() { # page <title> <message>
  local title="$1"; shift
  if ! "$NTFY_ALERT" --title "$title" --tags "candybar,warning" "$@" >/dev/null 2>&1; then
    echo "candybar-watchdog: page send failed ($title)" >&2
  fi
}

if (( ${#failures[@]} > 0 )); then
  printf '%s\n' "${failures[@]}" > "$FAILS_TMP" && mv "$FAILS_TMP" "$FAILS"
  write_state fail
  if [[ "$prev" != "fail" ]]; then
    page "candybar DOWN" "candybar-watchdog FAIL on $(hostname -s): $(IFS='; '; echo "${failures[*]}"). No re-page until recovery; watch https://candybar.delo.sh/healthz."
  fi
  exit 0
fi

# all green
touch "$STAMP"
rm -f "$FAILS"
write_state ok
if [[ "$prev" == "fail" ]]; then
  page "candybar RECOVERED" "candybar-watchdog green again on $(hostname -s): healthz, canary and executor all pass."
fi
exit 0
