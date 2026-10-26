---
title: 'Story 7.14: Wave 8 — James Brennan / IPM'
type: 'feature'
created: '2026-10-26'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '216657b461cd32d02da41fde9dc30f8cedaeab10'
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-7-context.md'
  - '{project-root}/_bmad-output/planning-artifacts/cron-inventory.md'
  - '{project-root}/deploy/migration/README.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The 8 James Brennan / IPM jobs run on hand-installed or installer-managed systemd user timers; they are unmetered, uninventoried, and their installers would resurrect the timers on re-run. Story 7.14 moves them onto candybar with ordering preserved (`ipm-metrics` :00/:05… → `ipm-internal-dashboard` +1 min; `james-brennan-nightly-suite` 03:11 → `james-brennan-nightly-fix` 04:23).

**Approach:** Give the untracked units a tracked home and resolve the ipm-observability worktree; patch the four james-brennan installers and the skillex `activity-report install-timer` path so re-installs never re-enable the timers; then drive all 8 through `candybar migrate` (plan --write, commit, cutover, verify) as `systemd`-runner candybars — only the `.timer` units retire, the `.service` units stay untouched.

**Decisions recorded (non-interactive run; no human to ask):**
- The epic AC's "Given Waves 5-6 done" is waived: the orchestrator dispatched waves concurrently; each wave is self-contained per the ledger.
- `activity-report@james-brennan`'s rollback must `enable --now activity-report@james-brennan.timer` **by name**: its FragmentPath is the template (`activity-report@.timer`), so the ledger's default enable-by-path would enable the template, not the instance. Its ledger entry carries explicit disable/rollback commands.
- `sync-jim-dropbox`'s tracked home is delorenj/zshyzsh `systemd/` (its script already lives there); interval-timer phase is not preserved (accepted deviation, jitter dropped).
- ipm-observability's tracked home is delorenj/DeLoContainers: units at `systemd/user/ipm-metrics.{timer,service}`, payload at `stacks/monitoring/ipm-observability/`, install script that copies but does NOT enable the timer. The worktree branch (0 commits ahead of origin/main) gets its uncommitted work committed, merged to main, pushed, and the worktree removed.
- skillex gating: `activity_report.schedule.scheduler: "candybar"` in the project's `.project.json` makes `install-timer` write units/drop-in but skip `systemctl enable --now`.
- `sync-jim-dropbox` (exit 2 CLI drift) and `activity-report@james-brennan` (exit 2 after a full successful dry run) are `native_failing: true` — parity, not success, is the bar.

## Boundaries & Constraints

**Always:**
- Ledger-first: every cutover goes through `uv run candybar migrate` against `deploy/migration/ledger.yaml`; commit the ledger after every state change.
- Source-of-truth commit (owning repo, pushed) recorded in `source_of_truth.commit` BEFORE cutover — the CLI refuses without it.
- Cutover order is the CLI's: register disabled → retire native → enable. Never both schedulers live, never neither.
- Runner is `systemd` for all 8: `{kind: systemd, unit: <name>.service}`; units, drop-ins (`schedule.conf`, `dry.conf`), hardening and alerting stay exactly as installed.
- Cron + tz from the inventory verbatim: `*/5` / `1-59/5` / `17 6` / `*/10` (UTC) / `30 3` / `11 3` / `0 3` / `23 4`, all `America/New_York` except sync-jim-dropbox (UTC).
- Stage only own files in every repo (shared dirty trees: skillex, all-skills, zshyzsh, docker, james-brennan); `git pull --rebase origin main` before every push; commits end with the Claude Code attribution line.

