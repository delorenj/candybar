#!/usr/bin/env bash
# Create the per-agent runtime GitHub repo, init from scaffold, submodule it.
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

already_done 20-runtime-repo && { log "[20] runtime repo already set up — skipping"; exit 0; }
[[ "${SKIP_RUNTIME_REPO:-0}" == "1" ]] && { log "[20] runtime repo — SKIPPED (SKIP_RUNTIME_REPO=1)"; mark_done 20-runtime-repo; exit 0; }

PROFILE_HOME="$HOME/.hermes/profiles/$PROFILE_NAME"
RUNTIME_LOCAL="$ROLE_DIR/runtime"
GH_OWNER="${RUNTIME_REPO%%/*}"
GH_NAME="${RUNTIME_REPO##*/}"

log "[20] runtime repo: gh:$RUNTIME_REPO"

# 1. Create the GitHub repo (private) if it doesn't exist
if gh repo view "$RUNTIME_REPO" >/dev/null 2>&1; then
  log "    GH repo exists; reusing"
else
  log "    creating GH repo (private)"
  gh repo create "$RUNTIME_REPO" --private \
    --description "Hermes runtime (HERMES_HOME) for $AGENT_ID — auto-checkpointed memory + state" \
    --disable-issues --disable-wiki >/dev/null
fi

REMOTE_URL=$(gh repo view "$RUNTIME_REPO" --json sshUrl -q .sshUrl)

# 2. Check if remote already has commits — if so, skip the scaffold push.
#    This makes the step idempotent across failed-run retries.
if git ls-remote --heads "$REMOTE_URL" 2>/dev/null | grep -q refs/heads; then
  log "    remote already has commits — skipping scaffold push"
  REMOTE_HAS_CONTENT=1
else
  REMOTE_HAS_CONTENT=0
fi

# 2a. Stage the runtime scaffold into a tmp dir (only if remote is empty)
TMP=$(mktemp -d)
log "    populating scaffold in $TMP"
cp -a "$RUNTIME_SCAFFOLD_DIR/." "$TMP/"

# Render scaffold templates with role-specific values
python3 - "$TMP" "$AGENT_ID" "$REPO" "$ROLE" "$DISPLAY_NAME" <<'PYEOF'
import sys, pathlib, re
root, agent_id, repo, role, display = sys.argv[1:6]
root = pathlib.Path(root)
mapping = {
    "{{agent_id}}": agent_id, "{{repo}}": repo, "{{role}}": role,
    "{{display_name}}": display,
}
for p in root.rglob("*"):
    if p.is_file() and p.suffix in (".md", ".yaml", ".yml", ".sh", ".py", ".gitignore", ".gitattributes"):
        try:
            t = p.read_text()
            for k, v in mapping.items(): t = t.replace(k, v)
            p.write_text(t)
        except UnicodeDecodeError:
            pass
PYEOF

# 3. Copy current global config.yaml so the agent inherits provider/skills.
if [[ -f "$HOME/.hermes/config.yaml" ]]; then
  cp "$HOME/.hermes/config.yaml" "$TMP/config.yaml"
fi
# Copy the project's SOUL.md as the canonical starting personality.
cp "$ROLE_DIR/SOUL.md" "$TMP/SOUL.md"

# 4. Git init + LFS + initial commit + push (skip if remote already has content)
if [[ "$REMOTE_HAS_CONTENT" == "0" ]]; then
  (
    cd "$TMP"
    git init -b main >/dev/null
    git lfs install --local >/dev/null 2>&1 || warn "git-lfs not installed; sessions.db will commit as raw binary"
    git lfs track "*.db" >/dev/null 2>&1 || true
    git lfs track "*.sqlite" >/dev/null 2>&1 || true
    git add -A
    git -c commit.gpgsign=false commit -m "Initial scaffold for $AGENT_ID" >/dev/null
    git remote add origin "$REMOTE_URL"
    git push -u origin main 2>&1 | tail -3
  )
fi

# 5. Submodule-add into the role dir
PROJECT_PATH="$(project_repo_path)" || die "no project git root"
# Compute relative path from the ROLE dir (which exists), then append /runtime
REL_ROLE_PATH="$(realpath --relative-to="$PROJECT_PATH" "$ROLE_DIR")"
REL_SUBMODULE_PATH="${REL_ROLE_PATH}/runtime"
log "    adding submodule at $REL_SUBMODULE_PATH"

# Idempotent: if the submodule is already registered, just update it.
# This handles re-runs where the .done marker was cleared or copier
# --overwrite regenerated the scripts.
SUBMODULE_ALREADY_REGISTERED=0
if git -C "$PROJECT_PATH" submodule status "$REL_SUBMODULE_PATH" >/dev/null 2>&1; then
  SUBMODULE_ALREADY_REGISTERED=1
fi

if [[ "$SUBMODULE_ALREADY_REGISTERED" == "1" ]]; then
  log "    submodule already registered — updating"
  (
    cd "$PROJECT_PATH"
    # If the local dir is missing (e.g. previous rm -rf), re-init it
    if [[ ! -d "$REL_SUBMODULE_PATH/.git" ]]; then
      git submodule update --init "$REL_SUBMODULE_PATH" 2>&1 | tail -3
    fi
  )
else
  # Clean any leftover untracked directory before adding
  rm -rf "$RUNTIME_LOCAL"
  (
    cd "$PROJECT_PATH"
    git submodule add "$REMOTE_URL" "$REL_SUBMODULE_PATH" 2>&1 | tail -3
  )
fi

# 6. Symlink the runtime back into ~/.hermes/profiles/<name>/ so hermes finds it.
# Actually we WANT HERMES_HOME = the runtime dir, not the cloned profile.
# Move the profile contents into runtime, then symlink the profile dir to runtime.
PROFILE_HOME="$HOME/.hermes/profiles/$PROFILE_NAME"
if [[ -d "$PROFILE_HOME" && ! -L "$PROFILE_HOME" ]]; then
  log "    migrating profile state into the runtime submodule"
  # Preserve any keys/state hermes profile create left
  for f in auth.json auth.lock .env config.yaml; do
    [[ -f "$PROFILE_HOME/$f" && ! -e "$RUNTIME_LOCAL/$f" ]] && cp "$PROFILE_HOME/$f" "$RUNTIME_LOCAL/$f"
  done
  rm -rf "$PROFILE_HOME"
  ln -sfn "$RUNTIME_LOCAL" "$PROFILE_HOME"
  log "    $PROFILE_HOME -> $RUNTIME_LOCAL"
fi

rm -rf "$TMP"
mark_done 20-runtime-repo
