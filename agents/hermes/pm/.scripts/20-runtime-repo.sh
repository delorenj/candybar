#!/usr/bin/env bash
# Provision the per-agent, pure-local Hermes runtime without creating a Git
# repository or a project submodule. Runtime durability belongs to Hindsight;
# this directory may contain secrets and mutable agent state (PJAN-41).
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

already_done 20-runtime-repo \
  && log "[20] local runtime scaffold already set up — re-auditing singleton profile wiring"
if [[ "${SKIP_RUNTIME_REPO:-0}" == "1" ]]; then
  clear_done 20-runtime-repo
  log "[20] local runtime — DEFERRED (SKIP_RUNTIME_REPO=1; completion marker cleared)"
  exit 0
fi

PROFILE_HOME="$HOME/.hermes/profiles/$PROFILE_NAME"
RUNTIME_LOCAL="$ROLE_DIR/runtime"
PROJECT_PATH="$(project_repo_path)" || die "no project git root"
REL_ROLE_PATH="$(realpath --relative-to="$PROJECT_PATH" "$ROLE_DIR")"
REL_RUNTIME_PATH="${REL_ROLE_PATH}/runtime"

log "[20] local runtime: $RUNTIME_LOCAL"

# Fail closed if an older installation still models runtime as a project
# submodule. `pjangler migrate` performs the non-destructive index transition;
# this provisioner never removes or rewrites an existing nested repository.
if git -C "$PROJECT_PATH" ls-files --stage -- "$REL_RUNTIME_PATH" | grep -q '^160000 '; then
  die "$REL_RUNTIME_PATH is still a tracked gitlink; run 'pjangler migrate' before provisioning"
fi
if [[ -f "$PROJECT_PATH/.gitmodules" ]] &&
   git -C "$PROJECT_PATH" config -f .gitmodules --get-regexp '^submodule\..*\.path$' 2>/dev/null |
     awk -v expected="$REL_RUNTIME_PATH" '$2 == expected { found=1 } END { exit !found }'; then
  die "$REL_RUNTIME_PATH still has a stale .gitmodules mapping; run 'pjangler migrate' before provisioning"
fi

mkdir -p "$RUNTIME_LOCAL"
if [[ -e "$RUNTIME_LOCAL/.git" ]]; then
  warn "    existing nested runtime repository preserved; no fetch, commit, or push will be attempted"
fi

# Render the scaffold in a temporary directory, then copy only missing paths.
# Existing memory, configuration, credentials, and sessions always win.
TMP="$(mktemp -d)"
cleanup() { rm -rf -- "$TMP"; }
trap cleanup EXIT
cp -a "$RUNTIME_SCAFFOLD_DIR/." "$TMP/"
python3 - "$TMP" "$AGENT_ID" "$REPO" "$ROLE" "$DISPLAY_NAME" <<'PYEOF'
import pathlib
import sys

root, agent_id, repo, role, display = sys.argv[1:6]
root = pathlib.Path(root)
mapping = {
    "{{agent_id}}": agent_id,
    "{{repo}}": repo,
    "{{role}}": role,
    "{{display_name}}": display,
}
for path in root.rglob("*"):
    if path.is_file() and path.suffix in (".md", ".yaml", ".yml", ".sh", ".py", ".gitignore", ".gitattributes"):
        try:
            text = path.read_text()
            for source, target in mapping.items():
                text = text.replace(source, target)
            path.write_text(text)
        except UnicodeDecodeError:
            pass
PYEOF
# Never let a literal secret reach the runtime. The scaffold is rendered from
# templates, so a leaked credential shows up here before anything is copied.
python3 "$(dirname "$0")/secret-scan.py" "$TMP"
cp -an "$TMP/." "$RUNTIME_LOCAL/"

# Seed mutable identity/config only when no local value exists.
if [[ "$ROLE" == "reporter" ]]; then
  # Generate a delta-only runtime config for least-privilege reporters. Never copy the shared PM
  # config: it may carry dashboard credentials, write-capable MCPs, or broad tools.
  MODEL_PROVIDER="$(yaml_get model.provider)"
  MODEL_NAME="$(yaml_get model.name)"
  CANONICAL_SKILLS_DIR="${CANONICAL_SKILLS_DIR:-$(config_get fleet.canonical_skills_dir "$HOME/.agents/skills")}"
  if [[ ! -e "$RUNTIME_LOCAL/config.yaml" ]]; then
    python3 - "$RUNTIME_LOCAL/config.yaml" "$PROJECT_PATH" "${HERMES_TIMEZONE:-America/New_York}" \
      "$MODEL_PROVIDER" "$MODEL_NAME" "$CANONICAL_SKILLS_DIR" "$ROLE" <<'PYEOF'