**Never:**
- Never `candybar run` the three external-side-effect jobs (nightly-suite dispatches CI, daily-update publishes portal rows, nightly-fix hands runs to navi-pm) — first scheduled fire is the proof.
- Never edit the 8 `.service` units or the activity-report drop-ins as part of migration.
- No daemon code changes, so no container redeploy (other waves run concurrently).
- No new plaintext secrets anywhere (`env.op` files carry `op://` refs only — verified).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Happy cutover | `migrate cutover <id> --wait 600` between fires | candybar registered enabled; timer retired (probe confirms); ledger `cutover` | CLI auto-restores native + leaves candybar disabled |
| Guard window | cutover within min(2m, period/4) of next fire (all the */5 and */10 jobs) | refused, exit 1; `--wait` polls until the window opens | n/a |
| Native service running | sync-jim-dropbox mid-run (15min Timeout) | cutover refused while the unit is active | retry after it exits |
| Parity verify | sync-jim-dropbox / activity-report first (manual or scheduled) run exits 2 | `migrate verify` passes on `native_failing` parity | a success or a different failure mode fails verify |
| Template-instance rollback | `migrate rollback systemd:activity-report@james-brennan.timer` | explicit commands enable the instance by name; template file untouched | daemon down → `disable_pending`, exit 3, re-run finishes |
| Installer re-run after cutover | any patched installer / `activity-report install-timer --project james-brennan` | units written/refreshed, timer NOT enabled | n/a |

</frozen-after-approval>

## Code Map

- `deploy/migration/ledger.yaml` + `src/candybar/migration/{ledger,native,ops}.py` -- the 8 new entries and the CLI that drives them; ledger candybar block fields: name, description, tags, schedule, timezone, runner (never `enabled`)
- `src/candybar/registry/manifest.py` (`SLUG_RE`) -- candybar names are kebab-case: `activity-report@james-brennan` registers as `activity-report-james-brennan`
- `src/candybar/executor/systemd_runner.py` -- auto-installs `<unit>.d/50-candybar.conf` env drop-in + holder reference on first start; meters from unit properties after exit
- `~/docker/.worktrees/feat-ipm-call-observability` (delorenj/DeLoContainers) -- uncommitted: `mise.toml` ipm:check task, `scripts/ipm-check.py`, `tests/test_ipm_{check,dashboard}.py`, grafana `dashboards/IPM/` + provisioning change; branch is 0 ahead of origin/main
- `~/.local/share/ipm-observability/{ipm_metrics.py,ipm-observability-requirements.txt,ipm-observability.env.op}` + `~/.config/systemd/user/ipm-metrics.{timer,service}` -- untracked payload + units to commit into DeLoContainers
- `~/code/james-brennan/scripts/install-{billing-ledger,daily-update,nightly-fix}-timer.sh` + `devops/internal-dashboard/install.sh` -- drop the `enable --now` of the four retired timers
- `~/code/skillex/all-skills/activity-report/scripts/ar/{schedule.py,config.py}` + `scripts/tests/test_schedule.py` -- `scheduler: candybar` config gate on the enable step
- `~/code/james-brennan/.project.json` -- set `activity_report.schedule.scheduler: "candybar"`
- `~/.config/zshyzsh/systemd/` (delorenj/zshyzsh) -- tracked home for `sync-jim-dropbox.{timer,service}` + install script that does not enable the timer
- `~/.local/bin/james-brennan-nightly` -- launcher used by both nightly units; unchanged

## Tasks & Acceptance

**Execution:**
- [ ] DeLoContainers -- commit ipm worktree work + tracked home for ipm-metrics units/payload (install script with no `enable --now`); push branch, merge to main, push main, remove worktree -- AC2
- [ ] zshyzsh -- `systemd/sync-jim-dropbox.{timer,service}` + `systemd/install-sync-jim-dropbox.sh` (copies units, does not enable); commit+push -- AC2
- [ ] james-brennan -- patch 4 installers to stop enabling timers; set `.project.json` scheduler flag; commit+push -- AC4
- [ ] skillex (all-skills submodule) -- `scheduler: candybar` gate in `install_timer_cmd` + config validation + test; commit+push submodule, bump pin in skillex, push -- AC3
- [ ] `deploy/migration/ledger.yaml` -- 8 entries (explicit commands for the template instance); `plan --write` each; commit+push -- AC1
- [ ] cutover all 8 via CLI (`--wait` for the */5 jobs); internal healthy jobs get one manual `candybar run` (must succeed); the two native-failing get one manual run (parity exit 2); commit+push ledger -- AC1
- [ ] sprint-status 7-14, spec updates; board CAND-46; commit+push -- bookkeeping

