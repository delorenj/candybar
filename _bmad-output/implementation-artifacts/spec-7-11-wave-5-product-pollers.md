---
title: 'Story 7.11: Wave 5 — product pollers (telnyx-s3-intake, gruvato-acoustid-check, mobile-deploy-hub-catchup, mobile-deploy-hub-macwatch)'
type: 'feature'
created: '2026-10-26'
status: 'in-review'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '216657b461cd32d02da41fde9dc30f8cedaeab10'
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Four product pollers (HeyMa telnyx-s3-intake every minute, gruvato-acoustid-check every 3h, mobile-deploy-hub catchup + macwatch every 2 min) still run on systemd user timers, and the AD-20 overlap-skip they depend on (deferred from 7.3, owed before any overlap-capable job migrates) is not implemented — today an overlapping systemd start is recorded `failed`.

**Approach:** First land AD-20 in the executor (overlap `skipped` + executor-backlog coalescing) with tests and a redeploy. Then migrate each of the four jobs through the AD-21 ledger (`candybar migrate plan/cutover/verify`): systemd-runner candybars with the inventory's cron + America/New_York, the `.service` units untouched (OnFailure alerting preserved), timers disabled, and each owning repo's source of truth (HeyMa README install step, gruvato unit-header install comment, mobile-deploy-hub `tools/catchup/units/install.sh`) changed and pushed so a re-install cannot resurrect a timer.

## Boundaries & Constraints

**Always:**
- Cutover order per AD-21: register `enabled:false` → retire native scheduler → enable; ledger committed before and after each state change; `source_of_truth.commit` recorded before cutover.
- Wall-clock semantics: cron strings from `cron-inventory.md` verbatim, timezone `America/New_York`; jitter (`RandomizedDelaySec`) dropped and recorded as a deviation.
- Units with `OnFailure=` (telnyx, gruvato) get NO `on_failure` hook on the runner (AD-18 forbids the duplicate; cutover refuses it).
- External-side-effect jobs (both mobile-deploy-hub jobs) are never `candybar run` to test: they verify by first scheduled fire.
- Internal jobs (telnyx, gruvato) get one manual `candybar run` after cutover, which must succeed (gruvato: succeed-or-exit-2-parity if its quota alert is open natively).
- Rollback on any failure: `candybar migrate rollback <id>` immediately; report in rolledBack.
- After the daemon code change, redeploy the container AND restart `candybar-executor` per deploy/README.md (pair deploy, AD-17); confirm `/healthz` 200 and a `deploy-smoke` fire.

**Never:**
- Never leave both schedulers live for a job, and never neither.
- Never edit the four `.service` units' directives (Environment, KillMode, TimeoutStartSec, Slice/Nice, OnFailure stay).
- Never register the candybar-watchdog or any liveness guard as a candybar; no liveness-guard migration in this wave.
- Never restart hermes-gateway (no hermes jobs in this wave).
- No schema/event-vocabulary changes: `skipped.data.reason` is a free string (`overlap`, `executor_backlog_coalesced` need no schema rev).
- No manual runs of the external-side-effect jobs; no per-wave wrapper scripts.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| exec overlap | invoke arrives while same candybar's exec child runs in-process | `skipped` fact `reason=overlap`, command acked, no second child | publish failure → nak(delay=1) |
| systemd overlap (executor-started) | invoke arrives while the executor's own start of the unit is still running | in-process live set catches it: `skipped(overlap)`, acked | as above |
| systemd overlap (external/manual start) | unit `activating`/`active` from a hand start or a pre-restart run | pre-start probe catches it: `skipped(overlap)`, acked | probe raises (user manager down) → nak(delay=1), never a spurious fail |
| overlap race residual | unit goes busy between probe and `start --wait` | runner's existing refusal stands, recorded `failed` (loud, rare) | unchanged |
| manual run during live run | `trigger=manual` while live | same overlap skip (AD-20 has no manual exemption) | as above |
| backlog, one stale command | scheduled command >2 min old, a later non-skipped fire exists ≤ now | `skipped(executor_backlog_coalesced)`, acked | schedule/tz/skip read failure → nak(delay=1) (retry beats wrong skip) |
| backlog survivor | stale scheduled command, no later non-skipped fire ≤ now | runs with `trigger=misfire` (invoked event, claim row, AD-8 env all say misfire) | N/A |
| backlog all-skipped tail | later fires exist but every one is operator-skipped | the command runs (it is the latest non-skipped) | N/A |
| backlog, own fire skipped post-dispatch | the command's fire got an operator skip after its command was published | `skipped` with the operator-skip kind, acked | N/A |
| fresh command | fire ≤2 min old | runs normally, no skip reads | N/A |
| cutover guard | cutover within min(2 min, period/4) of a native/candybar fire, jitter pending, or native run active | `migrate cutover` refuses; `--wait` polls until clear | exit 1/3 per CLI contract |

