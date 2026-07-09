#!/usr/bin/env bash
# Auto-checkpoint commit for the agent's runtime submodule.
# Idempotent — exits 0 with no commit if there are no changes.
#
# Secret-scan gate (PJAN): before committing, the staged diff is scanned for
# high-signal credentials. On a hit the checkpoint ABORTS — it unstages, does
# NOT commit or push, logs loudly, and exits non-zero — so the known
# auto-commit secret-leak recurrence cannot re-leak through the heartbeat.
set -euo pipefail

RUNTIME_DIR="$(cd "$(dirname "$0")/../runtime" && pwd)"
cd "$RUNTIME_DIR"

# Skip if not a git repo (e.g. submodule not initialized)
[[ -d .git || -f .git ]] || exit 0

# Returns 0 when the staged diff looks clean, 1 when a likely secret is present.
secret_scan_ok() {
  local added
  added="$(git diff --cached -U0 --no-color 2>/dev/null | grep -E '^\+' | grep -vE '^\+\+\+ ' || true)"
  [[ -n "$added" ]] || return 0

  # Dependency-free, high-signal patterns (distinctive prefixes → ~zero false
  # positives) over ADDED lines only.
  if printf '%s\n' "$added" | grep -Eq \
    'AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|gh[posru]_[A-Za-z0-9]{20,}|xox[baprs]-[0-9A-Za-z-]{10,}|sk-[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{35}'; then
    return 1
  fi

  # Deeper scan when gitleaks is installed (exit 1 == leaks found; other
  # non-zero == tool/usage error, which we ignore so checkpoints aren't blocked).
  if command -v gitleaks >/dev/null 2>&1; then
    local rc=0
    git diff --cached --no-color 2>/dev/null | gitleaks stdin --no-banner --redact >/dev/null 2>&1 || rc=$?
    [[ "$rc" -eq 1 ]] && return 1
  fi
  return 0
}

git add -A
if git diff --cached --quiet; then
  exit 0
fi

if ! secret_scan_ok; then
  printf '[checkpoint] ABORT: potential secret in staged runtime changes — not committing/pushing.\n' >&2
  printf '[checkpoint] Inspect: git -C %s diff --cached   (remove the secret or add a .gitignore rule)\n' "$RUNTIME_DIR" >&2
  git reset -q || true
  exit 3
fi

git -c commit.gpgsign=false commit -m "checkpoint $(date -Iseconds)" >/dev/null
git push origin HEAD 2>&1 | tail -1 || true
