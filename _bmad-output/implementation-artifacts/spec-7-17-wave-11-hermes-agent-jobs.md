---
title: 'Story 7.17: Wave 11 — Hermes agent jobs'
type: 'feature'
created: '2026-10-26'
status: 'in-progress'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '8576544'
context:
  - '{project-root}/_bmad-output/planning-artifacts/epics.md'
  - '{project-root}/_bmad-output/planning-artifacts/cron-inventory.md'
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-7-context.md'
  - '{project-root}/deploy/migration/README.md'
  - '{project-root}/deploy/migration/ledger.yaml'
---

## Intent

Move the six live Hermes agent-mode cron jobs off the Hermes ticker onto candybar's `hermes` runner (AD-19), so their schedules live in the candybar registry while execution, sessions, monitors and Telegram delivery stay in Hermes. This is the final migration wave and the only one that (a) deploys the 7.4 `scheduler: external` pin to the fleet and (b) batches the one `hermes-gateway-restart` the whole epic needed.

**Jobs (inventory ids):**

| id | profile | name | native schedule | candybar cron | timezone | side effects |
| --- | --- | --- | --- | --- | --- | --- |
| `hermes:33god-pm:777c511a009c` | 33god-pm | cronjob_manage (crank beat) | `*/5 * * * *` | `*/5 * * * *` | America/New_York | external (send-keys into panes) |
| `hermes:33god-pm:f3db72089593` | 33god-pm | aperture-lane-beat | every 120m | `0 */2 * * *` | UTC | external (commit+push, dispatch) |
| `hermes:delodocs-pm:fc39bd8b33c8` | delodocs-pm | delodocs-triage-second-pass | `0 9 * * *` | `0 9 * * *` | America/New_York | internal |
| `hermes:dumply:8aecd027dbd0` | dumply | daily-story | `30 20 * * *` | `30 20 * * *` | America/New_York | external (Telegram w/ Carrie, Plane) |
| `hermes:peter-griffin:b240575fae0c` | peter-griffin | Daily token usage + anomaly report | `53 5 * * *` | `53 5 * * *` | America/New_York | external (report files) |
| `hermes:peter-griffin:3684c5aa8eac` | peter-griffin | Daily token optimization | `23 6 * * *` | `23 6 * * *` | America/New_York | external (one prod change) |

## Boundaries & Constraints

**Always:** cutover order per AD-21 — register candybar `enabled=false`, pin the Hermes job `scheduler: external` (`hermes cron edit <id> --scheduler external`), then enable the candybar; every entry recorded in `deploy/migration/ledger.yaml` with exact rollback (`hermes cron edit <id> --scheduler internal`); the candybar's `runner` is `{kind: hermes, profile, job_id, timeout_s}`; timezone per the inventory row; source-of-truth changes committed and pushed to the owning repo BEFORE `migrate cutover` (its `source_of_truth.commit` gate requires it); the ONE `hermes-gateway-restart --reason` runs after ALL six jobs are pinned external, never during; the crank deadman (`061c4dbedc1c`, excluded class (b)) is untouched and must keep working — verified after the crank cutover; the peter-griffin ordering (06:23 gates on 05:53's manifest <2h old) verified on first scheduled fires; external-side-effect jobs are never manually `candybar run`.

**Never:** changing what the agent does, its prompt, monitor, deliver target or workdir; pausing/removing the Hermes job record (it must still exist — the runner triggers it); migrating the excluded crank-watchdog `061c4dbedc1c`; running `candybar run` on any of the five external jobs (only `delodocs-triage-second-pass`, internal, gets one manual verification run); restarting hermes-gateway more than once or with anything but `hermes-gateway-restart --reason`; touching other waves' jobs.

**Decision (recorded):** `f3db72089593` keeps the inventory's mapping `every 120m` → `0 */2 * * *` UTC. The native interval is anchored to the last run (currently ~:13 odd hours); the candybar phase-locks it to even hours at :00. That is the documented accepted deviation for interval→cron jobs ("phase is not preserved; only the interval matters", cron-inventory.md).

**Manifest home (recorded):** migrated-job manifests live under `deploy/migration/manifests/<slug>/candybar.toml` in this repo — the ledger is already here, the waves all register from one place, and the owning repos' sources of truth are Hermes job records, not candybar manifests. (The spine's structural seed does not pin a wave-manifest directory; this adds it next to `ledger.yaml`.)

