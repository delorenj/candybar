#!/usr/bin/env bash
# Create the initial named Hermes profile and sanitize cloned chat credentials.
# The directory remains REAL. Step 20 delegates final shared-vs-owned link
# topology to `pj migrate hermes.runtime-singleton`; no template step may
# replace the profile itself with a symlink.

if [[ "${SKIP_HOST_STATE:-0}" == "1" ]]; then
  printf '%s\n' '[10] Hermes profile — DEFERRED (SKIP_HOST_STATE=1)' >&2
  exit 0
fi

# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

already_done 10-hermes-profile && { log "[10] profile already created — skipping"; exit 0; }

log "[10] creating hermes profile: $PROFILE_NAME"
PROFILE_HOME="$HOME/.hermes/profiles/$PROFILE_NAME"

if [[ -d "$PROFILE_HOME" ]]; then
  log "    profile dir already exists; reusing"
else
  # --clone (NOT --clone-all): copies config.yaml, .env, SOUL.md only.
  # --clone-all has a recursion bug — it copies the entire ~/.hermes tree,
  #   including profiles/ itself, producing nested profiles/<name>/profiles/<name>/...
  # We do explicit skill + plugin + hooks copies below to avoid that.
  "$HERMES_BIN" profile create "$PROFILE_NAME" --clone --no-alias
fi

# Manually copy the inheritable bits that --clone doesn't get.
# These are content-only dirs; safe to mirror without recursion risk.
log "    mirroring skills, plugins, hooks from default profile"
for sub in skills plugins hooks cron skins; do
  src="$HOME/.hermes/$sub"
  dst="$PROFILE_HOME/$sub"
  if [[ -d "$src" && "$src" != "$PROFILE_HOME"* ]]; then
    mkdir -p "$dst"
    # cp -R, dereferencing symlinks; -u to preserve newer if dst exists
    cp -RLu "$src/." "$dst/" 2>/dev/null || cp -RL "$src/." "$dst/" 2>/dev/null || true
  fi
done

# Strip any inherited gateway/runtime state so this profile boots clean.
rm -f "$PROFILE_HOME/gateway.pid" "$PROFILE_HOME/gateway_state.json" \
      "$PROFILE_HOME/processes.json" "$PROFILE_HOME/state.db" 2>/dev/null || true
# Belt-and-suspenders: if a profiles/ dir somehow exists, remove it
[[ -d "$PROFILE_HOME/profiles" ]] && rm -rf "$PROFILE_HOME/profiles"

# Strip inherited messaging-platform credentials from the cloned .env.
# `profile create --clone` copies the DEFAULT profile's .env verbatim — and the
# default profile is an operator's own agent (e.g. Condaleeza on Slack). Without
# this, every sub-agent inherits the parent's bot token and would (a) hijack the
# parent's Slack/Telegram socket if it ever connects, and (b) crash-loop: the
# gateway treats an inherited-but-unusable platform as "configured", fails to
# connect it, and exits non-fatal only when ZERO platforms are configured. A
# sub-agent must establish its OWN identity via the Wire steps (30-telegram etc),
# never borrow the parent's.
PROFILE_ENV="$PROFILE_HOME/.env"
if [[ -f "$PROFILE_ENV" ]]; then
  # Some Hermes versions implement `profile create --clone` by symlinking the
  # cloned .env back to the default profile.  Never sanitize through that
  # symlink: doing so would delete the operator's fleet-wide provider secrets.
  # Break only the .env link into a private, mode-0600 copy first.
  if [[ -L "$PROFILE_ENV" ]]; then
    log "    detaching cloned .env symlink before profile-local sanitization"
    PROFILE_ENV_COPY="$PROFILE_HOME/.env.profile-local.$$"
    cp -L "$PROFILE_ENV" "$PROFILE_ENV_COPY"
    chmod 600 "$PROFILE_ENV_COPY"
    mv -fT "$PROFILE_ENV_COPY" "$PROFILE_ENV"
  fi
  log "    stripping inherited platform credentials from profile .env"
  python3 - "$PROFILE_ENV" <<'PYEOF'
