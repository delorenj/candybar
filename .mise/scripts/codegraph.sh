#!/usr/bin/env bash
# Mise enter hook: ensure the CodeGraph CLI is available and initialize the
# project index. If `codegraph` is not installed, this script installs it
# non-interactively into the project-local .mise/bin directory and retries.
#
# This is intended to run from a mise enter hook so onboarding a new host is
# fully automatic.

set -euo pipefail

REPO_ROOT="${MISE_PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
PROJECT_BIN_DIR="$REPO_ROOT/.mise/bin"
mkdir -p "$PROJECT_BIN_DIR"

# Install the CodeGraph CLI into the project-local bin directory.
install_codegraph() {
  echo "[mise] codegraph not found. Installing non-interactively..."
  export CODEGRAPH_BIN_DIR="$PROJECT_BIN_DIR"
  curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh

  if [ -x "$PROJECT_BIN_DIR/codegraph" ]; then
    export PATH="$PROJECT_BIN_DIR:$PATH"
  else
    echo "[mise] codegraph install did not place a binary at $PROJECT_BIN_DIR/codegraph" >&2
    return 1
  fi
}

# Ensure a codegraph binary is available on PATH.
ensure_codegraph() {
  if command -v codegraph >/dev/null 2>&1; then
    return 0
  fi

  # Check the project-local bin dir first (previous install from this hook).
  if [ -x "$PROJECT_BIN_DIR/codegraph" ]; then
    export PATH="$PROJECT_BIN_DIR:$PATH"
    return 0
  fi

  # Check typical user-level install locations before fetching anything.
  for d in "$HOME/.local/bin" "$HOME/.codegraph/current/bin"; do
    if [ -x "$d/codegraph" ]; then
      export PATH="$d:$PATH"
      return 0
    fi
  done

  install_codegraph
}

# Attempt to initialize the project graph. If the command is missing, install
# it and retry once.
init_project() {
  local err_file
  err_file="$(mktemp)"
  trap 'rm -f "$err_file"' RETURN

  if codegraph init -i "$REPO_ROOT" 2>"$err_file"; then
    return 0
  fi

  # If the failure looks like a missing binary, install and retry.
  if grep -qiE 'command not found|not installed|No such file|executable file not found' "$err_file" 2>/dev/null; then
    ensure_codegraph
    codegraph init -i "$REPO_ROOT"
    return 0
  fi

  cat "$err_file" >&2
  return 1
}

init_project
