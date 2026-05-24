#!/usr/bin/env bash
# Create a Plane project in the 33god workspace (one project per agent).
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

already_done 40-plane && { log "[40] plane already set up — skipping"; exit 0; }
[[ "${SKIP_PLANE:-0}" == "1" ]] && { log "[40] plane — SKIPPED"; mark_done 40-plane; exit 0; }

[[ -n "$PLANE_API_KEY" ]] || { warn "[40] PLANE_API_KEY not set; skipping. set PLANE_33GOD_API_KEY and re-run ./.scripts/40-plane.sh"; exit 0; }

# Build the 5-char identifier: <repo[0..2]><role[0..1]> uppercased
RAW=$(printf '%s%s' "${REPO:0:3}" "${ROLE:0:2}" | tr -cd '[:alnum:]' | tr '[:lower:]' '[:upper:]')
while (( ${#RAW} < 3 )); do RAW="${RAW}X"; done
IDENT="${RAW:0:5}"

# Project name = display_name (already smart-cased: "Bloodbank PM", "Hermes-Agent Dev").
# Plane forbids hyphens in names — substitute spaces.
NAME="${DISPLAY_NAME//-/ }"

log "[40] creating plane project '$NAME' [$IDENT]"

# Check for existing project with this identifier
EXISTING=$(curl -sS "$PLANE_BASE/api/v1/workspaces/$PLANE_WORKSPACE/projects/?per_page=200" \
  -H "X-API-Key: $PLANE_API_KEY" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
ident = '$IDENT'
for p in d.get('results', []):
    if (p.get('identifier') or '').upper() == ident:
        print(p['id']); break")

if [[ -n "$EXISTING" ]]; then
  log "    plane project $IDENT already exists (id=$EXISTING) — reusing"
  PROJECT_ID="$EXISTING"
else
  RESP=$(curl -sS -X POST "$PLANE_BASE/api/v1/workspaces/$PLANE_WORKSPACE/projects/" \
    -H "X-API-Key: $PLANE_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$(python3 -c "import json,sys; print(json.dumps({'name': sys.argv[1], 'identifier': sys.argv[2], 'description': sys.argv[3]}))" \
        "$NAME" "$IDENT" "Hermes agent board for $AGENT_ID")")
  PROJECT_ID=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id') or '')")
  [[ -n "$PROJECT_ID" ]] || die "plane create failed: $RESP"
  log "    plane project created id=$PROJECT_ID"
fi

yaml_set plane.identifier "$IDENT"
echo "$PROJECT_ID" > "$ROLE_DIR/.scripts/.plane-project-id"
mark_done 40-plane
