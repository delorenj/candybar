#!/usr/bin/env bash
# Bind this agent to its Plane board — WITHOUT inventing a board identifier.
#
# The identifier is Plane's to assign. This step never mints one. It resolves
# the board through providers/plane.sh and persists ONLY the identifier Plane
# reports back; if Plane reports none, it dies loudly rather than writing a
# guess into role.yaml.
#
# Resolution order:
#   1. The repo-root .project.json binding (the SOT) — read back live.
#   2. Otherwise a repo-named board lookup, falling back to creation.
#
# Proposing an identifier for a BRAND-NEW board is legitimate, but it may only
# come from explicit configuration (PLANE_IDENTIFIER, or an already-recorded
# role.yaml plane.identifier). Full new-board provisioning with the
# .project.json transaction lives in 42-ticket-provider.sh.
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

already_done 40-plane && { log "[40] plane already set up — skipping"; exit 0; }
[[ "${SKIP_PLANE:-0}" == "1" ]] && { log "[40] plane — SKIPPED"; mark_done 40-plane; exit 0; }

[[ -n "$PLANE_API_KEY" ]] || { warn "[40] PLANE_API_KEY not set; skipping. set PLANE_33GOD_API_KEY and re-run ./.scripts/40-plane.sh"; exit 0; }

PLANE_ADAPTER="$(dirname "$0")/providers/plane.sh"
[[ -x "$PLANE_ADAPTER" ]] || die "[40] missing Plane adapter: $PLANE_ADAPTER"

# The board belongs to the REPO, not to this role. display_name carries the
# role suffix ("Holocene PM") and must never become the board name — that is
# how role-suffixed duplicate boards get created. Matches 42-ticket-provider.sh.
NAME="$(printf '%s' "$REPO" | tr '_-' '  ' | python3 -c 'import sys; print(" ".join(w[:1].upper()+w[1:] for w in sys.stdin.read().split()))')"

# Identifier PROPOSAL, used only in a create request and never persisted as
# though it were confirmed. Explicit configuration only — never derived from
# the repo or role strings.
PROPOSED_IDENT="${PLANE_IDENTIFIER:-$(yaml_get plane.identifier)}"

# 1. Prefer the recorded .project.json binding and read it back live.
OUT="$("$PLANE_ADAPTER" resolve 2>/dev/null || true)"
if [[ -n "$OUT" ]]; then
  log "[40] resolving plane board from the .project.json binding"
else
  log "[40] no bound board — resolving/creating repo board '$NAME'${PROPOSED_IDENT:+ (proposed identifier $PROPOSED_IDENT)}"
  OUT="$("$PLANE_ADAPTER" create_board "$NAME" "$PROPOSED_IDENT" "Hermes agent board for $AGENT_ID")" \
    || die "[40] plane board resolution failed for '$NAME'"
fi

PROJECT_ID="$(printf '%s' "$OUT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("board_id","") or "")')"
LIVE_IDENTIFIER="$(printf '%s' "$OUT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("identifier","") or "")')"

[[ -n "$PROJECT_ID" ]] || die "[40] plane returned no board id: $OUT"
[[ -n "$LIVE_IDENTIFIER" ]] \
  || die "[40] plane returned no authoritative identifier — refusing to persist a guess: $OUT"

log "    plane board id=$PROJECT_ID identifier=$LIVE_IDENTIFIER (authoritative, read back from Plane)"

yaml_set plane.identifier "$LIVE_IDENTIFIER"
echo "$PROJECT_ID" > "$ROLE_DIR/.scripts/.plane-project-id"
mark_done 40-plane
