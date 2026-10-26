---
title: 'Story 7.12: Wave 6 — IntelliForia operations (4 jobs)'
type: 'feature'
created: '2026-10-26'
status: 'done'
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
- [x] intelliforia repo: `infra/pull-prod/` tracked home + install.sh; `infra/client-health/install.sh` stops enabling timers; staging README/timer/mise comment updated; commit + push (9ff09c5d)
- [x] Ledger: add 4 entries (wave 7.12), `plan --write`, record the intelliforia commit sha, commit ledger (d14d229)
- [x] Cut over all four jobs with `candybar migrate cutover`; commit ledger (0b0b1c7)
- [x] Manual `candybar run` smoke for `intelliforia-client-health` and `intelliforia-client-health-monday` (internal); both succeeded
- [x] Spec + sprint-status updates; commit + push candybar

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

## Results (2026-10-26)

**Cutovers (all four, one scheduler each at every step):**

| inventory id | candybar | id | first fire |
|---|---|---|---|
| systemd:intelliforia-pull-prod.timer | intelliforia-pull-prod | 01a17938-39bc-74a6-a5d3-26e2d01f9f75 | Tue 04:15 EDT (2026-10-27T08:15Z) |
| systemd:intelliforia-client-health.timer | intelliforia-client-health | 01a17938-56bc-7741-aba0-83c8ba8cf906 | Tue 05:30 EDT |
| systemd:intelliforia-client-health-monday.timer | intelliforia-client-health-monday | 01a17938-5cb9-7594-a7a6-f6a3e96e3a4b | Mon 2026-11-02 08:30 EST (13:30Z — correct across the Nov 1 DST end) |
| systemd:intelliforia-staging-track-main.timer | intelliforia-staging-track-main | 01a17938-6222-7abf-b39e-41495eff680f | Tue 07:05 EDT |

**Verification:**
- Foundations live before cutover: `/healthz` 200, `candybar-executor.service` active, `deploy-smoke-systemd` canary succeeding with metering (17:00Z, memory 3.0 MB).
- `migrate status --wave 7.12 --probe`: all four `cutover`, candybar enabled, native=retired.
- Manual runs (internal jobs): `intelliforia-client-health` success in 1.3 s (journal: "health snapshot … (12 organizations)", "no tripwires fire today"), metered memory 39 MB / cpu 0.53; `intelliforia-client-health-monday` success, exit 0, metered 36 MB. Both ran through the candybar drop-in/holder path on the unchanged units.
- External jobs (pull-prod, staging-track-main) were NOT manually run (AD-21); their first scheduled fires tomorrow morning are the proof. Ledger `verify` stays pending for all four until then.

**Commits:** intelliforia 9ff09c5d (source of truth); candybar d14d229 (plan), 0b0b1c7 (cutover ledger).

## Independent Review (2026-10-26)

Review layers ran inline (no subagent capability in this session — same documented deviation as 7.1-7.6).

- **AC1 (ordering):** schedules registered exactly as inventoried; the ExecStartPre wait and track-main stand-down live in the unchanged units/scripts. Holds by construction; observable at tomorrow's 04:15/05:30/07:05 fires.
- **AC2 (tracked home + pushed):** infra/pull-prod/ created with verbatim unit copies + repo-sync; install.sh refuses (exit 1, warning) to leave an enabled timer outside a rollback. Holds.
- **AC3 (SuccessExitStatus=75):** the systemd runner's verdict comes from the unit's `Result` property (systemd_runner.py), and systemd reports `success` for exit 75 under `SuccessExitStatus=75`. The unit is unchanged, so the mapping is systemd's own. Holds by construction; a live 75 needs an unreachable droplet and was not forced.
- **AC4 (env preserved):** `CLIENT_HEALTH_NOTIFY_CAIT=0` is a unit `Environment=` line (untouched); the ntfy token is `op read` by the script at runtime over the unit's PATH (untouched). The manual run exercised exactly this path and succeeded.
- **AC5 (no manual runs of staging-touching jobs):** honored.
- **Rejected candidate finding:** pull-prod's RandomizedDelaySec=15m drop means the candybar fires at 04:15 sharp where the timer fired 04:15-04:30. Recorded as an accepted deviation in the ledger (the jitter existed to avoid hammering prod at a round number alongside on-the-hour jobs; 04:15 is not on the hour and the inventory classified the job migrate with the cron `15 4 * * *`).
- **Rejected candidate finding:** both client-health manual runs started in the same second. They are distinct units with no shared lock; the nightly pull-prod ordering guard only matters against pull-prod, which was idle. Not a defect.

**Deferred:** none. Ledger `verify` for the four entries completes at their first scheduled fires (orchestrator/owner follow-up; external jobs tracked in the story's pendingFirstFire report).
</content>