## Source-of-truth changes per job (do before cutover, commit+push)

1. `hermes:33god-pm:777c511a009c` — delorenj/agents `~/.agents/tools/jev-clairic/hermes/cron-jobs.json` (the recreate-snapshot): add `"scheduler": "external"` to the `777c511a009c` entry so a recreate restores the pinned form, not a ticker-live job. Commit+push delorenj/agents (stage only that file; other agents share the index).
2. `hermes:33god-pm:f3db72089593` — NO tracked source of truth exists (job record gitignored; monitor script `aperture-lane-monitor.sh` unversioned in `~/.hermes/profiles/33god-pm/scripts/`). Recorded in the ledger as unversioned with the live record diff (scheduler field). The story ALSO gives the monitor script a tracked home: commit it to `~/.agents/tools/jev-clairic/hermes/` next to `zcrank-gate.sh` (same profile, same install.sh copy pattern already covers `hermes/*.sh`) — this is the 7.15-style "unversioned wrapper gets a tracked home" requirement the epic applies to this wave too.
3. `hermes:delodocs-pm:fc39bd8b33c8` — unversioned (DeLoDocs vault is not git). Ledger records the live jobs.json diff.
4. `hermes:dumply:8aecd027dbd0` — unversioned (`~/.hermes/profiles/dumply/cron/jobs.json`). Ledger records the diff.
5. `hermes:peter-griffin:b240575fae0c` — unversioned. Ledger records the diff.
6. `hermes:peter-griffin:3684c5aa8eac` — unversioned. Ledger records the diff.

## I/O & Edge-Case Matrix

| Scenario | State | Expected | Handling |
| --- | --- | --- | --- |
| register before restart | gateway still runs old ticker code | pin is inert; candybar registered disabled | allowed — the cutover only enables candybars AFTER the restart |
| cutover before restart | `scheduler: external` not yet honored | ticker keeps firing AND candybar would fire → double-run | NEVER enable candybars before the batched restart; order is pin-all → restart → enable-all |
| restart kills a running beat | crank beat in flight at restart | hermes-gateway-restart waits for idle by design; in-flight turn ends first | batch the restart, run it once, after pins |
| a migrated job fails first fire | result fail | `migrate verify` exit 1 → rollback via ledger (restore internal, disable candybar) | rolledBack in story report |
| peter-griffin ordering | 3684c5aa8eac at 06:23 reads 05:53 manifest | manifest <2h old → optimization proceeds | verified on 2026-10-27 morning fires (both are next-day jobs) |
| crank deadman | watchdog reads `~/.hermes/profiles/33god-pm/cron/output/777c511a009c/*.md` | hermes-runner attempts still write output there | verify the dir gets a new beat file after cutover |
| dumply delivery | telegram:-5546637302 interactive session | delivery still works via hermes gateway | verified on first scheduled fire (20:30) |
| verify window | 4 of 6 jobs fire next morning (05:53/06:23/09:00/20:30 local) | verify can't complete same-day | mark cutover in ledger, `verify` pending → status 3; first fires prove it |

## Tasks & Acceptance

