#!/usr/bin/env bash
# Lifecycle helper — resolves the CANONICAL Krebs ticket lifecycle for this role.
#
# The state machine is NOT defined here. It lives in
# krebs/spec/lifecycle.v1.yaml, which is the contract between ticket providers
# and fleet orchestrators. This script is a reader: it answers "which tp band
# does phase X map to" and "is this ticket stale" without any repo growing its
# own private copy of the phase list. A second copy is how a PM ends up
# transitioning against labels the board no longer uses.
#
# Provider LABELS are deliberately absent — those belong to the tp adapter's
# normalized-state map (.scripts/providers/<provider>.sh). This layer speaks
# only phases and the five tp bands.
#
# Usage:
#   lifecycle.sh phases                 list every phase with band + staleness
#   lifecycle.sh band <phase>           print the tp band for a phase
#   lifecycle.sh staleness <phase>      print staleness minutes ("-" if none)
#   lifecycle.sh is-terminal <phase>    exit 0 when the phase is terminal
#   lifecycle.sh stale <phase> <iso8601>  exit 0 when that timestamp is stale
#   lifecycle.sh spec                   print the resolved spec path
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolution order: explicit override, the 33GOD checkout, then a sibling walk
# for hosts that keep the platform elsewhere.
resolve_spec() {
  if [ -n "${KREBS_LIFECYCLE_SPEC:-}" ] && [ -r "$KREBS_LIFECYCLE_SPEC" ]; then
    printf '%s\n' "$KREBS_LIFECYCLE_SPEC"; return 0
  fi
  local candidates=(
    "$HOME/code/33GOD/krebs/spec/lifecycle.v1.yaml"
    "$SCRIPT_DIR/../../../../krebs/spec/lifecycle.v1.yaml"
    "$SCRIPT_DIR/../../../../../krebs/spec/lifecycle.v1.yaml"
  )
  local c
  for c in "${candidates[@]}"; do
    [ -r "$c" ] && { printf '%s\n' "$c"; return 0; }
  done
  return 1
}

SPEC="$(resolve_spec || true)"

need_spec() {
  if [ -z "$SPEC" ]; then
    echo "lifecycle: canonical spec not found (set KREBS_LIFECYCLE_SPEC)" >&2
    echo "lifecycle: expected krebs/spec/lifecycle.v1.yaml in the 33GOD checkout" >&2
    exit 2
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "lifecycle: python3 required to read $SPEC" >&2
    exit 2
  fi
}

# One python entrypoint for every query keeps YAML parsing in exactly one place.
query() {
  need_spec
  python3 - "$SPEC" "$@" <<'PY'
import sys
try:
    import yaml
except ImportError:
    sys.exit("lifecycle: PyYAML required to read the lifecycle spec")

spec_path, op = sys.argv[1], sys.argv[2]
arg = sys.argv[3] if len(sys.argv) > 3 else None
arg2 = sys.argv[4] if len(sys.argv) > 4 else None

spec = yaml.safe_load(open(spec_path)) or {}
states = {s["phase"]: s for s in (spec.get("states") or []) if s.get("phase")}
if not states:
    sys.exit(f"lifecycle: no states in {spec_path}")

def need_phase(p):
    if p not in states:
        sys.exit(f"lifecycle: unknown phase {p!r}; known: {', '.join(states)}")
    return states[p]

if op == "phases":
    print(f"{'phase':<14} {'tp_band':<12} {'terminal':<9} stale_after")
    for name, s in states.items():
        stale = s.get("staleness_minutes")
        print(f"{name:<14} {str(s.get('tp_band')):<12} "
              f"{str(bool(s.get('terminal'))).lower():<9} "
              f"{(str(stale) + 'm') if stale else '-'}")
elif op == "band":
    print(need_phase(arg)["tp_band"])
elif op == "staleness":
    print(need_phase(arg).get("staleness_minutes") or "-")
elif op == "is-terminal":
    sys.exit(0 if need_phase(arg).get("terminal") else 1)
elif op == "stale":
    from datetime import datetime, timezone
    mins = need_phase(arg).get("staleness_minutes")
    if not mins:
        sys.exit(1)  # phase has no staleness budget -> never stale
    try:
        ts = datetime.fromisoformat(arg2.replace("Z", "+00:00"))
    except Exception:
        sys.exit(f"lifecycle: unparseable timestamp {arg2!r} (want ISO 8601)")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ts).total_seconds() / 60
    sys.exit(0 if age > mins else 1)
else:
    sys.exit(f"lifecycle: unknown operation {op!r}")
PY
}

case "${1:-phases}" in
  phases)      query phases ;;
  band)        query band "${2:?phase required}" ;;
  staleness)   query staleness "${2:?phase required}" ;;
  is-terminal) query is-terminal "${2:?phase required}" ;;
  stale)       query stale "${2:?phase required}" "${3:?iso8601 timestamp required}" ;;
  spec)        need_spec; printf '%s\n' "$SPEC" ;;
  --help|-h|help)
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
  *)
    echo "lifecycle: unknown operation ${1}" >&2
    echo "try: phases | band <phase> | staleness <phase> | is-terminal <phase> | stale <phase> <ts> | spec" >&2
    exit 2 ;;
esac
