---
title: 'Story 7.15: Wave 9 — Dev Journal and reporting'
type: 'feature'
created: '2026-10-26'
status: 'in-progress'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '79ce5de'
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-7-context.md'
  - '{project-root}/deploy/migration/README.md'
  - '{project-root}/_bmad-output/planning-artifacts/cron-inventory.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The Dev Journal's 06:00 → 06:15 → 06:30 chain and the jev-rollup sidecar run on three different schedulers (Hermes ticker, two kinds of systemd timer), so the daily report pipeline has no single inventoried, ordered, metered home.

**Approach:** Migrate the wave's five jobs onto candybar per AD-16..AD-21: the two no-agent Hermes jobs become `exec` runners (their scripts run directly; the Hermes jobs are pinned `scheduler: external` AND paused, since the running gateway predates the pin), the three infra systemd timers become `systemd` runners (only the `.timer` retires), and the Dev Journal `fleet_health` collector learns to read the candybar API so the report still sees migrated jobs.

## Boundaries & Constraints

**Always:** cutover order register-disabled → retire native → enable, driven only by `candybar migrate` against the committed ledger; wall-clock semantics preserved (all five jobs are America/New_York wall-clock); source-of-truth commits land in the owning repo (and are pushed) before the ledger records their SHA and before cutover; external-side-effect jobs (the two Dev Journal generators) are never manually run — first scheduled fire is the proof; internal jobs (`dev-journal-reconcile`, `sync-daily-reports-to-s3`, `jev-rollup-cadence`) get one manual `candybar run`; ledger committed after every mutation; only my own files/lines staged (other wave agents share the tree).

**Never:** the hermes runner for these no-agent jobs (AD-19: script directly via `exec`); touching the six wave-7.17 hermes agent jobs or any other wave's entries; a container redeploy (no daemon code changes); hand-editing jobs.json; manual runs of the external jobs; restarting hermes-gateway.

**Decisions recorded (no human to ask):** manifests live in `deploy/migration/manifests/<name>/candybar.toml` (the convention the other waves established). Hermes retirement is `cron edit --scheduler external` + `cron pause` (rollback: `edit --scheduler internal` then `cron resume`) — the pin alone is inert on the running pre-7.17 ticker, and the probe requires the pin, so both are needed. `dev-journal-reconcile` is `native_failing: true` (steady-state exit 3 = EXIT_UNMET on invalid legacy dates; verified by parity). jev-rollup's `failure_deliver: telegram` is reproduced by an `on_failure` hook: `hermes send --to telegram:7564050286`. Candybars execute the versioned script copies (`infra/scripts/dev-journal-hermes-nightly.sh`, `~/.agents/tools/jev-clairic/hermes/jev-rollup-cadence.sh`), retiring the unversioned profile wrappers.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| exec cutover (hermes no-agent) | job enabled on old ticker | candybar registered disabled; job pinned external + paused (old ticker cannot fire it, probe reads retired); candybar enabled | any failure after native touched → resume + internal, candybar stays disabled |
| systemd cutover | timer enabled, `.service` plain oneshot | timer `disable --now`; service kept for the systemd runner; first run auto-installs the 50-candybar drop-in + holder ref | rollback enables by resolved unit-file path (linked-unit rule) |
| reconcile parity | native steady-state exit 3 | candybar records `fail` exit=3; `verify` accepts on `native_failing` parity | a result OTHER than exit 3 is a real regression → investigate/rollback |
| jev-rollup fire | `*/10` NY; script branches on local H/M | env literal `TZ=America/New_York` reaches the child; tick/roll/day cadence unchanged | on_failure argv sends telegram with `CANDYBAR_FAILURE_MESSAGE` |
| fleet_health, API up | candybar API reachable | `candybar:` summary + per-candybar failure/disabled lines; hermes jobs with `scheduler=external` rendered as candybar-managed, not flagged | — |
| fleet_health, API down | connection refused | source `candybar-api` recorded failed, section degrades to `partial` with a caveat | never crashes the report |
| two schedulers check | post-cutover probe | `verify` fails loudly if the native scheduler is active again | re-run disable, fix source of truth |

</frozen-after-approval>

## Code Map

