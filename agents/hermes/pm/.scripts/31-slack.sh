#!/usr/bin/env bash
# Opt-in, profile-local Slack Socket Mode provisioning.
#
# Slack credentials are invocation-only inputs.  Capture them before _lib.sh
# sources fleet.env so an accidentally shared fleet token can never provision a
# profile.  The non-secret allowed-user policy may still come from fleet.env.
INVOCATION_SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN-}"
INVOCATION_SLACK_APP_TOKEN="${SLACK_APP_TOKEN-}"
INVOCATION_SLACK_ALLOWED_USERS="${SLACK_ALLOWED_USERS-}"
INVOCATION_ENABLE_SLACK="${ENABLE_SLACK-${WIRE_SLACK-}}"

# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

SLACK_BOT_TOKEN="$INVOCATION_SLACK_BOT_TOKEN"
SLACK_APP_TOKEN="$INVOCATION_SLACK_APP_TOKEN"
if [[ -n "$INVOCATION_SLACK_ALLOWED_USERS" ]]; then
  SLACK_ALLOWED_USERS="$INVOCATION_SLACK_ALLOWED_USERS"
else
  SLACK_ALLOWED_USERS="${SLACK_ALLOWED_USERS-}"
fi
ENABLE_SLACK="$INVOCATION_ENABLE_SLACK"
unset INVOCATION_SLACK_BOT_TOKEN INVOCATION_SLACK_APP_TOKEN

slack_yaml_update() {
  python3 - "$ROLE_YAML" "$@" <<'PYEOF'
import json
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
updates = dict(zip(sys.argv[2::2], sys.argv[3::2]))
text = path.read_text(encoding="utf-8")
match = re.search(r"(?ms)^slack:\s*\n(?P<body>(?:^[ \t]+.*\n?)*)", text)
if not match:
    raise SystemExit(f"slack metadata block missing from {path}")
body = match.group("body")
for key, value in updates.items():
    replacement = f"  {key}: {json.dumps(value)}"
    body, count = re.subn(
        rf"(?m)^\s+{re.escape(key)}:\s*.*$", lambda _: replacement, body, count=1
    )
    if count != 1:
        raise SystemExit(f"Slack metadata key {key!r} missing from {path}")
path.write_text(text[: match.start("body")] + body + text[match.end("body") :], encoding="utf-8")
PYEOF
}

