---
title: 'Story 7.12: Wave 6 — IntelliForia operations (4 jobs)'
type: 'feature'
created: '2026-10-26'
status: 'in-progress'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '8576544'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/planning-artifacts/cron-inventory.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-7-context.md'
  - '{project-root}/deploy/migration/README.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The IntelliForia nightly chain (prod pull 04:15 → client health 05:30 → weekday staging track-main 07:05, plus the Monday 08:30 health summary) runs on four hand/user-maintained systemd timers. Epic 7 moves every migrate-classified job onto candybar with wall-clock semantics preserved (America/New_York; DST ends 2026-11-01) and every cutover reversible through the AD-21 ledger.

**Approach:** migrate all four as `systemd`-runner candybars (AD-18: the `.service` units stay byte-identical — Environment, SuccessExitStatus=75, ExecStartPre/Post chains, TimeoutStartSec, Nice/IOSchedulingClass all stay in force; only the `.timer` units retire). Record each in `deploy/migration/ledger.yaml`, change the source of truth in IntelliForia/intelliforia so a re-install cannot resurrect a timer (commit+push), then `candybar migrate cutover` per job. Internal jobs (the two client-health jobs) get one manual `candybar run` smoke; external-side-effect jobs (pull-prod, staging-track-main) verify by first scheduled fire only.

## Boundaries & Constraints

**Always:** cron + tz exactly as inventoried (`15 4 * * *`, `30 5 * * *`, `30 8 * * 1`, `5 7 * * 1-5`, all America/New_York); runner `{kind: systemd, unit: <name>.service}`; cutover only via `candybar migrate` (register disabled → retire native → enable); ledger committed after every state change; owning-repo changes committed and pushed BEFORE cutover with the sha in `source_of_truth.commit`; `RandomizedDelaySec` drops recorded as deviations; never both schedulers live, never neither.

**Never:** editing the four `.service` units' behavior; `candybar run` on pull-prod or staging-track-main; touching the Hermes gateway; redeploying the candybar container (no daemon code changes in this story); migrating excluded jobs.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| normal cutover | timer active, far from fire | candybar registered disabled → timer disabled → candybar enabled; ledger `cutover` | any failure after native touch auto-restores the timer |
| pull-prod exits 75 | droplet unreachable during ExecStartPost | systemd `Result=success` (SuccessExitStatus=75) → candybar `success` | runner reads `Result`; unit unchanged |
| client-health fires while pull-prod runs | pull-prod.service active | unit's ExecStartPre waits it out (unchanged unit) | n/a |
| staging fires while pull-prod runs | refresh active | track-main.sh stands down for the day (unchanged) | n/a |
| manual run of client-health | pull-prod idle, prod copy fresh (<30h) | success, snapshot written, ntfy sent | fail → rollback via ledger |
| re-install of client-health | install.sh re-run | services refreshed, timers NOT re-enabled | notice printed |
| reboot catch-up | machine slept through 04:15 | candybar misfire coalesce-to-latest replaces Persistent=true | recorded deviation-free (AD-6 mapping) |

</frozen-after-approval>

## Code Map

