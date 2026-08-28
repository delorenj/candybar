#!/usr/bin/env bash
# Bind this agent to the repo's ONE ticket board.
#
# Source of truth is the repo-root .project.json `ticket_provider` block
# (written by the CommonProject base template). The model is:
#   ONE board per repo — the PM owns it, and its heartbeat reconciliation pass
# watches it. So we never mint a per-agent, role-suffixed board ("Foo PM" /
# "Foo Sentinel"). Instead:
#   1. If .project.json already names a board  -> BIND to it (no creation).
#   2. Otherwise (hermes run on a repo with no CommonProject board yet)
#      -> create ONE repo-named board and write it back into .project.json so
#         it becomes the SOT for every agent in this repo.
# Either way we register this agent under .project.json `agents` and mirror the
# binding into role.yaml for back-compat (80-registry.sh / 99-summary.sh).

# The caller's negative board grant is authoritative. Check it before sourcing
# _lib.sh so a skipped step cannot load fleet credentials, create logs/markers,
# inspect bindings, or reach a provider adapter.
if [[ "${SKIP_PLANE:-0}" == "1" ]]; then
  printf '%s\n' '[42] ticket provider — SKIPPED (SKIP_PLANE=1)' >&2
  exit 0
fi

# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

# Locate the repo-root .project.json (the SOT).
REPO_ROOT="$(project_repo_path 2>/dev/null || true)"
[ -n "$REPO_ROOT" ] || REPO_ROOT="$(cd "$ROLE_DIR/../../.." 2>/dev/null && pwd)"
PROJECT_JSON="$REPO_ROOT/.project.json"
ROLE_DIR_REL="${ROLE_DIR#"$REPO_ROOT"/}"

# Serialize the complete read / provider check-or-create / atomic write
# transaction per project.  The lock lives outside the checkout so a normal
# provision cannot leave repository dirt behind.
command -v flock >/dev/null 2>&1 \
  || die "flock is required for safe ticket-provider binding"
PROJECT_LOCK_KEY="$(printf '%s' "$PROJECT_JSON" | sha256sum)"
PROJECT_LOCK_KEY="${PROJECT_LOCK_KEY%% *}"
PROJECT_LOCK_DIR="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/pjangler-ticket-provider"
mkdir -p "$PROJECT_LOCK_DIR"
PROJECT_LOCK_FILE="$PROJECT_LOCK_DIR/$PROJECT_LOCK_KEY.lock"
[[ ! -L "$PROJECT_LOCK_FILE" ]] \
  || die "refusing ticket-provider lock symlink: $PROJECT_LOCK_FILE"
exec {PROJECT_LOCK_FD}>"$PROJECT_LOCK_FILE"
chmod 600 "$PROJECT_LOCK_FILE"
flock -w "${PROJECT_LOCK_TIMEOUT_SECONDS:-30}" "$PROJECT_LOCK_FD" \
  || die "timed out waiting for ticket-provider lock: $PROJECT_JSON"
project_lock_release() {
  flock -u "$PROJECT_LOCK_FD" 2>/dev/null || true
  exec {PROJECT_LOCK_FD}>&-
}
trap project_lock_release EXIT

[[ ! -L "$PROJECT_JSON" ]] || die "refusing .project.json symlink: $PROJECT_JSON"
if [[ -e "$PROJECT_JSON" ]]; then
  python3 - "$PROJECT_JSON" <<'PY' \
    || die "malformed .project.json; refusing ticket-provider mutation or board creation"
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
try:
    value = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError):
    raise SystemExit(1)
raise SystemExit(0 if isinstance(value, dict) else 1)
PY
fi

# shellcheck source=lib/ticket-provider.sh
source "$(dirname "$0")/lib/ticket-provider.sh"

already_done 42-ticket-provider \
  && log "[42] ticket-provider marker found — revalidating canonical board binding"

# pj <dotted.key> — read a string value from .project.json (empty if absent).
pj() {
  [ -f "$PROJECT_JSON" ] || { printf ''; return 0; }
  python3 - "$PROJECT_JSON" "$1" <<'PY'
import sys, json, pathlib
d = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(d, dict):
    raise SystemExit(".project.json root must be an object")
cur = d
for k in sys.argv[2].split("."):
    if isinstance(cur, dict) and k in cur:
        cur = cur[k]
    else:
        print(""); raise SystemExit(0)
print(cur if isinstance(cur, str) else "")
PY
}

