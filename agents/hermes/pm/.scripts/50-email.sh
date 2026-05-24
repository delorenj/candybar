#!/usr/bin/env bash
# Cloudflare Email Routing rule:  <agent>@delo.sh -> jaradd@gmail.com
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

already_done 50-email && { log "[50] email already wired — skipping"; exit 0; }
[[ "${SKIP_EMAIL:-0}" == "1" ]] && { log "[50] email — SKIPPED"; mark_done 50-email; exit 0; }

# Resolve token (env > 1Password > skip with note)
if [[ -z "${CF_EMAIL_ROUTING_TOKEN:-}" ]]; then
  if command -v op >/dev/null 2>&1; then
    CF_EMAIL_ROUTING_TOKEN=$(op read 'op://DeLoSecrets/Cloudflare-EmailRouting/token' 2>/dev/null || true)
  fi
fi
if [[ -z "${CF_EMAIL_ROUTING_TOKEN:-}" ]]; then
  warn "[50] no CF Email Routing token found."
  warn "     Create one at https://dash.cloudflare.com/profile/api-tokens with:"
  warn "       Zone (delo.sh)  - Email Routing Rules: Edit"
  warn "       Zone (delo.sh)  - Email Routing Settings: Read"
  warn "       Account         - Email Routing Addresses: Read"
  warn "     Store at op://DeLoSecrets/Cloudflare-EmailRouting/token, then re-run:"
  warn "       cd $ROLE_DIR && ./.scripts/50-email.sh"
  exit 0
fi

log "[50] cloudflare email routing rule: $EMAIL_ADDR -> $FORWARD_TO"

# Look for an existing rule with this 'to' matcher
EXISTING=$(curl -sS "$CF_API/zones/$CF_ZONE_DELO_SH/email/routing/rules?per_page=200" \
  -H "Authorization: Bearer $CF_EMAIL_ROUTING_TOKEN" \
  | python3 -c "
import sys, json
addr = '$EMAIL_ADDR'
d = json.load(sys.stdin)
if not d.get('success'):
    sys.stderr.write(f'CF error: {d.get(chr(34)+\"errors\"+chr(34))}\n')
    sys.exit(2)
for rule in d.get('result') or []:
    for m in rule.get('matchers') or []:
        if m.get('field') == 'to' and m.get('value') == addr:
            print(rule.get('tag') or rule.get('id')); sys.exit(0)
")

if [[ -n "$EXISTING" ]]; then
  log "    rule for $EMAIL_ADDR already exists ($EXISTING) — reusing"
  RULE_ID="$EXISTING"
else
  BODY=$(python3 -c "
import json,sys
print(json.dumps({
  'name': f'hermes:{sys.argv[1]}',
  'enabled': True,
  'priority': 100,
  'matchers': [{'field': 'to', 'type': 'literal', 'value': sys.argv[2]}],
  'actions':  [{'type': 'forward', 'value': [sys.argv[3]]}],
}))" "$AGENT_ID" "$EMAIL_ADDR" "$FORWARD_TO")
  RESP=$(curl -sS -X POST "$CF_API/zones/$CF_ZONE_DELO_SH/email/routing/rules" \
    -H "Authorization: Bearer $CF_EMAIL_ROUTING_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$BODY")
  RULE_ID=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print((d.get('result') or {}).get('tag') or (d.get('result') or {}).get('id') or '')")
  [[ -n "$RULE_ID" ]] || die "email rule create failed: $RESP"
  log "    rule created: $RULE_ID"
fi

echo "$RULE_ID" > "$ROLE_DIR/.scripts/.cf-rule-id"
mark_done 50-email