import re, sys, pathlib
p = pathlib.Path(sys.argv[1])
# Identity-bearing platform creds that must be per-agent, not inherited.
keys = (
    "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_ALLOWED_USERS",
    "SLACK_SIGNING_SECRET", "SLACK_HOME_CHANNEL",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS",
    "DISCORD_BOT_TOKEN", "DISCORD_ALLOWED_USERS", "DISCORD_HOME_CHANNEL",
    "DISCORD_HOME_CHANNEL_NAME",
)
# Only touch *uncommented* assignments (leave the template's `# KEY=` examples).
pat = re.compile(r"^\s*(?:%s)=" % "|".join(keys))
lines = p.read_text().splitlines(keepends=True)
kept = [ln for ln in lines if not pat.match(ln)]
if len(kept) != len(lines):
    p.write_text("".join(kept))
PYEOF
  chmod 600 "$PROFILE_ENV"
fi

if [[ "$ROLE" == "pm" ]]; then
  VOX_URL_VALUE="${VOX_URL:-$(config_get fleet.vox_url 'https://vox.delo.sh')}"
  if [[ -n "$VOX_URL_VALUE" ]]; then
    log "    ensuring VOX_URL is present for PM voice mode"
    python3 - "$PROFILE_ENV" "$VOX_URL_VALUE" <<'PYEOF'
import pathlib, sys

path = pathlib.Path(sys.argv[1])
vox_url = sys.argv[2]
lines = path.read_text().splitlines() if path.exists() else []
for idx, line in enumerate(lines):
    if line.startswith("VOX_URL="):
        lines[idx] = f'VOX_URL="{vox_url}"'
        break
else:
    lines.append(f'VOX_URL="{vox_url}"')
path.write_text("\n".join(lines) + "\n")
PYEOF
    chmod 600 "$PROFILE_ENV"
  fi
fi

# Never persist a project-specific terminal.cwd through the named profile.
# The generated launchers pass TERMINAL_CWD process-locally instead.
#
# config.yaml is GENERATED, never hand-written and never a symlink to the fleet
# base. It is deep_merge(~/.hermes/config.yaml, <profile>/config.delta.yaml).
# The old symlink-to-base topology was actively harmful: Hermes' atomic writes
# use os.replace, which REPLACES a symlink with a regular file, so the first
# in-agent write (/model, onboarding, a config migration) silently detached the
# profile onto a frozen copy of an old base — and a symlink gave the profile no
# way to override anything in the first place.
#
# Seed an EMPTY delta: a new agent should be identical to the fleet base, and
# every line here is an override someone must justify later.
PROFILE_DELTA="$PROFILE_HOME/config.delta.yaml"
if [[ ! -f "$PROFILE_DELTA" ]]; then
  log "    seeding empty config.delta.yaml (override-only SSOT)"
  cat > "$PROFILE_DELTA" <<'DELTA_EOF'
# Override-only delta for this Hermes profile.
# Merged over ~/.hermes/config.yaml to produce config.yaml (which is GENERATED).
# Empty == identical to the fleet base. Add ONLY what must differ.
{}
DELTA_EOF
  chmod 600 "$PROFILE_DELTA"
fi

# Pin the identity-memory bank explicitly rather than relying on the fleet
# bank_id_template (agent-{profile}). {profile} resolves through Hermes'
# get_active_profile_name(), which calls Path.resolve() on HERMES_HOME and
# requires a lowercase id sitting directly under profiles/. A symlinked profile
# dir or an uppercase name silently yields the literal "custom" — which would
# merge this agent's PRIVATE memory into a bank shared with every other agent
# that also failed to resolve.
PROFILE_MEM_CFG="$PROFILE_HOME/hindsight/config.json"
mkdir -p "$(dirname "$PROFILE_MEM_CFG")"
if [[ ! -f "$PROFILE_MEM_CFG" ]]; then
  log "    pinning identity-memory bank: agent-$PROFILE_NAME"
  printf '{\n  "bank_id": "agent-%s"\n}\n' "$PROFILE_NAME" > "$PROFILE_MEM_CFG"
  chmod 600 "$PROFILE_MEM_CFG"
fi