- `deploy/migration/ledger.yaml` (this repo) — four new entries, ids `systemd:intelliforia-pull-prod.timer`, `systemd:intelliforia-client-health.timer`, `systemd:intelliforia-client-health-monday.timer`, `systemd:intelliforia-staging-track-main.timer`, wave `7.12`. Driven by `uv run candybar migrate {plan --write, cutover}`; the CLI fills `unit_file` and default disable/rollback commands (`disable --now <timer>` / `enable --now <resolved unit file>`, AD-21 path-form for systemd 257).
- `src/candybar/migration/ops.py` — cutover/verify/rollback verbs (reuse, do not modify).
- `src/candybar/executor/systemd_runner.py` — installs each unit's `50-candybar.conf` drop-in and holder reference on first start; verdict from `Result`/`ExecMainStatus` (SuccessExitStatus=75 → `Result=success`); metering from `MemoryPeak`/`CPUUsageNSec` (reuse, do not modify).
- IntelliForia/intelliforia (`~/code/intelliforia`, origin IntelliForia/intelliforia, branch main):
  - `infra/pull-prod/systemd/intelliforia-pull-prod.{service,timer}` (NEW) — tracked home for the hand-maintained units (copied verbatim from `~/.config/systemd/user/`); `infra/pull-prod/install.sh` (NEW) installs the service (and `intelliforia-repo-sync` to `~/.local/bin` if absent) and explicitly does NOT enable the timer.
  - `infra/client-health/install.sh` — stop copying/enabling the two timers; install `.service` files only (keep the CLIENT_HEALTH_NOTIFY_CAIT=1 preservation), print the candybar notice.
  - `infra/staging/README.md` + `infra/staging/systemd/intelliforia-staging-track-main.timer` — install block drops `enable --now …timer`; timer file header marks it retired-by-candybar (kept for rollback).
  - `mise.toml` comment at `staging:track-main` — note candybar scheduling.
- Live units (`systemctl --user cat`): pull-prod (OnCalendar 04:15, RandomizedDelaySec=15m, Persistent, SuccessExitStatus=75, log file under ~/.local/state/intelliforia/), client-health (05:30, jitter 300, ExecStartPre waits on pull-prod, `CLIENT_HEALTH_NOTIFY_CAIT=0`, script reads ntfy token via `op read` at runtime), client-health-monday (Mon 08:30 America/New_York), staging-track-main (Mon..Fri 07:05, Persistent).
- Secrets: no env changes — the script's `op read op://DeLoSecrets/…` runs inside the unchanged unit with its existing PATH.

## Tasks & Acceptance

**Execution:**
- [x] Confirm foundations live: `/healthz` 200, `candybar-executor.service` active, `deploy-smoke-systemd` canary succeeding with metering (17:00Z run, memory 3.0 MB)
- [ ] intelliforia repo: `infra/pull-prod/` tracked home + install.sh; `infra/client-health/install.sh` stops enabling timers; staging README/timer/mise comment updated; commit + push
- [ ] Ledger: add 4 entries (wave 7.12), `plan --write`, record the intelliforia commit sha, commit ledger
- [ ] Cut over all four jobs with `candybar migrate cutover`; commit ledger
- [ ] Manual `candybar run` smoke for `intelliforia-client-health` and `intelliforia-client-health-monday` (internal); must succeed
- [ ] Spec + sprint-status updates; commit + push candybar

**Acceptance (from epics.md Story 7.12):**
- Ordering holds: pull-prod 04:15 → client-health 05:30 (ExecStartPre wait intact — unit unchanged) and staging-track-main 07:05 weekdays stands down while pull-prod is active
- pull-prod units have a tracked home in IntelliForia/intelliforia `infra/` before cutover; all intelliforia changes committed and pushed
- `SuccessExitStatus=75` maps to candybar success; a non-listed exit maps to fail (via systemd `Result`, unit unchanged)
- `CLIENT_HEALTH_NOTIFY_CAIT=0` and the `op://` ntfy token flow preserved verbatim (units untouched; script resolves via `op` on its existing PATH)
- staging-touching jobs (pull-prod, staging-track-main) verify by first scheduled fire only — never manually run

## Implementation Notes

- Wave-5 dependency ("Given Wave 5 done") is not met (ledger is empty; waves run concurrently per the orchestrator). Proceeding: nothing in wave 6 depends on wave 5's jobs, and the intra-chain ordering is preserved by the unchanged units.
- Cutover window: all four next native fires are ≥15h out at cutover time (13:1x EDT); the 2-minute guard is nowhere near binding.
- `intelliforia-client-health.timer` installed copy is a plain file (not a `systemctl link`), but rollback still enables by resolved path per the AD-21 default.
</content>
