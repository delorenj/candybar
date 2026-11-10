Review these source-only branches independently; no live mutations or sender invocations.

Candybar: `/home/delorenj/code/33GOD/candybar/.worktrees/fix-policy-local-only`, branch `fix-policy-local-only`.
Agents gate: `/home/delorenj/.agents/.worktrees/fix-policy-agent-attention`, branch `fix-policy-agent-attention`.

Inspect each `git diff main...HEAD` plus changed/untracked source if still present. Look for missing behavior as well as incorrect changes. Verify canonical register and autosync cannot restore Telegram from this manifest, lifecycle remains paused, local wrapper pending/failure/recovery never sends, active local attention keys are stable while diagnostic text changes, recovery obeys existing subset suppression, audit violations/unknown remain intact. Check read-only lock-contention diagnosis and preserve exact rollback identity checks. No blanket target-watch or steward shutdown is authorized.

Run Candybar focused tests with a scratch-isolated XDG_RUNTIME_DIR (autosync uses a production endpoint-keyed flock otherwise); run agents tools/jev-clairic/tests. Report findings with concrete paths and reproducible evidence; do not claim live acceptance from offline tests. Parent must perform a real paused invocation after canonical source landing/API runner readback, before authorizing resume. Source is not deployed yet. Live API last read: paused but old routed runner was already restored by autosync.
