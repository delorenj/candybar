#!/usr/bin/env bash
set -euo pipefail
RUNTIME_DIR="$(cd "$(dirname "$0")/../runtime" && pwd)"
cd "$RUNTIME_DIR"
git add -A
if git diff --cached --quiet; then exit 0; fi
git -c commit.gpgsign=false commit -m "checkpoint $(date -Iseconds)" >/dev/null
git push origin HEAD 2>&1 | tail -1 || true