**Acceptance Criteria:**
- Given the live deployment, when all 8 cutovers land, then each job has a ledger entry, runs under candybar with the inventory cron+tz, its timer is disabled, and `systemctl --user list-timers` shows no native fire for it.
- Given the ordering chains, when the night runs, then ipm-internal-dashboard fires one minute after ipm-metrics and nightly-fix after nightly-suite (visible in `/schedule` and invocation rows).
- Given a re-run of any patched installer or `activity-report install-timer --project james-brennan`, when it completes, then the retired timer is not re-enabled.
- Given `sync-jim-dropbox` and `activity-report@james-brennan` fail natively (exit 2), when they run under candybar, then the invocation records the same failure and `migrate verify` accepts it on parity.
- Given the three external-side-effect jobs, when this story reports, then they are cut over and listed as pending-first-fire (never manually run).

## Implementation Notes

- **Foundation live:** `https://candybar.delo.sh/healthz` 200; `candybar-executor.service` active on the host; `deploy-smoke-systemd` canary's scheduled rows have `success` and nonzero MemoryPeak/CPU; timezone and DST scheduling pinned by `fire_times()` tests and live registration (`America/New_York`). All 8 runner `.service` units are installed on the host and none carries `OnFailure=`.
- **Source-of-truth landed:** delorenj/DeLoContainers main `fe111c627fd3a1af525c9765b33f4f5c31cda69e` (ipm-metrics tracked home, collector payload, installer that never enables timer); delorenj/zshyzsh main `2e31426c73a0e54c6f8af7d0bd0e235e81771a38` (sync-jim-dropbox tracked units + installer); AutomaticAI-io/james-brennan main PR #790 (`e4bfb2d21ace40506cd65188e798cea93a5c84c2`, four installers no longer enable their timers) and PR #791 (`f5f90be169adedee0db9cb1cf9991d66a03b70a9`, `.project.json` activity_report.schedule.scheduler=candybar); delorenj/skills main `9bc8d6c08c5ecfdc5bd3bf84e5e9dad35eba69f0` (install-timer gate + test), delorenj/skillex pin `fbdd702924b7447867a9c2aa31d3190346363091`; delorenj/monitoring main `b9bc0df` (IPM dashboard contract test + cross-repo runner). The IPM feature worktree was merged and removed; monitoring dashboard was already present in its extracted repo byte-identical to the worktree version.
- **Cutovers:** all 8 through `candybar migrate plan --write` and `cutover` in one wave. The high-frequency jobs waited for the native jitter guard; no timer disabled by hand. All 8 native timers report disabled, all candybars enabled and native=retired. The ledger `source_of_truth.commit` values point at the commits above. `activity-report@james-brennan`'s explicit rollback enables the *instance by name* (template FragmentPath would otherwise misaddress it).
- **Manual internal verification:** ipm-metrics manual success and first scheduled 17:45Z success (runtime 2760ms, memory 87MB); ipm-internal-dashboard manual success and first scheduled 17:51Z success (runtime 31.0s, memory 84.5MB), proving the +1 minute order after ipm-metrics at 17:50Z; james-brennan-billing-ledger manual success; sync-jim-dropbox manual fail exit 2 (native parity), its first scheduled 17:50Z fail exit 2 metered (34.3s, 21.7MB) and verified by `migrate verify`; activity-report dry-run manual **fail exit 2** after completing both internal and external compose passes, emitting two `bloodbank.project.activity.recorded` dry-run events and skipping portal/verify/retain/durable copy per its unchanged `dry.conf` (runtime 636.3s, memory 501MB). This is native result parity; first scheduled fire remains pending 2026-10-27 07:30Z. The three external jobs were **not** manually run and are pending first scheduled fire on 2026-10-27.
- **Installer re-run proof:** all three james-brennan timer installers, `activity-report install-timer --project james-brennan`, the DeLoContainers ipm installer and the zshyzsh sync installer were re-run after cutover: every timer remained disabled; activity-report's `schedule.conf` and hand-added `dry.conf` were unchanged. internal-dashboard/install.sh has external Cloudflare/AWS/Docker steps, so its disable gate was reviewed statically and not re-run.
- **Tests:** activity-report full suite 256 passed (schedule tests 9/9); DeLoContainers ipm-check tests 4/4; monitoring dashboard contract 15/15 through `heavy python3 scripts/ipm-dashboard-check.py ~/docker/ipm-observability/ipm_metrics.py`; all 8 committed manifests parse and equal their ledger candybar blocks exactly. No daemon code changed, so no container redeploy.
- **Shared-tree concurrency:** other wave agents wrote the same ledger while this wave was running. The ledger CLI locks, but a hand-written append before the CLI acquired its lock was briefly overwritten by a concurrent CLI write; entries were re-appended, `plan --write` ran under the lock, and the plan was pushed before cutovers. Another wave's commits carried the eight cutover statuses to HEAD before a separate cutover commit could be made. No other wave's hunks were staged in this wave's commits.