# pj_write — merge board binding (optional) + this agent into .project.json.
# args: set_provider(0|1) provider board_id workspace identifier team
pj_write() {
  REPO="$REPO" REPO_ROOT="$REPO_ROOT" AGENT_ID="$AGENT_ID" ROLE="$ROLE" \
  ROLE_DIR_REL="$ROLE_DIR_REL" PROJECT_DESC="${PROJECT_DESC:-}" \
  python3 - "$PROJECT_JSON" "$@" <<'PY'
import errno
import sys, os, json, pathlib, stat, tempfile
(path, set_provider, provider, board_id, workspace, identifier, team) = sys.argv[1:8]
p = pathlib.Path(path)
if p.is_symlink():
    raise SystemExit(f"refusing .project.json symlink: {p}")
if p.exists():
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"malformed .project.json: {type(exc).__name__}")
    if not isinstance(d, dict):
        raise SystemExit(".project.json root must be an object")
else:
    d = {}
repo = os.environ.get("REPO", "")
d.setdefault("project_name", repo)
d.setdefault("project_slug", repo)
if not d.get("repo_path"):
    d["repo_path"] = os.environ.get("REPO_ROOT", "")
if set_provider == "1":
    tp = d.setdefault("ticket_provider", {})
    tp["type"] = provider
    if workspace:  tp["workspace"] = workspace
    if identifier: tp["identifier"] = identifier
    if board_id:   tp["board_id"] = board_id
    if team:       tp["team"] = team
    tp.pop("board_url", None)
    tp["state"] = "linked" if board_id else "deferred"
ag = d.setdefault("agents", {})
entry = ag.get(os.environ["AGENT_ID"], {})
if not isinstance(entry, dict):
    entry = {}
entry.update({
    "role": os.environ["ROLE"],
    "role_dir": os.environ["ROLE_DIR_REL"],
    "provisioning_state": "linked" if board_id else "deferred",
})
ag[os.environ["AGENT_ID"]] = entry
rendered = json.dumps(d, indent=2) + "\n"
p.parent.mkdir(parents=True, exist_ok=True)
mode = stat.S_IMODE(p.stat().st_mode) if p.exists() else 0o644
fd, temporary = tempfile.mkstemp(prefix=f".{p.name}.ticket-provider-", dir=p.parent)
try:
    os.fchmod(fd, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, p)
    unsupported = {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL), errno.ENOSYS}
    directory_fd = os.open(p.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno not in unsupported:
                raise
    finally:
        os.close(directory_fd)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY
  log "    .project.json updated (agent=$AGENT_ID)"
}

# Mirror the binding into role.yaml so legacy consumers keep working.
mirror_to_role_yaml() {
  # mirror_to_role_yaml <provider> <board_id> <workspace> <identifier> <team>
  local provider="$1" bid="$2" ws="$3" ident="$4" team="$5"
  yaml_set ticket_provider.name "$provider" 2>/dev/null || true
  [ -n "$bid" ]  && yaml_set ticket_provider.board_id "$bid" 2>/dev/null || true
  case "$provider" in
    plane)
      [ -n "$bid" ] && echo "$bid" > "$ROLE_DIR/.scripts/.plane-project-id"
      [ -n "$ws" ]    && yaml_set ticket_provider.workspace "$ws" 2>/dev/null || true
      [ -n "$bid" ]   && yaml_set ticket_provider.project "$bid" 2>/dev/null || true
      [ -n "$ws" ]    && yaml_set plane.workspace "$ws" 2>/dev/null || true
      [ -n "$ident" ] && yaml_set plane.identifier "$ident" 2>/dev/null || true
      ;;
    trello)
      [ -n "$bid" ] && yaml_set ticket_provider.board "$bid" 2>/dev/null || true
      ;;
    linear)
      [ -n "$team" ] && yaml_set ticket_provider.team "$team" 2>/dev/null || true
      ;;
  esac
}

# ── Provider resolution ──────────────────────────────────────────────────
# An existing repo board (in .project.json) wins — every agent binds to it.
SOT_TYPE="$(pj ticket_provider.type)"
SOT_BOARD_ID="$(pj ticket_provider.board_id)"
SOT_WS="$(pj ticket_provider.workspace)"
SOT_IDENT="$(pj ticket_provider.identifier)"
SOT_TEAM="$(pj ticket_provider.team)"

# role.yaml provider comes from copier --data (the operator's pjangler choice).
ROLE_PROVIDER="$(yaml_get ticket_provider.name)"

if [ -n "$SOT_BOARD_ID" ]; then
  # ── BIND to the repo's existing board ──────────────────────────────────
  PROVIDER="${SOT_TYPE:-${ROLE_PROVIDER:-plane}}"
  if [ -n "$ROLE_PROVIDER" ] && [ "$ROLE_PROVIDER" != "$PROVIDER" ]; then
    warn "[42] requested provider '$ROLE_PROVIDER' but repo board is '$PROVIDER' (.project.json wins); binding to existing board"
  fi
  log "[42] binding $AGENT_ID to existing repo board (provider=$PROVIDER, id=$SOT_BOARD_ID)"
  LIVE_IDENT="$SOT_IDENT"
  if [ "$PROVIDER" = plane ]; then
    OUT="$(tp resolve)" || die "existing Plane board could not be validated"
    LIVE_IDENT="$(printf '%s' "$OUT" | python3 -c 'import sys,json