import json, pathlib, re, sys
path, cwd, timezone, provider, model, skills, role = sys.argv[1:8]
if not re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z_]+)*", timezone):
    raise SystemExit("unsafe timezone")
config = {
    "timezone": timezone,
    "terminal": {"cwd": cwd},
    "skills": {"external_dirs": [skills]},
}
if provider or model:
    config["model"] = {}
    if provider:
        config["model"]["provider"] = provider
    if model:
        config["model"]["default"] = model
config["platform_toolsets"] = {
    "cli": ["web", "delegation", "no_mcp"],
    "cron": ["web", "delegation", "no_mcp"],
}
config["agent"] = {
    "disabled_toolsets": [
        "browser", "terminal", "file", "code_execution", "cronjob",
        "kanban", "homeassistant", "computer_use", "project", "skills",
    ]
}
config["delegation"] = {"max_spawn_depth": 1, "inherit_mcp_toolsets": False}
pathlib.Path(path).write_text(json.dumps(config, indent=2) + "\n")
PYEOF
  fi
  if [[ ! -e "$RUNTIME_LOCAL/profile.yaml" ]]; then
    cat > "$RUNTIME_LOCAL/profile.yaml" <<'YAML'
config:
  inherit_from: default
  save_mode: delta
YAML
  fi
else
  CANONICAL_PM_CONFIG="$(config_get fleet.canonical_pm_config "$HOME/.hermes/config.yaml")"
  if [[ -f "$CANONICAL_PM_CONFIG" && ! -e "$RUNTIME_LOCAL/config.yaml" ]]; then
    cp "$CANONICAL_PM_CONFIG" "$RUNTIME_LOCAL/config.yaml"
  fi
fi
if [[ ! -e "$RUNTIME_LOCAL/SOUL.md" ]]; then
  cp "$ROLE_DIR/SOUL.md" "$RUNTIME_LOCAL/SOUL.md"
fi

# Named-profile topology belongs exclusively to PJangler. Preview first, then
# apply the one idempotent rule. This script never removes or replaces a real
# profile directory and never creates the obsolete profile -> runtime symlink.
if [[ "$PJANGLER_BIN" != */* ]]; then
  PJANGLER_BIN="$(command -v "$PJANGLER_BIN" 2>/dev/null || true)"
fi
[[ -n "$PJANGLER_BIN" && -x "$PJANGLER_BIN" ]] \
  || die "PJángler CLI not found; install 'pj' or set PJANGLER_BIN"
"$PJANGLER_BIN" migrate hermes.runtime-singleton "$PROJECT_PATH" --dry-run --json >/dev/null \
  || die "singleton-runtime audit failed; profile was left untouched"
"$PJANGLER_BIN" migrate hermes.runtime-singleton "$PROJECT_PATH" --json >/dev/null \
  || die "singleton-runtime migration failed; inspect with: pj migrate hermes.runtime-singleton '$PROJECT_PATH' --dry-run"
[[ -d "$PROFILE_HOME" && ! -L "$PROFILE_HOME" ]] \
  || die "PJángler did not establish a real named profile at $PROFILE_HOME"
log "    singleton profile verified by pj migrate hermes.runtime-singleton: $PROFILE_HOME"

# Never persist a project-specific terminal.cwd through the named profile.
# config.yaml is fleet-shared in the singleton topology.  The manual and
# service launchers pass TERMINAL_CWD process-locally for this role instead.

profile_config_set() {
  local key="$1"
  [[ -x "$HERMES_BIN" ]] \
    || die "Hermes CLI is not executable; cannot configure named profile: $HERMES_BIN"
  if ! env HERMES_HOME="$PROFILE_HOME" "$HERMES_BIN" config set "$@" >/dev/null; then
    die "required Hermes config write failed for named profile $PROFILE_NAME: $key"
  fi
}

if [[ "$ROLE" == "pm" ]]; then
  VOXXY_PLUGIN_DIR="${VOXXY_PLUGIN_DIR:-$(config_get fleet.voxxy_plugin_dir "$HOME/code/voxxy/plugins/tts/voxxy")}"
  if [[ -d "$VOXXY_PLUGIN_DIR" ]]; then
    mkdir -p "$RUNTIME_LOCAL/plugins/tts"
    ln -sfn "$VOXXY_PLUGIN_DIR" "$RUNTIME_LOCAL/plugins/tts/voxxy"
    log "    linked Voxxy plugin into runtime"
  else
    warn "    Voxxy plugin dir missing: $VOXXY_PLUGIN_DIR"
  fi

  profile_config_set plugins.enabled.0 tts/voxxy
  profile_config_set tts.provider voxxy
  profile_config_set tts.voice rick
  log "    set PM named-profile TTS provider -> voxxy"
fi

mark_done 20-runtime-repo
