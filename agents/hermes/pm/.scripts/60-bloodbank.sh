#!/usr/bin/env bash
# Install the bloodbank consumer + envelope helper into the runtime submodule.
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

already_done 60-bloodbank && { log "[60] bloodbank already installed — skipping"; exit 0; }
[[ "${SKIP_BLOODBANK:-0}" == "1" ]] && { log "[60] bloodbank — SKIPPED"; mark_done 60-bloodbank; exit 0; }

RUNTIME="$ROLE_DIR/runtime"
[[ -d "$RUNTIME" ]] || die "runtime not initialized; run 20-runtime-repo.sh first"

log "[60] installing bloodbank consumer for $AGENT_ID"

# The consumer.py template lives in the runtime-scaffold and was already
# copied in step 20. We just need to verify it's there and tweak per-agent
# values (substituted at scaffold-copy time, but we double-check).
CONSUMER="$RUNTIME/bloodbank-consumer.py"
[[ -f "$CONSUMER" ]] || die "consumer template missing: $CONSUMER"
chmod +x "$CONSUMER"

# Health check: is NATS up?
if (echo > /dev/tcp/$BLOODBANK_NATS_HOST/$BLOODBANK_NATS_PORT) 2>/dev/null; then
  log "    NATS reachable at $BLOODBANK_NATS_HOST:$BLOODBANK_NATS_PORT"
else
  warn "    NATS not reachable; consumer will retry on start"
  warn "    bring up:  cd ~/code/33GOD/bloodbank && docker compose -f compose/docker-compose.yml up -d"
fi

# Ensure nats-py is available in the hermes venv (uv-managed, no pip binary)
if ! "$HERMES_AGENT_REPO/.venv/bin/python" -c "import nats" 2>/dev/null; then
  warn "    python nats-py not installed in hermes venv; installing via uv"
  if command -v uv >/dev/null 2>&1; then
    (cd "$HERMES_AGENT_REPO" && uv pip install --quiet --python .venv/bin/python nats-py 2>&1 | tail -3) || true
  else
    warn "    uv not available either — install nats-py manually: cd $HERMES_AGENT_REPO && uv pip install nats-py"
  fi
fi

mark_done 60-bloodbank