try: print(str(json.load(sys.stdin).get("identifier") or ""))
except Exception: print("")')"
    [ -n "$LIVE_IDENT" ] || die "existing Plane board has no authoritative live identifier"
  fi
  mirror_to_role_yaml "$PROVIDER" "$SOT_BOARD_ID" "$SOT_WS" "$LIVE_IDENT" "$SOT_TEAM"
  pj_write 1 "$PROVIDER" "$SOT_BOARD_ID" "$SOT_WS" "$LIVE_IDENT" "$SOT_TEAM"
  mark_done 42-ticket-provider
  exit 0
fi

# ── No repo board yet: create ONE repo-named board (no role suffix) ───────
PROVIDER="${ROLE_PROVIDER:-${SOT_TYPE:-plane}}"
log "[42] no board in .project.json — bootstrapping a repo board (provider: $PROVIDER)"

# Identifier PROPOSAL for a brand-new board only. It is sent to the provider in
# the create request and is NEVER persisted as though it were confirmed: every
# write below uses LIVE_IDENT, read back from the provider's own response.
PROPOSED_RAW=$(printf '%s' "$REPO" | tr -cd '[:alnum:]' | tr '[:lower:]' '[:upper:]')
while [ ${#PROPOSED_RAW} -lt 2 ]; do PROPOSED_RAW="${PROPOSED_RAW}X"; done
PROPOSED_IDENT="${SOT_IDENT:-${PROPOSED_RAW:0:4}}"
# Board NAME = repo name, separators->space, title-cased. NOT display_name —
# display_name carries the role suffix and must never become the board name.
NAME="$(printf '%s' "$REPO" | tr '_-' '  ' | python3 -c 'import sys; print(" ".join(w[:1].upper()+w[1:] for w in sys.stdin.read().split()))')"
DESC="Ticket board for $REPO"

case "$PROVIDER" in
  linear)
    if [[ -z "${LINEAR_API_KEY:-}" ]]; then
      warn "[42] LINEAR_API_KEY not set; set role.yaml/.project.json ticket_provider.team and re-run ./.scripts/42-ticket-provider.sh"
      pj_write 1 linear "" "" "$PROPOSED_IDENT" "$SOT_TEAM"
      mark_done 42-ticket-provider; exit 0
    fi
    OUT="$(tp resolve 2>/dev/null || true)"
    BID="$(printf '%s' "$OUT" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("board_id",""))
except Exception: print("")')"
    BURL="$(printf '%s' "$OUT" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("board_url",""))
except Exception: print("")')"
    if [ -n "$BID" ]; then
      mirror_to_role_yaml linear "$BID" "" "$PROPOSED_IDENT" "$SOT_TEAM"
      pj_write 1 linear "$BID" "" "$PROPOSED_IDENT" "$SOT_TEAM"
    else
      warn "[42] linear resolve returned no board; set ticket_provider.team and re-run"
      pj_write 1 linear "" "" "$PROPOSED_IDENT" "$SOT_TEAM"
    fi
    ;;

  plane|trello)
    KEYVAR=PLANE_API_KEY; [ "$PROVIDER" = trello ] && KEYVAR=TRELLO_KEY
    if [[ -z "${!KEYVAR:-}" ]]; then
      warn "[42] $KEYVAR not set; skipping board creation. Set creds and re-run ./.scripts/42-ticket-provider.sh"
      # Deferred: no board was created, so there is no confirmed identifier.
      # Persist nothing rather than freezing the proposal into .project.json.
      pj_write 1 "$PROVIDER" "" "${SOT_WS:-$PLANE_WORKSPACE}" "" ""
      mark_done 42-ticket-provider; exit 0
    fi
    OUT="$(tp create_board "$NAME" "$PROPOSED_IDENT" "$DESC")" || die "create_board failed for $PROVIDER"
    BID="$(printf '%s' "$OUT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("board_id",""))')"
    BURL="$(printf '%s' "$OUT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("board_url",""))')"
    LIVE_IDENT="$(printf '%s' "$OUT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("identifier","") or "")')"
    WS="${SOT_WS:-$PLANE_WORKSPACE}"
    [ "$PROVIDER" = trello ] && WS=""
    [ "$PROVIDER" != plane ] || [ -n "$LIVE_IDENT" ] \
      || die "created/bound Plane board has no authoritative live identifier"
    [ -n "$LIVE_IDENT" ] || LIVE_IDENT="$PROPOSED_IDENT"
    mirror_to_role_yaml "$PROVIDER" "$BID" "$WS" "$LIVE_IDENT" ""
    pj_write 1 "$PROVIDER" "$BID" "$WS" "$LIVE_IDENT" ""
    ;;

  *) die "unknown ticket provider: $PROVIDER (expected linear|plane|trello)" ;;
esac

mark_done 42-ticket-provider
