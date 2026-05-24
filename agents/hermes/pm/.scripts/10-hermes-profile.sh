#!/usr/bin/env bash
# Create the per-agent Hermes profile (clones from default ~/.hermes).
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

# Apply role-specific config overrides.
REPO_PATH="$(project_repo_path)" || die "couldn't locate project repo root"
log "    setting terminal.cwd = $REPO_PATH"
env HERMES_HOME="$PROFILE_HOME" "$HERMES_BIN" config set terminal.cwd "$REPO_PATH"

# Canonical shared-skill source of truth + local PM fallback sync.
CANONICAL_SKILLS_DIR="${CANONICAL_SKILLS_DIR:-/home/delorenj/.agents/skills}"
CANONICAL_PM_SKILL_SRC="$CANONICAL_SKILLS_DIR/subagent-driven-development"
LOCAL_PM_SKILL_DST="$PROFILE_HOME/skills/software-development/subagent-driven-development"

if [[ -d "$CANONICAL_SKILLS_DIR" ]]; then
  log "    setting skills.external_dirs[0] = $CANONICAL_SKILLS_DIR"
  env HERMES_HOME="$PROFILE_HOME" "$HERMES_BIN" config set skills.external_dirs.0 "$CANONICAL_SKILLS_DIR"

  # Ensure key PM/local-ops skills are symlinked into runtime/profile skills root.
  # This preserves canonical ownership and keeps updates instant across agents.
  read -r -a SYMLINKED_RUNTIME_SKILLS <<< "${SYMLINKED_RUNTIME_SKILLS:-delonet-conventions delonet-dotenv hermes-pm-template-maintenance hindsight subagent-driven-development}"
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
