---
title: 'Story 7.6: Off-candybar self-watchdog'
type: 'feature'
created: '2026-10-26'
status: 'in-review'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: 'bc49f81'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-7-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** If candybar's heartbeat, dispatcher, API or host executor dies silently, the only thing that would notice is candybar itself — a scheduler must never be its own watchdog (AD-22). The migration waves (7.7+) must not start without this backstop, and today nothing pages when candybar stops firing.

**Approach (AD-22):** a `systemd --user` timer `candybar-watchdog.timer` (every 2 minutes, NOT a candybar) runs a check script that verifies three things: `https://candybar.delo.sh/healthz` answers 200, the every-minute canary candybar `watchdog-canary` (new, exec runner, no side effects) has a terminal invocation no older than 3 minutes, and `candybar-executor.service` is active. It pages ntfy once per outage and once on recovery, and touches an ok-stamp that a new infra-doctor `file_age` probe watches (a dead watchdog cannot go unnoticed).

## Boundaries & Constraints

**Always:** the watchdog runs as a systemd --user timer every 2 minutes and is never registered as a candybar; the three checks are exactly healthz, canary freshness (finished within 3 min), executor unit active; ntfy paging is once on the OK→FAIL transition and once on recovery (state file, like infra-doctor's transition model); the ok-stamp is touched only on full success of all three checks; the infra-doctor probe is committed to delorenj/infra `infra-doctor/config/probes.yaml` and deployed per its `sync.sh`; the canary candybar is registered enabled as part of this story; a CI grep gate fails if `candybar-watchdog` ever appears in a candybar manifest/registration.

**Never:** migrating any job (waves do); registering the watchdog as a candybar; auto-remediation/restart of candybar components (page-only, no remediate_* keys); re-paging every tick while an outage persists; per-candybar checks beyond the one canary.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| all green | healthz 200, canary finished ≤3 min ago, executor active | touch ok-stamp, no page; recovery page if previously failing | none |
| api down | healthz non-200/timeout | one page naming the failing check; stamp NOT touched | page failure logged, exit stays 0 (timer must not flap) |
| dispatcher stalled | healthz 200 but canary's latest terminal invocation >3 min old | one page naming canary staleness; stamp not touched | as above |
| executor stopped | unit not active | one page; stamp not touched | as above |
| repeated failure ticks | outage persists | no additional pages (state file) | none |
| recovery | all checks green after an outage | one recovery page; stamp touched; state cleared | none |
| api reachable but invocations endpoint errors | 5xx / invalid JSON on canary read | treated as FAIL (cannot prove freshness), one page | none |
| watchdog itself dies | timer stopped | infra-doctor `file_age` probe on the ok-stamp FAILs (max_age ~10 min) and pages | handled by infra-doctor, not this unit |
| canary never ran | no invocation rows at all | FAIL as stale | none |

</frozen-after-approval>

## Code Map

- `deploy/watchdog/` (new) -- `candybar-watchdog.timer`, `candybar-watchdog.service`, `candybar-watchdog.sh`, `install.sh`, `README.md` (AD-22). The unit pair is linked into `~/.config/systemd/user/` like `deploy/host/` units.
- `deploy/watchdog/candybar-watchdog.sh` -- the check: curl `https://candybar.delo.sh/healthz`; curl `GET /candybars` to resolve the canary id, then `GET /candybars/{id}/invocations?limit=1` and parse `finishedAt`/`result`; `systemctl --user is-active candybar-executor.service`. State file + ok-stamp under `$XDG_STATE_HOME/candybar-watchdog/` (`~/.local/state/candybar-watchdog/`); pages via `~/.local/bin/ntfy-alert`.
- `deploy/smoke/watchdog-canary/candybar.toml` (new) -- the every-minute canary candybar, exec runner, no side effects (pattern: `deploy/smoke/exec/candybar.toml`). Name `watchdog-canary`, schedule `* * * * *`, `noise = true`.
- `scripts/check-deploy-hygiene.sh` or a new `scripts/check-watchdog-not-a-candybar.sh` -- the CI grep gate; wire into `.github/workflows/ci.yml` next to the deploy-hygiene step.
- `delorenj/infra` repo: `infra-doctor/config/probes.yaml` -- new `file_age` probe `candybar-watchdog-alive` on `~/.local/state/candybar-watchdog/ok`, `max_age_seconds: 600`; deployed with `infra-doctor/sync.sh install` (mirrored, not symlinked).
- Reuse, do not modify: `deploy/host/install.sh` pattern for link+enable; `deploy/README.md` gains a watchdog section; `/candybars` + `/candybars/{id}/invocations` API shapes (camelCase fields `finishedAt`, `result`).

## Tasks & Acceptance

**Execution:**
- [x] `deploy/watchdog/candybar-watchdog.sh` -- the check script: three checks, transition-only ntfy paging via ntfy-alert, ok-stamp touch on full success, never exits nonzero on check failure (exit 1 only on page-send failure logged to journal) -- the timer runs every 2 min so a hard fail state churns systemd for nothing
- [x] `deploy/watchdog/candybar-watchdog.{service,timer}` -- oneshot service, timer `OnCalendar=*:0/2`, `Persistent=true`; service runs the script
- [x] `deploy/watchdog/install.sh` + `deploy/watchdog/README.md` -- idempotent link/enable/install; README documents checks, state, stamp, rollback
- [x] `deploy/smoke/watchdog-canary/candybar.toml` -- canary manifest; register it live enabled (`candybar register`) and confirm it fires
- [x] `scripts/check-watchdog-not-a-candybar.sh` + ci.yml step -- grep gate: `candybar-watchdog` must not appear under `deploy/smoke/**/candybar.toml` or as a registered name in any manifest; fails loudly without `rg` like the hygiene gate
- [x] `deploy/README.md` -- watchdog section (pair with the host-executor section)
- [x] delorenj/infra: `infra-doctor/config/probes.yaml` -- add `candybar-watchdog-alive` file_age probe; `./sync.sh install`; commit + push
- [x] tests: a shell-level test harness for the script (stub curl/systemctl via PATH) pinning: all-green stamps and stays silent; each single failure pages exactly once across repeated ticks; recovery pages once; API 5xx on invocations = FAIL. Put under `tests/watchdog/` run by a small pytest wrapper so CI covers it.
- [x] Live: install + enable the timer, run the simulated-outage test (stop `candybar-executor.service`, wait ≤2 min, confirm exactly one page and no stamp; restart, confirm recovery page + stamp), then restore and confirm green state

**Acceptance Criteria:**
- Given the live deployment, when this story lands, then `systemctl --user list-timers` shows `candybar-watchdog.timer` active with a 2-minute cadence, and the script's three checks are healthz, canary ≤3 min, executor active
- Given a simulated outage (executor unit stopped), when the timer next fires, then exactly one ntfy page is sent and the ok-stamp is not touched; when the unit is restarted and the timer fires green, exactly one recovery page is sent and the stamp is touched
- Given the story is done, when infra-doctor runs, then probe `candybar-watchdog-alive` is OK while the watchdog runs and FAILs within 10 minutes of `systemctl --user stop candybar-watchdog.timer`
- Given CI, when `candybar-watchdog` is introduced into any candybar manifest, then the grep gate fails the build

## Verification

**Commands:**
- `uv run pytest -q` -- expected: existing suite green plus the watchdog script tests
- `systemctl --user list-timers candybar-watchdog.timer` -- active, 2-min cadence
- `systemctl --user stop candybar-executor.service && sleep 130 && journalctl --user -u candybar-watchdog --since "-3min"` -- one page line, no stamp; then restart executor and observe one recovery page
- `cd ~/code/infra && infra-doctor/sync.sh check` -- no drift after install
- `curl -s https://candybar.delo.sh/healthz` -- 200 after any daemon redeploy

## Implementation Notes

- The review layers ran inline in the implementing session (no subagent capability), matching the documented deviation of stories 7.1-7.5. Two real defects were found and patched before this story was declared done; both have tests that fail on the first-cut script. See the Review Triage Log.
- Canary freshness is counted from `startedAt`, not `finishedAt`: a claimed-but-wedged run (hard-killed executor leaves `result`/`finishedAt` null) must page, and it must page when it ages out even though its `finishedAt` never exists.
- State writes (`state`, `failing`) are tmp-file + rename: a torn read on the next tick could read "fail" and suppress the first real outage page.
- The watchdog script is linked (not copied) by `install.sh`, so a repo edit is live on the next 2-minute fire without a unit restart.
- Cold-start: on install the script pages DOWN once while the canary has not yet fired (state file starts "ok"), then RECOVERED once the canary's first fire lands. Observed live at 16:50/16:52. Intended, not a bug.

## Spec Change Log

## Review Triage Log

- `candybar-watchdog.sh` canary check (first cut read the newest terminal row's `finishedAt`) -- **high (patched)**: an executor hard-killed mid-run leaves the newest row `result: null` with `finishedAt` null forever; the first cut read past it to an older terminal row and measured THAT row's freshness, so a wedged run delayed the page arbitrarily. Fixed by counting freshness from the newest row's `startedAt`; a wedged run ages out like a fire that never ran. Test `test_wedged_run_ages_out` fails on the first cut.
- `candybar-watchdog.sh` state/failing writes (first cut: `echo fail > "$STATE"`) -- **medium (patched)**: a kill mid-write leaves a torn state file; a torn file reading "fail" suppresses the first real DOWN page. Fixed with tmp-file + rename and an EXIT trap. All 12 tests re-run green.
- Blind-hunter pass (inline; floor N = min(floor(sqrt(32.8)+1),10) = 6). Candidates considered and handled or rejected: `failing` cleanup on recovery (handled, `rm -f`); stamp `touch` before state write (intentional: the stamp proves the checks passed, not the state machine); page-send failure leaves state=fail so the next green tick pages RECOVERED without a DOWN having arrived (accepted: rare, and the recovery page stays truthful); `date -d` parse failure maps to epoch 0 and reads stale (handled); duplicate canary names resolve via `head -n1` (impossible: name is a DB-unique slug); 000 code mapping on connection refusal (handled). No further survivors.
- Verification-gap pass (inline): the script's checks and paging transitions are covered by 12 stubbed-tool tests in the normal `uv run pytest` path; the AD-22 grep gate runs in ci.yml on every push; the infra-doctor probe is verified live (`sync.sh check` ok, probe present in the live config). The unit's 2-minute cadence is pinned by systemd's own `list-timers` after install rather than a test -- systemd parses OnCalendar itself.

## Design Notes

The watchdog has a watchdog: infra-doctor's `candybar-watchdog-alive` file_age probe (max_age 600s) on `~/.local/state/candybar-watchdog/ok` fails when the timer stops, so the AD-22 "never its own watchdog" rule holds transitively -- infra-doctor is itself covered by deadman probes in the same probes.yaml.