# Render config.yaml from base + delta when the renderer is available. Without
# it the profile still boots (Hermes reads whatever config.yaml exists), but it
# is not yet under inheritance and `pj audit` will say so.
PROFILE_RENDERER="${PROFILE_RENDERER:-$HOME/code/33GOD/hermes-agent-template/scripts/hermes-profile-config.py}"
if [[ -x "$PROFILE_RENDERER" || -f "$PROFILE_RENDERER" ]]; then
  log "    rendering config.yaml from fleet base + delta"
  python3 "$PROFILE_RENDERER" render --profile "$PROFILE_NAME" >/dev/null 2>&1 \
    || warn "    render failed; run hermes-profile-config.py render --profile $PROFILE_NAME"
else
  warn "    profile renderer not found at $PROFILE_RENDERER — config.yaml not rendered"
fi

# Canonical shared-skill source of truth + local PM fallback sync.
CANONICAL_SKILLS_DIR="${CANONICAL_SKILLS_DIR:-$(config_get fleet.canonical_skills_dir '/home/delorenj/.agents/skills')}"
CANONICAL_PM_SKILL_SRC="$CANONICAL_SKILLS_DIR/subagent-driven-development"
LOCAL_PM_SKILL_DST="$PROFILE_HOME/skills/software-development/subagent-driven-development"

if [[ -d "$CANONICAL_SKILLS_DIR" ]]; then
  # skills.external_dirs is a FLEET setting and already lives in
  # ~/.hermes/config.yaml, so every rendered profile inherits it. Writing it
  # per-profile here would (a) be redundant, and (b) write into the GENERATED
  # config.yaml, where the next render discards it — the classic "I set it and
  # it reverted" trap. Verify inheritance instead of re-asserting it.
  if ! env HERMES_HOME="$PROFILE_HOME" "$HERMES_BIN" config get skills.external_dirs 2>/dev/null \
       | grep -qF "$CANONICAL_SKILLS_DIR"; then
    warn "    skills.external_dirs does not include $CANONICAL_SKILLS_DIR"
    warn "    add it to the FLEET base (~/.hermes/config.yaml), then: hermes-profile-config.py render --all"
  else
    log "    skills.external_dirs inherited from fleet base: $CANONICAL_SKILLS_DIR"
  fi

  # Ensure key PM/local-ops skills are symlinked into runtime/profile skills root.
  # This preserves canonical ownership and keeps updates instant across agents.
  read -r -a SYMLINKED_RUNTIME_SKILLS <<< "${SYMLINKED_RUNTIME_SKILLS:-$(config_get fleet.symlinked_runtime_skills 'delonet-conventions delonet-dotenv hermes-pm-template-maintenance hindsight subagent-driven-development')}"
  mkdir -p "$PROFILE_HOME/skills"

  for skill_name in "${SYMLINKED_RUNTIME_SKILLS[@]}"; do
    src="$CANONICAL_SKILLS_DIR/$skill_name"
    dst="$PROFILE_HOME/skills/$skill_name"

    if [[ ! -f "$src/SKILL.md" ]]; then
      warn "    skipping runtime skill symlink (missing SKILL.md): $src"
      continue
    fi

    if [[ -L "$dst" && "$(readlink "$dst")" == "$src" ]]; then
      log "    runtime skill symlink already set: $dst -> $src"
      continue
    fi

    [[ -e "$dst" || -L "$dst" ]] && rm -rf "$dst"
    ln -s "$src" "$dst"
    log "    symlinked runtime skill: $dst -> $src"
  done
else
  warn "    canonical skills dir missing: $CANONICAL_SKILLS_DIR"
fi

if [[ -f "$CANONICAL_PM_SKILL_SRC/SKILL.md" ]]; then
  log "    syncing canonical PM workflow skill -> $LOCAL_PM_SKILL_DST"
  mkdir -p "$LOCAL_PM_SKILL_DST"
  cp -f "$CANONICAL_PM_SKILL_SRC/SKILL.md" "$LOCAL_PM_SKILL_DST/SKILL.md"
else
  warn "    canonical PM skill missing: $CANONICAL_PM_SKILL_SRC/SKILL.md"
fi

# Install the project's SOUL.md into the profile so the agent loads it.
if [[ -f "$ROLE_DIR/SOUL.md" ]]; then
  cp "$ROLE_DIR/SOUL.md" "$PROFILE_HOME/SOUL.md"
  log "    installed SOUL.md into profile"
fi

mark_done 10-hermes-profile