</frozen-after-approval>

## Code Map

- `src/candybar/executor/worker.py` -- `_execute` checks disabled, then backlog (for every previous-minute scheduled command, not only >120s old), then overlap before `invoked`; `_skip_disabled` generalized to `_skip(reason)` (fact-only, deterministic `uuid5(EXEC_NAMESPACE, f"skipped:{reason}:{key}")`, ack; nak on publish failure). `Executor._live: set[UUID]` (not `ProcessRegistry`) is reserved synchronously after the backlog check, released in `finally`; applies to exec/hermes/executor-started systemd runs.
- `src/candybar/executor/systemd_runner.py` -- `unit_busy(host, unit) -> bool | None` probes LoadState/ActiveState (None when systemd unreachable). In-runner BUSY_STATES refusal remains the race backstop.
- `src/candybar/executor/role.py` -- wires `unit_busy` (SystemdHost.from_env), `fire_context` (schedule+timezone) and `skip_set` (widened skip-window read, `group_skips`, tz-aware) ports.
- `src/candybar/registry/store.py` -- `get_fire_context(session, candybar_id) -> tuple[str, str] | None`; `list_skips` is reused for operator skips (AD-10: SQL stays in registry/).
- `src/candybar/dispatcher/schedule.py` -- reuse `previous_fire()`/`SkipSet` (`previous_fire` calls AD-16 `fire_times()`); no separate cron math.
- `tests/test_executor_overlap.py`, `tests/test_systemd_runner.py` -- 17 overlap/backlog cases plus unit_busy probe cases; old disabled-skip tests remain green.
- `deploy/migration/ledger.yaml` -- four new entries (ids verbatim from cron-inventory.md), driven by `uv run candybar migrate plan <id> --write` then cutover/verify.
- Owning repos: `~/HeyMa/components/telnyx-s3-intake/README.md` (install step drops `enable --now` of the timer, notes candybar + rollback), `~/code/gruvato/deploy/gruvato-acoustid-check.service` header comment (same), `~/code/mobile-deploy-hub/tools/catchup/units/install.sh` (Linux branch stops `enable --now`-ing both timers; still copies all four unit files so rollback-by-path works; comment points at the ledger).
- Reuse, do not modify: the four `.service` units; `deploy/host/*`; the dispatcher's skip paths; `build_candybar_skipped`.

## Tasks & Acceptance

**Execution:**
- [x] AD-20 daemon change (files per Code Map) + tests; full suite green; redeploy container + restart `candybar-executor`; `/healthz` 200 and a post-deploy `deploy-smoke` fire
- [x] Foundation live check recorded: executor active, systemd canary's latest invocation success, tz field on `/candybars`
- [x] HeyMa source-of-truth change committed+pushed; ledger entry `systemd:telnyx-s3-intake` (cron `* * * * *`, tz America/New_York, runner `{kind: systemd, unit: telnyx-s3-intake.service}`, jitter 5 s deviation); cutover via CLI
- [x] Forced-long-run overlap proof on telnyx: hand-start `telnyx-s3-intake.service` across a minute boundary; the candybar fire is recorded `skipped(overlap)` (executor journal + skipped fact), no concurrent units
- [x] One manual `candybar run` of telnyx succeeds; `migrate verify` passes on its next scheduled fire
- [ ] gruvato source-of-truth change committed+pushed; ledger entry `systemd:gruvato-acoustid-check.timer` (cron `7 */3 * * *`, jitter 120 s deviation); cutover; one manual run succeeds (or exit-2 parity if the quota alert is open — check native state first); verify on first scheduled fire — **pending 15:07 EDT (19:07Z)**
- [x] mobile-deploy-hub `install.sh` change committed+pushed; ledger entries for `systemd:mobile-deploy-hub-catchup.timer` (`*/2 * * * *`) and `systemd:mobile-deploy-hub-macwatch.timer` (`1-59/2 * * * *`); cutovers; NO manual runs — both first scheduled fires succeeded and are `verified`
- [x] `sprint-status.yaml` 7-11 → review; `deferred-work.md` AD-20 entry marked resolved; commit+push candybar

