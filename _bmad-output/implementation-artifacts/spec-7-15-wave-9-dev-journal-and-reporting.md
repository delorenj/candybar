---
title: 'Story 7.15: Wave 9 — Dev Journal and reporting'
type: 'feature'
created: '2026-10-26'
status: 'in-review'
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
- [x] infra timer headers + `systemd/user/README.md` -- commit+push delorenj/infra, record SHA
- [x] `~/.agents` jev-clairic snapshot marks 9b5dadbd1e4d migrated -- commit+push delorenj/agents, record SHA
- [x] fleet_health candybar source + tests; skill suite green -- commit+push delorenj/skills, bump the skillex pin, commit+push delorenj/skillex, record SHA
- [x] five manifests + five ledger entries (custom hermes disable/rollback pairs) -- commit candybar
- [x] `candybar migrate plan --write` per id (fills `unit_file`); commit ledger
- [x] cutover `dev-journal-reconcile` → `sync-daily-reports-to-s3` → `jev-rollup-cadence` → `dev-journal-nightly` → `dev-journal-ensure-nightly`; commit ledger after each
- [x] one manual `candybar run` for the three internal jobs; reconcile must fail with exit 3 exactly (parity), the other two succeed
- [x] `candybar migrate verify` wherever a first scheduled fire has landed (jev-rollup ≤10 min, reconcile ≤1 h); the rest stay `cutover` pending first fire
- [x] sprint-status 7-15 → review (only my line; another agent has uncommitted edits in the file)

**Acceptance Criteria:**
- Given the five natives active, when the wave lands, then the chain holds as candybars `dev-journal-nightly` (06:00) → `dev-journal-ensure-nightly` (06:15) → `sync-daily-reports-to-s3` (06:30), each native scheduler probed retired, and no job ever on two or zero schedulers during cutover
- Given a paused+pinned Hermes job, when the pre-7.17 ticker ticks, then the job does not fire natively (pause) and the candybar probe reads it retired (pin)
- Given the collector change, when `reportctl` collects fleet_health with the API up, then the section reports the candybar-registered jobs and renders the two retired Hermes jobs as candybar-managed

## Implementation Notes