truthy() {
  case "${1,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

if [[ "${SKIP_SLACK:-0}" == "1" ]]; then
  slack_yaml_update provisioning_status disabled
  log "[31] slack — DISABLED (SKIP_SLACK=1)"
  mark_done 31-slack
  exit 0
fi

already_done 31-slack && { log "[31] slack already wired — skipping"; exit 0; }

have_bot=0
have_app=0
[[ -n "$SLACK_BOT_TOKEN" ]] && have_bot=1
[[ -n "$SLACK_APP_TOKEN" ]] && have_app=1

if ! truthy "${ENABLE_SLACK:-0}" && (( ! have_bot && ! have_app )); then
  slack_yaml_update provisioning_status deferred
  log "[31] slack — deferred (opt in with ENABLE_SLACK=1 or supply both Slack tokens)"
  exit 0
fi

if (( have_bot != have_app )) && ! truthy "${ENABLE_SLACK:-0}"; then
  die "Slack provisioning requires a dedicated SLACK_BOT_TOKEN and SLACK_APP_TOKEN pair"
fi

if truthy "${ENABLE_SLACK:-0}" && [[ -t 0 ]]; then
  if [[ -z "$SLACK_BOT_TOKEN" ]]; then
    read -r -s -p "Slack Bot User OAuth Token (xoxb-...): " SLACK_BOT_TOKEN
    echo >&2
  fi
  if [[ -z "$SLACK_APP_TOKEN" ]]; then
    read -r -s -p "Slack App-Level Socket Mode Token (xapp-...): " SLACK_APP_TOKEN
    echo >&2
  fi
fi

[[ -n "$SLACK_BOT_TOKEN" && -n "$SLACK_APP_TOKEN" ]] \
  || die "Slack provisioning requires both SLACK_BOT_TOKEN and SLACK_APP_TOKEN"
[[ "$SLACK_BOT_TOKEN" == xoxb-* ]] || die "SLACK_BOT_TOKEN must be a Bot User OAuth token (xoxb-...)"
[[ "$SLACK_APP_TOKEN" == xapp-* ]] || die "SLACK_APP_TOKEN must be an App-Level Socket Mode token (xapp-...)"

SLACK_ALLOWED_USERS="${SLACK_ALLOWED_USERS//[[:space:]]/}"
if [[ -n "$SLACK_ALLOWED_USERS" && "$SLACK_ALLOWED_USERS" != "*" \
      && ! "$SLACK_ALLOWED_USERS" =~ ^[UW][A-Z0-9]+(,[UW][A-Z0-9]+)*$ ]]; then
  die "SLACK_ALLOWED_USERS must be '*' or comma-separated Slack member IDs"
fi

RUNTIME="$ROLE_DIR/runtime"
ENVF="$RUNTIME/.env"
mkdir -p "$RUNTIME"
[[ ! -L "$ENVF" ]] || die "refusing to write Slack credentials through symlink: $ENVF"

log "[31] verifying Slack bot identity via auth.test"
auth_response=$(curl -fsS \
  -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -X POST "https://slack.com/api/auth.test") \
  || die "Slack auth.test request failed"

identity=$(printf '%s' "$auth_response" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit("Slack auth.test returned an invalid response")
if not data.get("ok"):
    error = str(data.get("error") or "unknown_error")
    safe = "".join(c for c in error if c.isalnum() or c in "_-.")[:80]
    raise SystemExit("Slack auth.test rejected the bot token ({})".format(safe or "unknown_error"))
values = [data.get(k, "") for k in ("team_id", "team", "user_id", "bot_id", "user")]
if not values[0] or not values[2]:
    raise SystemExit("Slack auth.test response omitted required identity fields")
print("\t".join(str(v).replace("\t", " ").replace("\n", " ") for v in values))
') || die "Slack bot identity verification failed"
IFS=$'\t' read -r slack_team_id slack_team_name slack_bot_user_id slack_bot_id slack_bot_username <<< "$identity"

# Reject credential reuse, token rotation onto an identity owned by another
# agent, and credentials parked in shared env files. The scan, durable identity
# claim, and profile credential write share one fleet-wide flock.
export SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_ALLOWED_USERS
fleet_lock_acquire
trap 'fleet_lock_release' EXIT
python3 - "$REGISTRY_FILE" "$FLEET_ENV" "$ENVF" "$AGENT_ID" \
  "$slack_team_id" "$slack_bot_user_id" "$slack_bot_id" \
  "$ROLE_DIR" "$PROFILE_NAME" "$slack_team_name" "$slack_bot_username" <<'PYEOF'
import errno
import os
import pathlib
import re
import sys
import tempfile
try:
    import yaml  # type: ignore
except ImportError:
    raise SystemExit("PyYAML is required for Slack fleet claims")

(
    registry_path,
    fleet_path,
    target_path,
    agent_id,
    team_id,
    user_id,
    bot_id,
    role_dir,
    profile_name,
    team_name,
    bot_username,
) = sys.argv[1:]
bot_token = os.environ["SLACK_BOT_TOKEN"]
app_token = os.environ["SLACK_APP_TOKEN"]
target = pathlib.Path(target_path).resolve(strict=False)

def env_values(path):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    values = {}
    for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
        match = re.search(rf"(?m)^\s*(?:export\s+)?{key}\s*=\s*(.*)$", text)
        if match:
            value = match.group(1).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    return values

owners = [("shared fleet environment", pathlib.Path(fleet_path))]
registry = pathlib.Path(registry_path)
if registry.is_symlink():
    raise SystemExit(f"refusing to update registry symlink: {registry}")
data = {"schema_version": 1, "agents": {}}
if registry.is_file():
    try:
        data = yaml.safe_load(registry.read_text(encoding="utf-8")) or data
    except Exception as exc:
        raise SystemExit(f"cannot safely inspect Slack ownership registry: {type(exc).__name__}")
if not isinstance(data, dict) or not isinstance(data.get("agents", {}), dict):
    raise SystemExit("cannot safely inspect Slack ownership registry: invalid agents mapping")
agents = data.setdefault("agents", {})
for other_id, entry in agents.items():
    if other_id == agent_id or not isinstance(entry, dict):
        continue
    slack = entry.get("slack") or {}
    if isinstance(slack, dict):
        same_user = user_id and slack.get("bot_user_id") == user_id
        same_bot = bot_id and slack.get("bot_id") == bot_id
        same_team_user = team_id and same_user and slack.get("team_id") == team_id
        if same_bot or same_team_user:
            raise SystemExit(f"Slack bot identity is already assigned to agent {other_id}")
    other_role_dir = entry.get("role_dir")
    if other_role_dir:
        owners.append((f"agent {other_id}", pathlib.Path(str(other_role_dir)) / "runtime" / ".env"))

home_value = os.environ.get("HOME", "")
if home_value:
    home = pathlib.Path(home_value)
    owners.append(("shared Hermes root", home / ".hermes" / ".env"))
    profiles = home / ".hermes" / "profiles"
    if profiles.is_dir():
        for profile in profiles.iterdir():
            owners.append((f"profile {profile.name}", profile / ".env"))

seen = set()
for owner, path in owners:
    resolved = path.resolve(strict=False)
    if resolved == target or resolved in seen:
        continue
    seen.add(resolved)
    values = env_values(path)
    if values.get("SLACK_BOT_TOKEN") == bot_token:
        raise SystemExit(f"Slack bot token is already assigned to {owner}")
    if values.get("SLACK_APP_TOKEN") == app_token:
        raise SystemExit(f"Slack app token is already assigned to {owner}")

claim = agents.setdefault(agent_id, {})
if not isinstance(claim, dict):
    raise SystemExit(f"registry entry for {agent_id} is not a mapping")
claim["role_dir"] = role_dir
claim["profile_name"] = profile_name
claim["slack"] = {
    "provisioning_status": "verified",
    "team_id": team_id,
    "team_name": team_name,
    "bot_user_id": user_id,
    "bot_id": bot_id,
    "bot_username": bot_username,
}
registry.parent.mkdir(parents=True, exist_ok=True)
rendered = yaml.safe_dump(data, sort_keys=False)

def fsync_parent(target):
    unsupported = {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL), errno.ENOSYS}
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_fd = os.open(target.parent, flags)
    except OSError as exc:
        if exc.errno in unsupported:
            return
        raise
    try:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno not in unsupported:
                raise
    finally:
        os.close(directory_fd)

fd, temporary = tempfile.mkstemp(prefix=f".{registry.name}.slack-", dir=registry.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, registry)
    os.chmod(registry, 0o600)
    fsync_parent(registry)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PYEOF

# Atomically replace only the Slack fields in the profile-local runtime file.
# Secrets stay out of argv and are never written to root .env or fleet.env.
python3 - "$ENVF" <<'PYEOF'
import errno
import json
import os
import pathlib
import re
import tempfile

path = pathlib.Path(__import__("sys").argv[1])
if path.is_symlink():
    raise SystemExit(f"refusing to write Slack credentials through symlink: {path}")
path.parent.mkdir(parents=True, exist_ok=True)
text = path.read_text(encoding="utf-8") if path.exists() else ""
for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_ALLOWED_USERS"):
    text = re.sub(rf"(?m)^\s*(?:export\s+)?#?\s*{key}\s*=.*(?:\n|$)", "", text)
values = {key: os.environ.get(key, "") for key in (
    "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_ALLOWED_USERS"
)}
text = text.rstrip("\n")
if text:
    text += "\n"
text += "".join(f"{key}={json.dumps(value)}\n" for key, value in values.items())

def fsync_parent(target):
    unsupported = {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL), errno.ENOSYS}
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_fd = os.open(target.parent, flags)
    except OSError as exc:
        if exc.errno in unsupported:
            return
        raise
    try:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno not in unsupported:
                raise
    finally:
        os.close(directory_fd)

fd, temporary = tempfile.mkstemp(prefix=".env.slack-", dir=path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    fsync_parent(path)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PYEOF
unset SLACK_BOT_TOKEN SLACK_APP_TOKEN

slack_yaml_update \
  provisioning_status verified \
  team_id "$slack_team_id" \
  team_name "$slack_team_name" \
  bot_user_id "$slack_bot_user_id" \
  bot_id "$slack_bot_id" \
  bot_username "$slack_bot_username"

fleet_lock_release
trap - EXIT

if [[ -z "$SLACK_ALLOWED_USERS" ]]; then
  warn "    Slack is wired but denies all inbound users until SLACK_ALLOWED_USERS is set"
fi
log "    verified Slack bot $slack_bot_username in $slack_team_name (profile-local credentials)"
mark_done 31-slack
