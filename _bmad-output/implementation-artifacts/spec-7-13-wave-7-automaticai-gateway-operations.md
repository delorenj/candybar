---
title: 'Story 7.13: Wave 7 — AutomaticAI gateway operations (9 jobs)'
type: 'feature'
created: '2026-10-26'
status: 'in-review'
route: 'dispatch'
baseline_commit: '8576544b215cbf3d0b98df755e4ace6d226301ad'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Nine AutomaticAI gateway operations timers (token renewals, catalog sync, health checks, backups, free-provider scout) still run on raw systemd user timers in the client-portal repo — outside candybar's registry, metering and alerting.

**Approach:** Migrate all nine onto candybar as `systemd`-runner candybars (the `.service` units stay; only the `.timer`s retire), recorded in `deploy/migration/ledger.yaml` and driven by `candybar migrate plan/cutover/verify`, with client-portal's installer/docs changed so a re-install cannot resurrect the timers.

## Boundaries & Constraints

**Always:**
- Every cutover goes through the ledger: `plan --write` → commit ledger + manifests → client-portal source-of-truth commit+pushed → `cutover <id> --wait 600` → commit ledger → `verify <id>` (or defer to first scheduled fire).
- Runner kind is `systemd` for all nine (AD-18): units keep `EnvironmentFile`, `TimeoutStartSec`, `SuccessExitStatus`, `OnFailure=`, hardening. Never convert a unit to an `exec` argv.
- Wall-clock fidelity: OnCalendar jobs keep their local-time cron with `timezone: America/New_York`; relative-phase jobs use the inventory's recorded cron (`newapi-staff-sso` hourly at minute 7).
- `newapi-claude-token` is `native_failing: true` (219/219 exit 1 since 2026-10-21) and verifies on result parity.
- External-side-effect jobs are never manually `candybar run`-ed; their first scheduled fire is the proof.
- The AD-8 env and the user-manager environment (`OP_SERVICE_ACCOUNT_TOKEN`, DISPLAY/WAYLAND_DISPLAY for free-scout) reach the units unchanged — the systemd runner starts the unit under the same user manager.
- One unit test pins the `SuccessExitStatus=1` mapping: a unit whose `Result=success` with `ExecMainStatus=1` records a candybar success.

**Never:**
- Never touch `newapi-oauth-recovery.{service,timer}` — not in this wave.
- Never disable or unlink the nine `.service` files — the systemd runner starts them.
- Never restart hermes-gateway (no Hermes jobs in this wave).
- Never redeploy the candybar container or restart `candybar-executor` — no daemon code changes (the one test addition is test-only).
- Never hand-register via SQL or curl; only `candybar migrate` / the register API through the CLI.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| HAPPY_PATH cutover | ledger entry planned, source-of-truth commit pushed, native timer idle | candybar registered disabled → timer disabled → candybar enabled; status `cutover` | any failure after touching the timer auto-restores it and leaves the candybar disabled |
| JITTER_GUARD | `*/5` job with RandomizedDelaySec=15-20s | cutover waits (`--wait 600`) for a clean 75 s window (period/4) | refusal exits 1, no state changed |
| PARITY_VERIFY | `newapi-claude-token` first fire exits 1 | `verify` accepts on result parity (`native_failing: true`) | N/A |
| EXIT1_AS_SUCCESS | `newapi-vm-health` exits 1 (problems pushed to ntfy) | systemd `Result=success` via `SuccessExitStatus=1` → candybar records success; pinned by unit test | a real exit 2+ maps to failed with journal tail |
| RESURRECT_GUARD | `verify` finds the native timer active again | verify fails loudly (two schedulers) | rollback path restores one scheduler |

</frozen-after-approval>

## Code Map

- `deploy/migration/ledger.yaml` — the AD-21 ledger; this wave adds 9 entries (schema in `deploy/migration/README.md`).
- `deploy/migration/manifests/<name>/candybar.toml` — DECISION (no addendum-pinned location): wave manifests live here, mirroring the `deploy/smoke/<name>/candybar.toml` canary layout; the ledger `candybar:` block is what `candybar migrate` actually registers (validated as `CandybarManifest`, `src/candybar/migration/ledger.py:206`).
- `src/candybar/migration/ops.py` — plan/cutover/verify/rollback engine; cutover refuses near native/candybar fires (`guard_for` = min(2 min, period/4)) and auto-restores on failure.
- `src/candybar/executor/systemd_runner.py` `_outcome` — the verdict is the unit's `Result` property, so `SuccessExitStatus=1` already maps exit 1 to success; only a pinning test is added.
- `tests/test_systemd_runner.py` — stub-based runner tests; add the SuccessExitStatus=1 case next to the `exit-code` cases (~line 280-322).
- `~/code/client-portal/gateway/ops/systemd/*.{service,timer}` — the 9 units (sources of truth).
- `~/code/client-portal/gateway/ops/install-systemd.sh` — links every `ops/systemd/*.{service,timer}`; must stop linking the 9 retired timers.
- `~/code/client-portal/gateway/ops/README.md` (~line 356 token-watch install, ~line 366-375 "Host automation units" enable step) and `~/code/client-portal/gateway/ops/OPENROUTER-PASSTHROUGH.md` (~171-174) — enable steps to retire.

## Tasks & Acceptance

