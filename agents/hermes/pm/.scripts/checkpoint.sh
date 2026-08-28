#!/usr/bin/env bash
# Legacy checkpoint compatibility for installations whose runtime is still a
# nested Git repository. Pure-local runtimes intentionally skip this script.
# Idempotent — exits 0 with no commit if there are no changes.
#
# Secret-scan gate (PJAN): before committing, the staged diff is scanned for
# high-signal credentials. On a hit the checkpoint ABORTS — it unstages, does
# NOT commit or push, logs loudly, and exits non-zero — so the known
# auto-commit secret-leak recurrence cannot re-leak through the heartbeat.
set -euo pipefail

RUNTIME_DIR="$(cd "$(dirname "$0")/../runtime" && pwd)"
cd "$RUNTIME_DIR"

# Skip when the pure-local runtime has no Git metadata.
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

# A checkpoint on a DETACHED HEAD belongs to no branch: `git push origin HEAD`
# cannot resolve a destination, so every commit stays local forever. One runtime
# reached 232 orphaned commits this way before anyone noticed.
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
if [ "$BRANCH" = "HEAD" ]; then
  printf '[checkpoint] ABORT: %s is on a DETACHED HEAD — refusing to create commits that can never be pushed.\n' "$RUNTIME_DIR" >&2
  printf '[checkpoint] Fix: git -C %s switch -c main && git -C %s push -u origin main\n' "$RUNTIME_DIR" "$RUNTIME_DIR" >&2
  exit 4
fi

# Report an existing backlog BEFORE adding to it, so a broken push is visible on
# the very next tick instead of compounding silently for months.
backlog="$(git rev-list --count "origin/$BRANCH..HEAD" 2>/dev/null || echo 0)"
if [ "$backlog" -ge 10 ]; then
  printf '[checkpoint] WARNING: %s already has %s UNPUSHED commit(s) on %s.\n' "$RUNTIME_DIR" "$backlog" "$BRANCH" >&2
fi

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

# The push MUST be observable. This line used to be
#   git push origin HEAD 2>&1 | tail -1 || true
# where `|| true` swallowed every failure — diverged branch, auth, network —
# and the pipe masked git's exit status on top of it. Run hourly, that turns a
# broken push into an invisible, unbounded pile of local-only commits.
if ! git push origin "$BRANCH" >/dev/null 2>&1; then
  ahead="$(git rev-list --count "origin/$BRANCH..HEAD" 2>/dev/null || echo '?')"
  printf '[checkpoint] PUSH FAILED for %s (%s commit(s) exist ONLY on this disk).\n' "$RUNTIME_DIR" "$ahead" >&2
  printf '[checkpoint] Diagnose: git -C %s push origin %s\n' "$RUNTIME_DIR" "$BRANCH" >&2
  exit 5
fi