**Acceptance Criteria:**
- Given the four timers enabled, when the wave lands, then each job has a ledger entry (`verified` for internal jobs after first scheduled fire; `cutover` + pendingFirstFire for the external two), its timer is disabled and out of `timers.target.wants`, its `.service` is byte-identical to before, and `candybar migrate status --probe` shows no native scheduler active
- Given telnyx's unit is hand-started across a minute boundary, when the candybar fire arrives, then it is recorded `skipped` with `reason=overlap` and exactly one instance of the unit ran (AD-20)
- Given the executor was down past a fire, when it returns with queued commands, then only the latest non-skipped fire runs (trigger `misfire`); the rest are recorded `skipped(executor_backlog_coalesced)` — proven by a live pause/resume drill on the executor with a test candybar or by integration test
- Given gruvato's unit keeps `OnFailure=gruvato-acoustid-alert.service`, when a candybar-started run exits 2, then the alert unit fires (ntfy path intact) and the runner carries no `on_failure`
- Given mobile-deploy-hub ships a new release (`tools/hosts.mjs` re-runs `install.sh`), when that happens after cutover, then the timers stay disabled (install.sh no longer enables them)
- Given any job fails on candybar, when discovered, then `migrate rollback` restores the timer first and disables the candybar

## Implementation Notes

