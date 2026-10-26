---
title: 'Story 7.16: Wave 10 — personal LLM jobs'
type: 'feature'
created: '2026-10-26'
status: 'in-progress'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '8576544b215cbf3d0b98df755e4ace6d226301ad'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-7-context.md'
  - '{project-root}/_bmad-output/planning-artifacts/cron-inventory.md'
  - '{project-root}/deploy/migration/README.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Five personal LLM jobs (tolt-forge-daily, tolt-items-daily, pr-crusher, honda-hunt-daily-check, delodocs-wiki-self-tuning-weekly) still fire from systemd user timers, outside candybar's metering and control surface; each cutover must keep the unit's hardening and never leave the job on two schedulers or none.

**Approach:** Each job becomes a candybar with an AD-18 `systemd` runner (`{kind: systemd, unit: <name>.service}`) so the `.service` keeps every directive and only the `.timer` retires. Drive every cutover with `candybar migrate` (AD-21): ledger entry, source-of-truth change committed and pushed in the owning repo, `plan --write`, `cutover`. All five carry `native_failing: true` (pre-existing failures) and verify on result parity at the first scheduled fire.

## Boundaries & Constraints

**Always:** wall-clock semantics preserved (`America/New_York` per AD-16); cutover order register-disabled → retire timer → enable; the `.path` trigger `tolt-items-inbox.path` stays untouched; the DeLoDocs vault is not git, so its source-of-truth change is an in-file header comment and the ledger records `nogit-sha256:<sha256 of the edited file>` as the `commit` token plus the diff in the spec; no daemon code changes (no container redeploy); `git pull --rebase` before every push (other waves run concurrently).

**Never:** manually `candybar run` the three external-side-effect jobs (tolt-forge-daily, tolt-items-daily, pr-crusher); touching `tolt-items-inbox.{path,service}`; editing the job scripts to add result-file/token reporting (AC allows token spend 0 when the unit's tooling does not expose it); re-enabling any retired timer; changing what any job does or when it fires on the wall clock.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| cutover, all 5 | planned entry, source-of-truth committed, no fire within guard | candybar registered+enabled, timer disabled, status `cutover` | any failure after the timer is touched restores it (CLI) |
| tolt timers are `systemctl link`-ed | disable removes the link (systemd 257) | rollback enables by resolved repo path (`plan --write` fills `unit_file`) | restore fails → candybar never disabled |
| internal job manual run | honda-hunt, wiki-self-tuning after cutover | one manual run; a failure matching the pre-existing native failure (codex token / hermes timeout) is parity, not a migration defect | a NEW failure mode → rollback via ledger |
| external job first fire | tolt ×2, pr-crusher | verified only by the first scheduled fire (pendingFirstFire) | fail with native-parity result → still verified (native_failing) |
| DeLoDocs re-copy | vault timer file re-copied by hand | header comment tells the operator candybar owns scheduling; diff recorded in this spec | n/a |

</frozen-after-approval>

## Code Map

- `deploy/migration/ledger.yaml` -- gains five entries (ids verbatim from cron-inventory.md:126-130); the committed manifest record per AD-21
- `src/candybar/migration/{ledger,ops,native}.py` -- the CLI; no changes
- `src/candybar/executor/systemd_runner.py` -- self-installs `<unit>.d/50-candybar.conf` + holder reference on first run; no changes
- `~/code/tower-of-lost-things/ops/README.md` line 167 -- retire the `.timer` enable/link instruction for `tolt-items-*` and `tolt-forge-daily` (owner repo, commit+push)
- `~/code/tower-of-lost-things/foundry/cron/tolt-{forge,items}-daily.timer` -- add retired-by-candybar header comment (linked units; disable removes the links)
- `~/code/pr-crusher/scripts/install.sh` (~line 89) -- remove `systemctl --user enable --now pr-crusher.timer` under `--enable`; print that candybar schedules it (owner repo, commit+push)
- `~/code/DeLoDocs/Projects/project-fuckudeer/scripts/systemd/honda-hunt-daily-check.timer` and `~/code/DeLoDocs/_vault/Workflows/systemd/delodocs-wiki-self-tuning-weekly.timer` -- header comments (not git; sha256 recorded)

## Tasks & Acceptance

**Execution:**
- [ ] foundation live-check: `timezone` field on `/candybars`, `candybar-executor.service` active, `deploy-smoke-systemd` invocations succeeding
- [ ] tower-of-lost-things source of truth (README:167 + two timer headers) -- commit+push, record sha
- [ ] pr-crusher `scripts/install.sh` -- commit+push, record sha
- [ ] DeLoDocs vault timer headers + sha256 tokens (no git)
- [ ] five ledger entries (wave `7.16`, `native_failing: true`, deviations: jitter/Persistent/AccuracySec dropped)
- [ ] `candybar migrate plan --write` per id; commit ledger
- [ ] `candybar migrate cutover` per id; commit ledger after each
- [ ] one manual `candybar run` for each internal job (honda-hunt, wiki-self-tuning); record result vs pre-existing failure
- [ ] sprint-status + spec; commit+push candybar repo

**Acceptance Criteria:**
- Given the five timers enabled, when the wave lands, then each job has exactly one live scheduler (candybar), its timer disabled, and its `.service` unchanged.
- Given a re-install of tower-of-lost-things or pr-crusher from source, when its install path runs, then it does not re-enable the retired timer.
- Given `native_failing: true` entries, when the first scheduled fire lands, then `candybar migrate verify` accepts result parity and marks the entry verified.

## Implementation Notes

## Spec Change Log

## Review Triage Log

## Design Notes

- **systemd runner for all five** (AD-18): pr-crusher's bwrap/namespace hardening, MemoryMax/CPUQuota and systemd-creds, and tolt's batch.slice/TimeoutStartSec=9h survive only inside the unit; an `exec` runner would silently drop them.
- **Schedules:** `0 16 * * *`, `0 3 * * *`, `0 3 * * *`, `0 9 * * *`, `45 8 * * 1`, all `America/New_York` (inventory rows 126-130). Deviations recorded per entry: `RandomizedDelaySec` (60s/120s/15m) dropped, `Persistent=true` dropped (AD-6 misfire owns catch-up), `AccuracySec=1min` dropped.

## Verification

**Commands:**
- `uv run candybar migrate status --wave 7.16` -- five entries, status `cutover`
- `systemctl --user list-timers --all | grep -E 'tolt|pr-crusher|honda|delodocs-wiki-self'` -- none of the five timers listed
- `curl -s localhost:8700/candybars | grep -c America/New_York` -- 5 new candybars enabled with the NY zone
- `git -C ~/code/tower-of-lost-things log --oneline -1` and `git -C ~/code/pr-crusher log --oneline -1` -- source-of-truth commits pushed
