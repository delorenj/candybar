# Scheduling policy local-only source handoff

Source-only; no service restarted, schedule resumed, live runner patched or notification sent by this worker. Parent independent review remains required.

Agent gate branch `fix-policy-agent-attention` is pushed as `6a28d039ff722670ac49ec6c420cd7216fccb92e` in delorenj/agents.

## Verification
- Focused Candybar suite: 116 passed; `uv run ruff check .`: all checks passed.
- Existing agent attention tools suite: 47 passed, 5 subtests.
- Raw outputs: `policy-local-only-test-output.txt`, `policy-agent-attention-test-output.txt` in this directory.
- Real offline CLI test covers preserved valid legacy Telegram pending bytes/refusal, actual detector exit 2 preserving state, active audit/repeat/recovery and zero sender calls (tripwire executable). Canonical CLI register and autosync repair-old-route/readback are exercised in memory; paused lifecycle is preserved.

## Residual flap, read-only diagnosis
Canonical `deploy/migration` inode 105924329 had an exclusive flock first attributed to PID 3538128, later PID 3288117. Both were long-running installed uv-tool `candybar watch /home/delorenj/code/33GOD --if-present`, parent 4334. A direct shared LOCK_NB probe reproduced BlockingIOError. The newapi ledger read is valid, rolled-back, disable_pending=false, exact UUID 01a1793a-d2a0-7070-b267-4f6d3e41a3b5; API registration is disabled with matching newapi-allowances.service runner. `autosync._reconcile` holds migration directory locks while reading/syncing all definitions; multiple watcher processes contend. This explains observed unknown at probe time, but does not certify every historical alternating transition. No weakening of lock/rollback verification is justified. Source now distinguishes lock contention from malformed/unavailable evidence; unknown remains local. Deduplicate/re-scope watcher ownership under separate reviewed work if needed, not by stopping sessions/services here.

True violations remain docker-stack-ssbnk-cleanup.timer and tailscale-monitor.timer (system scope); no timers disabled and no exceptions granted.

## Other operational senders (read only)
- Candybar `src/candybar/steward/notify.py`: `StewardNotifier`, durable `candybar-steward-notify`, every completed/failed task except operator cancel; defaults target telegram:7564050286 / profile 33god-pm. Config owner `~/.config/candybar/steward.env`, CANDYBAR_STEWARD_NOTIFY_TARGET / PROFILE. The file has no notify overrides, so defaults apply. Host owner `deploy/host/candybar-steward.service`, EnvironmentFile steward.env. Do not set TARGET=off indiscriminately: failures and meaningful outcomes need distinct treatment; no change made.
- Bloodbank `services/target-watch/targets/selftest.toml`: watch-selftest, exact synthetic email watch-selftest@delo.sh or label Watch Selftest, notify_only sink to telegram:7564050286. `target_watch/paths.py` includes built-in targets by default; override TARGET_WATCH_PATHS or --targets controls selection. `target_watch/sink.py` uses TARGET_WATCH_HERMES_PROFILE default 33god-pm and hermes send. Owner services target-watch-match.service / target-watch-sink.service and that Bloodbank component, not Candybar detector.
- Human Jim is separately owned by `~/code/james-brennan/.watch/jim-brennan.toml`, target jim-brennan, Plane ticket sink with Telegram plus SIP ring and 10-minute ring cooldown. Untouched. Blanket shutdown/route disable would break meaningful client delivery.

## Critical deploy ordering (parent only after independent review)
Later read-only API observation found scheduling-policy-drift still paused **but autosync had already restored the old routed runner**. Live containment alone is not durable. Land the reviewed canonical Candybar manifest and gate source first; do not resume while canonical main still routes Telegram. Never sync this unlanded worktree into production.

Plain `register deploy/policy` is create-only and cannot update the existing row. From canonical Candybar after reviewed landing, use this same validated manifest through the canonical API client (no restart required):

```python
from pathlib import Path
from candybar.registry import load_manifest
from candybar.migration.client import CandybarApi
m = load_manifest(Path("deploy/policy/candybar.toml"))
a = CandybarApi()
rows = [r for r in a.list_candybars() if r["name"] == m.name]
assert len(rows) == 1
r = rows[0]
assert r["enabled"] is False
argv = m.runner["argv"]
assert "--local-only" in argv and "--profile" not in argv and "--target" not in argv
assert not any("telegram:" in arg for arg in argv)
a.update(r["id"], {"runner": m.runner, "description": m.description})
assert a.runner(r["id"])["runner"] == m.runner
assert a.get_candybar(r["id"])["enabled"] is False
```

Read any legacy transitions.json.pending.json first; local-only refuses it without deletion/send. Deliberate evidence-preserving quarantine is a parent decision, not an automated success shortcut. Once source and runner match, parent runs `uv run candybar run scheduling-policy-drift --wait` while paused, checks real invocation, local-report/full report and no transport, then reads runner again to catch autosync reversion. Only after independent acceptance decide resume separately. Active local attention reader is in the agents feature branch; source landing does not by itself prove the deployed Hermes prompt uses the updated text.