- Unattended workflow run (orchestrator-dispatched): the step-02 approval checkpoint and step-03/04 human halts are recorded as proceeded-through, matching the documented deviation of stories 7.1-7.6. Review layers will run inline (no subagent tool in this session).
- Story owns the deferred AD-20 item (deferred-work.md, from 7.3): it names telnyx-s3-intake and mobile-deploy-hub-catchup — this wave's jobs — as the settle deadline.
- AD-20 skip facts are fact-only (no invocations row), the same record path as 7.5's `reason=disabled` skip; the "+ registry row" wording in AD-20 refers to the dispatcher's skip-entry rows for operator skips. Skips are visible via the executor journal and the `bloodbank.cron.candybar.skipped` event. An overlap-skipped fire has no invocation row (nothing ran).
- Overlap applies to manual triggers too (AD-20 states the rule without exemption); a skipped manual run surfaces via the `skipped` event and executor journal, not as an API invocation row.
- Backlog threshold constant: 120 s (AD-20's "more than 2 minutes").
- Design refinement during implementation (2026-10-26): the "backlog walk cap 1440" matrix row was replaced — `previous_fire()` walks straight back from `now`, so the check is O(stored skips), not O(backlog minutes), and needs no cap. The replacement row pins the case where the command's OWN fire was operator-skipped after dispatch (latest non-skipped fire is older than the command's fire): it is recorded `skipped` with the operator-skip kind, not `executor_backlog_coalesced`.
- Live foundation check (17:31Z): executor active; `/healthz` 200; systemd canary's latest scheduled invocation `success` with nonzero memory/cpu; `/candybars` returned `timezone` on every row. Final daemon pair-deploy at 18:12Z (after the exact-minute boundary fix), post-deploy `deploy-smoke` success at 18:13Z, memory 262144B; public `/healthz` 200. Unit suite after 3 DST cases, tick-parse guard and exact-boundary regression: 1111 passed, 71 skipped; ruff clean. The backlog tests pin America/New_York fall-back fixed-hour, fall-back wildcard-hour and spring-gap semantics using the shared fire-time helper.
- Source-of-truth retirement commits pushed: HeyMa `fe19f9f900d25b29fe325e16f5e07839eddc7dc8`, gruvato `b3346622926fb6010a8da3300decbafce288f415`, mobile-deploy-hub `403dc4c39b3697533c2475e7b17f6dff2818c41e` (+ installer regression tests `fff1e30`). The current installed hub release's `install.sh` copy was updated to match the repo; a hub release re-installs the non-enabling script. All four `.service` unit directives are unchanged; `OnFailure` is still present on both telnyx and gruvato. Re-ran the installed hub `install.sh v1.2.25` at 14:04 EDT: both timers stayed inactive; the two focused Node suites passed 57/57. The current release's installer remains byte-identical to the repo version.
- All four jobs cut over by `candybar migrate`, timers retired; four committed `deploy/migration/manifests/<name>/candybar.toml` files match the ledger definitions exactly. `migrate status --wave 7.11 --probe`: telnyx, catchup, macwatch verified; gruvato cutover, first scheduled fire 19:07Z pending (manual check succeeded). No external job was manually run.
- First scheduled fires: telnyx 17:47Z success, memory 75460608B; catchup 17:50Z success, memory 43098112B; macwatch 17:51Z success, memory 31342592B. Internal manual runs: telnyx 17:47:38Z success, gruvato 17:48:34Z success. Gruvato's native check last succeeded 16:08:05Z, no alert open.
- Forced-long-run overlap proof: hand-started `telnyx-s3-intake.service` at 13:52:39 EDT across its 13:53 scheduled fire, finished 13:53:05 with exit 0; executor logged `telnyx-s3-intake skipped (overlap)` at 13:53:00, and no second unit run was started. The `skipped` fact is deterministic per (reason, key).
- Executor pause/resume drill 17:37:08–17:41:00Z: tick commands queued while executor stopped. The first code cut coalesced older 17:38 but allowed 17:40 to run just after 17:41, which overlap-skipped the 17:41 fire — a real ordering defect caught live. Two regression fixes landed: `9fc8b20` checks every earlier-minute command for a later fire (2m threshold now only relabels a survivor to misfire); `73e31d7` decides backlog before reserving the overlap slot, so a slow DB read cannot block the current fire. Both re-deployed and tested, no rollback of migrated jobs needed. The drill cost one DOWN/RECOVERED watchdog page and interrupted an unrelated long-running task on the executor (recorded fail); do not repeat it lightly.

## Spec Change Log

## Review Triage Log

## Design Notes

Why the overlap probe lives in the worker, not only in the runner: the skip must be recorded BEFORE `invoked` is published and the claim row commits, or a skipped run would masquerade as a started invocation. The runner's own BUSY_STATES refusal stays as the backstop for the check-then-start race (a hand `systemctl start` landing between probe and start) and still records `failed` — loud and rare beats silent.

Backlog coalescing needs no inter-command coordination: "a later non-skipped fire ≤ now exists" is a pure function of (fire, now, schedule, skips), so exactly one command in any queued burst survives regardless of processing order.

## Verification

**Commands:**
- `uv run pytest -q` -- existing suite green plus the new AD-20 tests
- `uv run ruff check .` -- clean
- `curl -s https://candybar.delo.sh/healthz` -- 200 after redeploy
- `uv run candybar migrate status --wave 7.11 --probe` -- four entries, native schedulers inactive
- `uv run candybar migrate verify <id>` -- internal jobs reach `verified`; external jobs exit 3 until first scheduled fire
- `systemctl --user list-timers --all | grep -E 'telnyx|acoustid|mobile-deploy'` -- timers inactive
- `journalctl --user -u candybar-executor.service --since -10min | grep -i 'overlap\|coalesced'` -- the forced-long-run proof

**Manual checks (if no CLI):**
- `GET /candybars/{id}/invocations?limit=3` for each migrated job after its first scheduled fire: trigger `scheduled`, metered (memoryPeakBytes > 0 for systemd units)