**Execution:**
- [x] `tests/test_systemd_runner.py` — add `test_success_exit_status_maps_exit1_to_success` — AC pin for the vm-health mapping
- [x] `deploy/migration/manifests/<job>/candybar.toml` × 9 — one manifest per job (name, schedule, timezone, runner `{kind: systemd, unit: <name>.service}`, tags `[migrated, automaticai]`, `noise: false`)
- [x] `deploy/migration/ledger.yaml` — author 9 entries (wave `7.13`), then `candybar migrate plan <id> --write` each to probe + fill unit_file/disable/rollback defaults
- [x] `~/code/client-portal/gateway/ops/install-systemd.sh` + `ops/README.md` + `ops/OPENROUTER-PASSTHROUGH.md` — stop linking/enabling the 9 retired timers; commit + push; record the sha as `source_of_truth.commit` in every entry
- [x] Cutover each job via `candybar migrate cutover <id> --wait 600`; commit ledger after each batch
- [x] Manual `candybar run` for the two internal jobs (`newapi-allowances`, `newapi-db-pull`) succeeded; the ledger's `verify` is pending db-pull's first scheduled fire, while the other seven were not manually invoked.
- [x] `_bmad-output/implementation-artifacts/sprint-status.yaml` — 7-13 to review
- [x] Live foundation check first: `/healthz` 200; `deploy-smoke-systemd` and `watchdog-canary` recent invocations successful; a non-UTC registration shows correct local next-fire via `/schedule/next`

**Acceptance Criteria:**
- Given waves 1-6 foundations live, when this wave lands, then all nine jobs migrate per ledger and client-portal's installer/README/PASSTHROUGH docs no longer enable the retired timers (committed + pushed to AutomaticAI-io/client-portal).
- `newapi-staff-sso` fires hourly at :07; `newapi-db-pull` fires 08:05 America/New_York (after the ~07:35 excluded anacron dump).
- `newapi-vm-health` exit 1 records candybar success; unit test pins the mapping.
- `newapi-claude-token` verifies on parity; `aai-free-scout` verifies only at its first scheduled fire.
- No job ever has two live schedulers or zero; every entry has working `rollback` commands recorded.

## Implementation Notes

- 2026-10-26: Foundation confirmed live before any cutover: `/healthz` 200, `candybar-executor.service` active, `deploy-smoke-systemd` and `watchdog-canary` succeeding on schedule. No Hermes jobs in this wave, so the hermes runner was not exercised.
- All nine registered as `systemd`-runner candybars; the `.service` units are untouched (including `SuccessExitStatus=1` on vm-health, `TimeoutStartSec`, the `.env.op` wrapper on staff-sso). The user-manager env (`OP_SERVICE_ACCOUNT_TOKEN`, DISPLAY) reaches the units unchanged — proven by `newapi-oauth-connections`' first scheduled fire succeeding (its `op` vault writes require the token).
- New pinning test `test_success_exit_status_maps_exit1_to_success` (tests/test_systemd_runner.py): `Result=success` with `ExecMainStatus=1` records a candybar success and fetches no journal. 35 passed, 6 skipped; ruff clean.
- Cutovers: db-pull, openrouter-sync, staff-sso, token-watch, free-scout went immediately; the `*/5` and `*/30` jobs waited out the 75s/120s guards via `--wait 600`. No rollbacks needed.
- client-portal source of truth: commit `f337c6f7` (pushed) — `install-systemd.sh` skips (and unlinks stale, disabled) the nine retired timers; README "Host automation units" + token-watch install and OPENROUTER-PASSTHROUGH install sections now point at the ledger; each retired `.timer` carries a RETIRED comment header.
- Verifications: allowances (manual run success exit 0, then scheduled 17:25Z success, verified), db-pull (manual run success — internal; ledger verify pending first scheduled fire Tue 08:05 ET), vm-health + oauth-connections (scheduled 17:25Z success, verified), claude-token (scheduled 17:30Z fail exit=1 = 219/219 native parity, verified on parity).
- INCIDENT (recovered): concurrent wave-7.8 agent's commit 6a011dc rewrote `ledger.yaml` from a stale copy and dropped all 38 committed entries. Restored by merging ae7cba7 (38 jobs) + the 15 new wave-7.8 entries (53 total), then re-verified claude-token and pushed 1365145. The 7.8 entries were preserved verbatim.
- Shared-tree notes: the commit 9227b60 swept in four other waves' untracked manifests and three other agents' sprint-status flips (shared working tree); 1365145 swept in a staged spec-7-9 and test_wave3_dst.py from the shared index. Content was intact in both cases; the owners' entries were preserved.
- Pending first scheduled fire (external side effects; never manually run): newapi-openrouter-sync (Tue 03:47 ET), newapi-token-watch (Tue 08:30 ET), aai-free-scout (Tue 04:37 ET), newapi-staff-sso (Mon 14:07 ET; hourly), newapi-db-pull (Tue 08:05 ET; manual-run proof already green).

## Spec Change Log

## Review Triage Log

## Design Notes

The nine candybars are thin registrations over untouched `.service` units — the wave's real work is ordering (source of truth BEFORE cutover) and the ledger record. Cron translations come verbatim from `cron-inventory.md`; deviations recorded per entry: `RandomizedDelaySec` jitter dropped (candybar fires on the minute), `OnBootSec`/`OnActiveSec` boot re-arm dropped (candybar misfire policy covers downtime), `Persistent=true` catch-up replaced by candybar's misfire/backlog rules (AD-6/AD-20).

## Verification

**Commands:**
- `uv run pytest tests/test_systemd_runner.py -q` — includes the new pin; all pass
- `uv run ruff check tests/test_systemd_runner.py` — clean
- `uv run candybar migrate status --wave 7.13 --probe` — all entries `cutover`/`verified`, native timers retired
- `curl -s https://candybar.delo.sh/healthz` — 200; `uv run candybar list` shows the nine with correct tz/next-fire