## Spec Change Log

## Review Triage Log

Inline review layers (no subagent launcher in this session; same deviation as foundation stories): blind-hunter, edge-case, and verification-gap over the wave's own commits/diffs and the live ledger. Final review found no surviving code defect. The following were caught and fixed during implementation:

| # | Finding | Verdict | Evidence / route |
|---|---------|---------|------------------|
| 1 | Template instance `activity-report@james-brennan.timer` default rollback would enable `activity-report@.timer` by FragmentPath, not the instance | medium | Verified by `systemctl show FragmentPath`; ledger entry uses explicit enable-by-instance-name; no native scheduler was toggled before this was in the ledger |
| 2 | IPM worktree lived in DeLoContainers, but its `stacks/monitoring/` target was deleted when monitoring was extracted to its own repo | medium | Merge conflict exposed it. Resolved: collector, checks, tracked units in DeLoContainers main; dashboard already byte-identical in delorenj/monitoring; dashboard test and explicit cross-repo runner landed there. Merge kept only the `mise.toml` task in DeLoContainers, not a resurrected monitoring tree |
| 3 | Concurrent wave CLI rewrote the shared ledger while this wave's hand-append was still uncommitted | low | The eight entries disappeared once, before any cutover. Re-appended, ran `plan --write` under the ledger lock and pushed. Cutovers were driven by the CLI; all eight entries are in HEAD with correct states. No two schedulers were live |
| 4 | A static test of `install-timer` with `scheduler=candybar` could silently call `enable --now` | low | Added `test_candybar_scheduler_writes_units_but_never_enables`, ran 9 schedule tests and 256 full activity-report tests. A live re-install kept the timer disabled and drop-ins intact |

## Design Notes

- **All 8 are `systemd` runners** (AD-18): the `.service` units keep `Environment`, `op run` wrappers, `TimeoutStartSec`, `Nice`/`IOSchedulingClass`, sandboxing (`ProtectSystem=strict` on billing-ledger) and the activity-report drop-ins (`schedule.conf` + hand-added `dry.conf`, `ACTIVITY_REPORT_DRY=1`). The executor starts them with `systemctl --user start --wait` and meters from unit properties; the runner installs the `50-candybar.conf` env drop-in itself on first start.
- **`activity-report@james-brennan.timer` is a template instance**: `FragmentPath` resolves to `activity-report@.timer`, so the ledger's systemd default rollback (enable by resolved path) would enable the template — wrong. Its entry carries explicit `disable: [systemctl --user disable --now activity-report@james-brennan.timer]` / `rollback: [systemctl --user enable --now activity-report@james-brennan.timer]`.
- **Deviations (recorded per-entry):** `ipm-metrics` AccuracySec=1s dropped; `ipm-internal-dashboard` AccuracySec=10s dropped and its native :30s offset becomes +1 cron minute (inventory-sanctioned); `sync-jim-dropbox` loses interval semantics and 2min jitter — cron `*/10` fixes the phase to :00/:10/…; nightly/daily timers' AccuracySec=1min dropped.

## Verification

**Commands:**
- `uv run candybar migrate status --wave 7.14` -- all 8 entries reach `cutover` (then `verified` as fires land)
- `systemctl --user list-timers --all | grep -Ei 'ipm|james|jim-dropbox|activity'` -- no NEXT elapse for retired timers
- `curl -s https://candybar.delo.sh/candybars` -- 8 new candybars, correct timezone each
- `uv run candybar run <name>` (internal jobs only) -- exit 0 and a `success` invocation row (parity exit 2 for the two native-failing)
- `bash ~/code/james-brennan/scripts/install-billing-ledger-timer.sh` (etc.) -- completes without enabling the timer

**Manual checks:**
- `/schedule` rows for the two ordering chains show the +1min / 03:11→04:23 ordering in UTC-correct instants (EDT on 2026-10-26; after the 2026-11-01 fall-back, EST shifts both chains together).