**Execution:**
- [x] Write the six manifests under `deploy/migration/manifests/<slug>/candybar.toml` (names: `crank-beat`, `aperture-lane-beat`, `delodocs-triage-second-pass`, `dumply-daily-story`, `token-usage-report`, `token-optimization`), each `runner = {kind="hermes", profile=..., job_id=..., timeout_s=3600}`, tags `[migrated, hermes-agent, wave-7-17]`, timezone per inventory. Noise: default false EXCEPT `crank-beat` (`*/5` = 288 fires/day → `noise=true` like the every-minute canaries; it is a high-frequency agent heartbeat).
- [x] Commit `aperture-lane-monitor.sh` to delorenj/agents `tools/jev-clairic/hermes/` and run its `install.sh` (idempotent copy) so the live script now has a tracked home; add `"scheduler": "external"` to `777c511a009c` (and `f3db72089593` if present — it is not; snapshot only covers the jev jobs) in `hermes/cron-jobs.json`; commit+push delorenj/agents (path-scoped). `install.sh` copied the monitor correctly (matching MD5), but later failed on its pre-existing `skills/operator-tools` path (profile skills are now flat under skillex); no install of the monitor is pending.
- [x] Add the six ledger entries (`uv run candybar migrate plan <id> --write` per job; hermes kind defaults already fill disable=`edit --scheduler external` / rollback=`edit --scheduler internal`); fix up candybar names/manifest-derived fields; commit ledger.
- [x] Register all six candybars `enabled=false` (via `candybar migrate cutover` up to its pin step — the CLI's hermes cutover edits the scheduler, so run cutovers in the safe window: register disabled → pin external → STOP before enable; if the CLI's cutover is atomic, run pin steps by hand and register via API/manifest, then record `candybar_id` in the ledger).
- [x] `hermes-gateway-restart --reason "epic-7 wave 7.17: activate scheduler:external pin for six migrated agent jobs"` — exactly once, after all six pins.
- [x] Enable the six candybars; confirm none double-fires (ticker no longer fires external jobs). Two crank ticks at 17:30 and 17:35 UTC each produced exactly one Hermes output file and one candybar invocation.
- [x] Crank deadman check: after the first crank-beat candybar fire, a new file appears in `~/.hermes/profiles/33god-pm/cron/output/777c511a009c/` and crank-watchdog stays green.
- [x] `delodocs-triage-second-pass` (internal): one manual `candybar run` after enable, must succeed; record verification. Invocation `e469178a-7679-4585-90b6-73d4735a1320` succeeded in 47.6s at 17:48Z, tokenSpend=139985; the host executor's Prometheus counter agrees.
- [ ] External five: list in pendingFirstFire; `migrate verify` them after their first scheduled fires (next morning for the peter-griffin pair and delodocs if the 09:00 fire is missed today; 20:30 for dumply; even-hours for aperture; 5-min for crank). Crank verified: scheduled invocation `6bd5a5b5-0731-5498-ac1e-dbac6738e6ad` at 17:30Z, success, metered; others pending.
- [ ] Update sprint-status.yaml (7-17 → review) and this spec; commit+push candybar; confirm `git unpushed` clean for candybar, delorenj/agents, and any other repo touched.

**Acceptance (Given/When/Then):**
- **Given** stories 7.1-7.6 done and live **When** the wave lands **Then** each job's Hermes record is `scheduler: external`, its candybar fires `hermes -p <profile> cron run <id> --wait` on the recorded cron+tz, and rollback is `scheduler: internal` per the ledger
- **Given** the crank beat migrated **When** it fires under candybar **Then** the excluded crank-watchdog still sees fresh beats in the hermes output dir
- **Given** the peter-griffin pair migrated **When** 06:23 fires **Then** it gates on the 05:53 manifest <2h old (verified on first scheduled fires)
- **Given** a hermes-runner invocation **When** it completes **Then** token spend from the attempt record lands in candybar metrics (AD-8 result file), and dumply still delivers to Telegram
- **Given** all six pinned external **When** the one batched `hermes-gateway-restart` runs **Then** it runs exactly once, after every pin, and the ticker never fires a migrated job again

## Open Questions

- None blocking. (Known: 4 of 6 jobs verify next-day; the story reports `partial` with pendingFirstFire until then.)

## Spec Change Log

- 2026-10-26: drafted from epics.md Story 7.17 + cron-inventory.md + live job records.
- 2026-10-26: implementation: six manifests and ledger entries committed (`216657b`, `85985fb`); delorenj/agents source-of-truth commit `03761d6`; all six registered disabled, pinned `scheduler=external`, one batched gateway restart (pid 584698 → 3248021), then six candybars enabled; ledger state commits `79ce5de`, `bf7bb8f`. Concurrent wave commits temporarily clobbered the ledger; wave-7.13 agent restored all entries in `1365145` (my six included), with live ids and cutover timestamps preserved. Crank-beat first fire verified in `1a9cca2` (17:30Z scheduled success, 170MB peak, 20.9s, no double-fire at 17:35Z). DeLoDocs manual run succeeded at 17:48Z (139985 tokens, counter matches). Aperture, dumply, peter-griffin pair and DeLoDocs first scheduled fire pending.