- `deploy/migration/ledger.yaml` -- append 5 entries (wave `7.15`); shared with concurrent waves, CLI-locked, never stage others' hunks
- `deploy/migration/manifests/{dev-journal-nightly,dev-journal-ensure-nightly,dev-journal-reconcile,sync-daily-reports-to-s3,jev-rollup-cadence}/candybar.toml` -- new manifests (wave convention)
- `src/candybar/migration/{ops,native,ledger}.py` -- the migrate verbs/probes (read-only this story); hermes probe: retired ⇔ `scheduler==external`
- `/home/delorenj/code/infra/systemd/user/dev-journal-{ensure-nightly,reconcile}.timer`, `sync-daily-reports-to-s3.timer` -- retirement note in each header (source of truth; no installer exists, units were hand-enabled)
- `/home/delorenj/code/infra/systemd/user/README.md` -- new: the retired-timers note + pointer at the ledger
- `/home/delorenj/.agents/tools/jev-clairic/hermes/cron-jobs.json` -- mark job `9b5dadbd1e4d` migrated-to-candybar in the versioned snapshot (delorenj/agents repo)
- `/home/delorenj/code/skillex/all-skills/delonet-daily-report/scripts/collectors/fleet_health.py` -- new `candybar-api` source: option `candybar_api_url` (default `http://127.0.0.1:8700`, empty disables), `_read_candybars` (urllib, GET /candybars + per-candybar `invocations?limit=1`), `scheduler` field on hermes job records, render block, FLEET_FIELDS + metrics additions
- `/home/delorenj/code/skillex/all-skills/delonet-daily-report/scripts/tests/test_fleet_health.py` -- stubbed-HTTP tests for the new source
- Candybar definitions (schedule, tz, runner):
  - `dev-journal-nightly`: `0 6 * * *` NY; exec argv=`[/home/delorenj/code/infra/scripts/dev-journal-hermes-nightly.sh]`, timeout_s=3600
  - `dev-journal-ensure-nightly`: `15 6 * * *` NY; systemd unit `dev-journal-ensure-nightly.service`
  - `dev-journal-reconcile`: `0 * * * *` NY; systemd unit `dev-journal-reconcile.service`; `native_failing: true`
  - `sync-daily-reports-to-s3`: `30 6 * * *` NY; systemd unit `sync-daily-reports-to-s3.service`
  - `jev-rollup-cadence`: `*/10 * * * *` NY; exec argv=`[/home/delorenj/.agents/tools/jev-clairic/hermes/jev-rollup-cadence.sh]`, env `TZ=America/New_York`, timeout_s=300, on_failure telegram hook

## Tasks & Acceptance

**Execution:**
- [ ] infra timer headers + `systemd/user/README.md` -- commit+push delorenj/infra, record SHA
- [ ] `~/.agents` jev-clairic snapshot marks 9b5dadbd1e4d migrated -- commit+push delorenj/agents, record SHA
- [ ] fleet_health candybar source + tests; skill suite green -- commit+push delorenj/skills, bump the skillex pin, commit+push delorenj/skillex, record SHA
- [ ] five manifests + five ledger entries (custom hermes disable/rollback pairs) -- commit candybar
- [ ] `candybar migrate plan --write` per id (fills `unit_file`); commit ledger
- [ ] cutover `dev-journal-reconcile` → `sync-daily-reports-to-s3` → `jev-rollup-cadence` → `dev-journal-nightly` → `dev-journal-ensure-nightly`; commit ledger after each
- [ ] one manual `candybar run` for the three internal jobs; reconcile must fail with exit 3 exactly (parity), the other two succeed
- [ ] `candybar migrate verify` wherever a first scheduled fire has landed (jev-rollup ≤10 min, reconcile ≤1 h); the rest stay `cutover` pending first fire
- [ ] sprint-status 7-15 → review (only my line; another agent has uncommitted edits in the file)

**Acceptance Criteria:**
- Given the five natives active, when the wave lands, then the chain holds as candybars `dev-journal-nightly` (06:00) → `dev-journal-ensure-nightly` (06:15) → `sync-daily-reports-to-s3` (06:30), each native scheduler probed retired, and no job ever on two or zero schedulers during cutover
- Given a paused+pinned Hermes job, when the pre-7.17 ticker ticks, then the job does not fire natively (pause) and the candybar probe reads it retired (pin)
- Given the collector change, when `reportctl` collects fleet_health with the API up, then the section reports the candybar-registered jobs and renders the two retired Hermes jobs as candybar-managed

## Implementation Notes

## Spec Change Log

## Review Triage Log

## Design Notes

- **Hermes double-retirement:** the probe (`native._probe_hermes`) reads `retired ⇔ scheduler == "external"`; the running gateway's ticker is pre-pin code and would still fire a pinned job, but it honors `enabled`/`paused_at` (`is_job_runnable`). So disable = pin (satisfies the probe, protects post-restart) + pause (stops today's ticker). Rollback order is `edit --scheduler internal` THEN `resume`: resumed-while-pinned still probes inactive.
- **systemd runner needs no per-story install:** `ensure_installed` writes `<unit>.d/50-candybar.conf` and the holder reference on first run (AD-18), and the units keep `After=network-online.target` because only the timer retires.
- **DST:** all five are fixed wall-clock jobs; after the 2026-11-01 fall-back the AD-16 helper keeps them at local 06:00/06:15/06:30 (11:0xZ) exactly like the timers' `OnCalendar=... America/New_York`.

## Verification

**Commands:**
- `uv run candybar migrate status --probe --wave 7.15` -- all five entries retired natively, candybars enabled
- `cd /home/delorenj/code/skillex/all-skills/delonet-daily-report/scripts && python3 -m unittest discover tests -q` -- fleet_health suite green
- `curl -s http://127.0.0.1:8700/candybars` -- five new candybars, `timezone: America/New_York`, enabled
- `uv run python -m pytest -q` (candybar) -- no regressions (no daemon code touched)

**Manual checks:**
- `systemctl --user list-timers --all | grep -E 'dev-journal|sync-daily'` shows the three timers inactive/disabled; the `.service` units intact
- `hermes -p 33god-pm cron list`: both jobs paused with `scheduler=external`
- next-morning chain: invocation rows at the expected UTC instants