- **Foundation live-check (2026-10-26 ~13:10 EDT):** `https://candybar.delo.sh/healthz` 200; API definitions expose `timezone` and `enabled`, host `candybar-executor.service` active, `deploy-smoke-systemd` scheduled invocations succeed with metering (17:00 and 17:15 UTC). Five native jobs were active and scheduling in America/New_York. No daemon code changed; no container redeploy.
- **Source of truth:** infra 4cd59522819cbd97b3dbfbde244f790b6ad46905 adds RETIRED headers to the three timer units and `systemd/user/README.md`; infra 7c1b7d2129cdc099de084d66c6549f601b00ee8d tracks the unversioned nightly wrapper's home (`scripts/README-dev-journal.md`) and marks the canonical script. Agents 584098d9ec560584b10252ce4b326cd47f5b0378 records jev-rollup's migrated state in its snapshot; agents 9a1cce1d7664b8276d80018ab841c7a7bdc74471 sets the versioned rollup source to executable 755.
- **Collector:** skills 5ffb526fe346e999bc6ca1fabe4876ad217d7a0e adds the candybar API source and five stubbed-HTTP tests; c10180cc08a5ab065ef64c1fe40de8cbb3180560 stops counting externally scheduled Hermes jobs as ticker-owned; d798feac257d591cd0c4858f709784d3a71e92a4 lists successful candybars in detail too. Skillex pins end at eec54ddce911df9d96ccd6bf3a6fdaf88617abd5. A live collector pass read 77 candybars and rendered the migrated nightly/recovery/rollup jobs, with status `complete`. The skills suite is 450 passed + 820 subtests, 4 failing; all four fail identically on HEAD before this change (1713c60 switched skill lookup to profile dirs but the tests still build global skills dirs). New candybar tests: 6 passed.
- **Cutovers:** reconcile `bf8ebdd` (13:42 EDT), S3 sync `dcd3fad` (13:43), jev-rollup `47f6b74` (13:44), nightly `c2eeabf` (13:45), ensure-nightly `bd57925` (13:45). The two Hermes jobs are both `enabled=false`, `state=paused`, `scheduler=external`; the three timers are inactive/not-found after `disable --now`; all five candybars are enabled and `candybar migrate status --probe --wave 7.15` reports native=retired. Every ledger mutation was committed/pushed, including source-of-truth updates in `fce2366`.
- **Immediate failure and rollback (jev-rollup):** the first manual candybar run failed with `PermissionError: [Errno 13]` on the versioned source (mode 644, while Hermes's profile copy was 755). Immediately ran `candybar migrate rollback` (restored Hermes before disabling candybar), committed mode 755 in agents 9a1cce1, then re-cut over (`22087c3`) and repeated the one manual run; it succeeded (165 ms, 13.98 MB). The first scheduled fire at 13:50 EDT succeeded (exit 0, 290 ms, 16.48 MB, cpu 0.659); `candybar migrate verify` marked it verified (`79943b0`). It is not in the final rolledBack list: the native scheduler is retired again and candybar owns it.
- **Other manual proofs:** reconcile returned `fail` exit=3, 1.025s, 19.65 MB — exact parity with the native timer's `ExecMainStatus=3`. S3 mirror returned `success` exit=0, 2.021s, 14.07 MB. Both systemd runner invocations installed `<unit>.d/50-candybar.conf` and the holder reference automatically; ensure-nightly's drop-in awaits its first scheduled fire. External jobs (`dev-journal-nightly` and `dev-journal-ensure-nightly`) were not manually run.
- **Verification:** candybar suite 1104 passed, 71 skipped; `ruff check .` clean. No daemon code changed. Healthz stayed 200. `candybar migrate status --probe --wave 7.15`: all five natives retired, all five candybars enabled; jev-rollup verified, the four others pending first scheduled fire (nightly/recovery/S3 tomorrow morning; reconcile at 14:00 EDT).
- **Review (inline, no subagent capability):** The three bmad-build review lenses were executed inline. Blind-hunter found the 644 source-script mode (the immediate rollback above) and the collector detail omission (successful candybars invisible); both are fixed with a failing-on-first-cut probe/test. Edge-case-hunter identified the pre-7.17 ticker's inert external pin (pin+pause was in the spec/ledger before cutover, live jobs.json confirms both flags) and linked-unit rollback by path (the `plan --write` resolved absolute paths). Verification-gap identified the lack of first-fire proof for four jobs; it is recorded as pending, not falsely marked verified. The collector's 4 pre-existing stale tests remain deferred to its owner; nothing from this wave changed their failure set.

## Spec Change Log

## Review Triage Log

Inline review layers (no subagent tool in this session). Findings verified against the diff and live state.

| # | Finding | Verdict | Evidence / route |
|---|---------|---------|------------------|
| 1 | Jev-rollup's versioned source was mode 644; exec runner could not spawn it | high | Real: first manual run `PermissionError`; immediate `candybar migrate rollback`, fixed source mode 755 (agents 9a1cce1), re-cut over, repeat manual run success; first scheduled fire verified. |
| 2 | Fleet collector listed only failed/disabled/no-run candybars; successful migrated jobs were invisible in the report detail | medium | Real: live pass showed a summary count but no successful jobs; collector d798fea renders one bounded line per candybar, test covers success. |
| 3 | `scheduler=external` Hermes job can stay enabled in agent-mode wave 7.17, but collector counted it as ticker-owned and displayed stale last_status/next_run_at | medium | Real: live 7.17 jobs already pin external while enabled; collector c10180c excludes them from Hermes active count and directs detail to candybar registry. |
| 4 | Pin-only retirement is inert in the running gateway until wave 7.17 restart | high | Real and already handled before cutover: both no-agent jobs use `cron edit --scheduler external` AND `cron pause`; jobs.json has enabled=false/state=paused/scheduler=external. |
| 5 | Systemd `disable` of a linked unit removes the name link; rollback-by-name fails | medium | Real and already handled by `plan --write`: absolute unit_file paths stored for all three timers; no ad hoc disable commands. |
| 6 | Four jobs have no first scheduled fire yet (06:00/06:15/06:30 tomorrow; reconcile 14:00 today) | low | Expected temporal gap, not a defect: ledger status remains `cutover`, not `verified`; first-fire verification is pending. |
| 7 | Four fleet_health skill-resolution tests fail on the unchanged HEAD | low | Pre-existing: 1713c60 moved lookup to profile skills dirs, but tests still build global skills dirs; compared baseline vs current, exact same four fail. Rejected as unrelated; the six new tests pass. |

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
