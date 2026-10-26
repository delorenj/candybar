---
title: 'Story 7.9: Wave 3 — backups and vault'
type: 'chore'
created: '2026-10-26'
status: 'in-review'
route: 'dispatch'
baseline_commit: 'c7034585f9b69c8851476a2bc1566128f77771e8'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The backup/vault chain (8 jobs: forever-ago local snapshot, three restic offsite jobs, the monthly restore drill, the ifvault nightly, the DeLoDocs inbox cleanup, and the crontab vault-to-Drive sync) still runs on systemd user timers and one crontab line — outside candybar's registry, metering and skip semantics, and jittered by `RandomizedDelaySec`.

**Approach:** Migrate all 8 per AD-21: ledger entry per job, systemd jobs as `systemd`-runner candybars (`.service` keeps every directive incl. `OnFailure=`; only the `.timer` retires), `crontab:22` as an `exec`-runner candybar (`timeout_s` 7200 ≥ the script's 101m self-cap; default overlap=skip), source-of-truth changes committed and pushed first, then `candybar migrate cutover`, never both schedulers live and never neither.

## Boundaries & Constraints

**Always:**
- Cron + IANA tz from the inventory, evaluated in America/New_York (AD-16); jitter dropped and recorded as an accepted deviation; `Persistent=true` catch-up maps to candybar misfire semantics (deviation).
- Source of truth (installer / repo unit file / vault copy) stops re-enabling the old scheduler, committed and pushed, with the commit recorded in `source_of_truth.commit` BEFORE cutover (the CLI refuses without it).
- `restic-dbdumps` and `ifvault-nightly` already fail natively (Drive quota/rclone) → `native_failing: true`, verify on result parity per AD-21 — never roll back for the pre-existing failure.
- `crontab:22` (external side effects: rclone sync deletes remote files) is never `candybar run` to test; first scheduled fire is the proof.
- Internal-side-effect systemd jobs get one manual `candybar run` smoke after cutover; ledger `verify` still waits for the first scheduled fire.
- The stale disabled `sync-vault.{timer,service}` is deleted at cutover so no third scheduler can be re-enabled; skillex `folder-curator/references/drive-sync.md` stops citing it.
- Ordering chain preserved by cron minutes: forever-ago 03:00 → restic-vault 04:30 → backup-drill (monthly 1st 09:00); anacron dump ~07:35 (excluded) → restic-dbdumps 09:15.

**Never:**
- No candybar `on_failure` on systemd-runner jobs whose unit has `OnFailure=` (the CLI refuses; the unit alerts as today).
- No hand-rolled registration SQL; only `candybar migrate`.
- No daemon code changes (no redeploy; other waves run concurrently) — the only code is the wave-3 DST test.
- No edits to the forever-ago README's generic user template (it cannot resurrect THIS machine's job — its paths are placeholders).
- Never restart hermes-gateway; not touched in this wave.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Daily systemd job | cutover at 13:xx EDT, next fire 0x:00 tomorrow | candybar fires at the cron minute in ET; `start --wait` meters from unit properties; OnFailure path intact | cutover refused within the 2-minute guard of the next native fire → retry with `--wait` |
| ifvault fall-back night | `40 1 * * *` America/New_York over 2026-11-01 | fires exactly once, 05:40Z (first occurrence, EDT) | pinned by simulated-clock tick walk |
| milkshake spring-forward | `30 2 * * *` America/New_York over 2027-03-14 | 02:30 does not exist → fires once at 03:00 EDT (07:00Z) | pinned by simulated-clock tick walk |
| inbox-cleanup */10 | cutover between 10-minute fires | first scheduled fire ≤10 min later; verify completes same day | guard = min(2 min, period/4) = 2 min |
| crontab exec job | 100-minute rclone sync | overlap=skip; executor timeout 7200s is a backstop behind the script's own 100m+60s kill | flock moves to $XDG_RUNTIME_DIR (set under the user manager, unlike cron) — recorded deviation |
| native_failing parity | restic-dbdumps / ifvault-nightly first fire fails like native | verify accepts on parity, records the invocation | rollback only if the failure MODE differs (not a Drive/rclone failure) |

</frozen-after-approval>

## Code Map

- `deploy/migration/ledger.yaml` + `deploy/migration/README.md` — the ledger; the `candybar:` block IS the committed manifest (validated as `CandybarManifest`); schema and per-kind default disable/rollback commands in the README.
- `src/candybar/migration/{ledger,ops,native}.py` — entry schema (systemd-timer needs `unit` ending in `.timer`; crontab needs exact `line`), cutover order register-disabled → retire-native → enable, guards, parity verify.
- `src/candybar/executor/systemd_runner.py` — auto-installs `<unit>.d/50-candybar.conf` + holder reference on first use; no manual drop-in work.
- `src/candybar/dispatcher/schedule.py` `matches_tick` — the dispatcher's tick match over `fire_times`; the wave-3 test simulates ticks with it.
- `tests/test_firetimes.py` — already pins the exact AD-16 instants for `40 1 * * *` (05:40Z once) and `30 2 * * *` (07:00Z gap-shift); the new test extends this to a full tick-by-tick dispatcher simulation.
- Live units: `~/.config/systemd/user/{forever-ago-vault,restic-vault,backup-drill}.{timer,service}` + `forever-ago-alert.service` — UNTRACKED, move into `~/docker/systemd/user/` (DeLoContainers) as canonical copies + symlinks, alongside the already-tracked `restic-dbdumps.*` and `milkshake-ledger-backup.*`.
- `~/code/intelliforia/infra/vault/install-nightly.sh` — currently ends in `systemctl --user enable --now ifvault-nightly.timer`; must stop enabling (resurrection vector). Live units match the repo copies (diffed clean).
- `~/code/DeLoDocs/_vault/Workflows/systemd/delodocs-inbox-cleanup.service` — vault copy drifted (missing `--hold-pdfs-in Wax`); DeLoDocs is NOT git → sync the file, fix `Workflows/README.md` install steps, record the diff in the ledger history.
- `~/.config/zshyzsh/scripts/sync-vault-to-drive.sh` (delorenj/zshyzsh, symlinked from `~/.local/bin/`) — header comment cites the cron slot; update to candybar. The crontab line itself is hand-maintained (no installer).
- `~/code/skillex/all-skills/folder-curator/references/drive-sync.md` lines ~108-112 — stale: claims the vault sync runs on both `sync-vault.timer` and cron.
- Crontab line 22: `30 */2 * * * /home/delorenj/.local/bin/sync-vault-to-drive` (verbatim, incl. leading position after the BASH_ENV header — removal by exact line match via the CLI).

## Tasks & Acceptance

**Execution:**
- [ ] `~/docker/systemd/user/` -- add `forever-ago-vault.{service,timer}`, `restic-vault.{service,timer}`, `backup-drill.{service,timer}`, `forever-ago-alert.service` as canonical copies; replace live files with symlinks; `daemon-reload`; commit+push delorenj/DeLoContainers -- untracked units need a tracked home before cutover (stage ONLY these files; the repo has other agents' dirt)
- [ ] `~/code/intelliforia/infra/vault/install-nightly.sh` -- drop `enable --now` of the timer (install units + daemon-reload only, print that scheduling is candybar's); commit+push -- re-install must not resurrect
- [ ] `~/code/DeLoDocs/_vault/Workflows/systemd/delodocs-inbox-cleanup.service` + `Workflows/README.md` -- sync the drifted ExecStart into the vault copy; README install steps stop enabling the timer (candybar schedules it) -- vault is not git; record diff in ledger
- [ ] `~/.config/zshyzsh/scripts/sync-vault-to-drive.sh` -- header comment: scheduled by candybar, not cron; commit+push zshyzsh -- source of truth for crontab:22
- [ ] `~/code/skillex/all-skills/folder-curator/references/drive-sync.md` -- fix the stale "runs on both timer and cron" claim; commit+push skillex
- [ ] `deploy/migration/ledger.yaml` -- 8 entries (wave `7.9`), then `candybar migrate plan --write` each, commit -- the committed manifests
- [ ] `tests/test_wave3_dst.py` -- simulated-clock tick walk of `matches_tick` for `40 1 * * *` and `30 2 * * *` (America/New_York) across both transitions; assert exactly the pinned instants -- the AC's simulated-clock proof
- [ ] Cutover each job via `candybar migrate cutover <id> [--wait]`; manual `candybar run` smoke for the 7 internal systemd jobs (parity-expected failure for restic-dbdumps/ifvault-nightly); `verify` for delodocs-inbox-cleanup (fires ≤10 min later)
- [ ] `_bmad-output/implementation-artifacts/sprint-status.yaml` -- 7-9 to review

**Acceptance Criteria:**
- Given the 8 ledger entries with pushed source-of-truth commits, when `cutover` runs per job, then each timer is disabled (and absent from timers.target.wants), the crontab line is gone, the stale sync-vault units are deleted, and each candybar is registered enabled with the inventory's cron + America/New_York
- Given the cut-over systemd jobs, when each is manually run once, then forever-ago-vault, restic-vault, backup-drill, milkshake-ledger-backup and delodocs-inbox-cleanup record `success` with unit-property metering, and restic-dbdumps / ifvault-nightly record the same failure mode as native (parity)
- Given the two DST schedules, when the simulated-clock walk crosses 2026-11-01 and 2027-03-14, then ifvault's `40 1 * * *` matches exactly tick 05:40Z on Nov 1 and milkshake's `30 2 * * *` matches exactly 07:00Z (03:00 EDT) on Mar 14
- Given no daemon code changed, when the story lands, then no container redeploy happened and /healthz stayed 200 throughout

## Implementation Notes

- 2026-10-26: Foundation live: `/healthz` 200; `deploy-smoke-exec` and `deploy-smoke-systemd` scheduled canaries have fresh successful, metered invocations. DST behavior pinned by `tests/test_wave3_dst.py` (3 passed, ruff clean); `systemd_runner.py` calls NATS `in_progress` every 25s against a 3600s `ack_wait` on long starts (runtime proof is still pending for `restic-dbdumps`).
- Canonical sources pushed first: DeLoContainers `33e58bf8f8df134bde641a8c2be1cb1cc5ae5e2f` (seven previously untracked units with do-not-reenable headers) and `a221e0101a4d608a867a191ad41d9b0de96e26c7` (retirement notes on the already-tracked restic-dbdumps and milkshake timers); live units symlinked into `~/.config/systemd/user/`, `daemon-reload` done. IntelliForia/intelliforia `753b34250359209c9324fde4d47ac8ddba3e2dd9` removes `enable --now` from `install-nightly.sh`. DeLoDocs vault copy (non-git) of the inbox-cleanup service synced to live, gaining `--hold-pdfs-in Wax`; Workflows README no longer enables its timer; exact diff in the ledger history. zshyzsh `d0e7c4e2400b1d2587d50699cf8844f7f948ce42` header records candybar scheduling. delorenj/skills `dfc5deb4504858fa5c4e4f33a542ede07cf5ce52` fixes stale drive-sync doc; skillex submodule pointer `db71277d60c7aaab2c70198daa868b192e0f8dd8` pushed. The rest of those repos' shared dirt was not staged.
- All 8 ledger entries planned and cut over via `candybar migrate` in this order: forever-ago-vault, restic-vault, backup-drill, restic-dbdumps, milkshake-ledger-backup, ifvault-nightly, delodocs-inbox-cleanup, crontab:22. The crontab line was removed and stale disabled sync-vault units deleted by the ledger's exact disable commands. Ledger history is committed upstream (the shared candybar repo's concurrent commits picked up these cutover writes before a separate commit could be made).
- Manual internal/no-side-effect smoke: forever-ago-vault success (1.045s, 383 MB peak); delodocs-inbox-cleanup success (447ms, 10 MB); restic-vault success (99s, 196 MB). restic-dbdumps is still running against Drive rate limits; ifvault-nightly failed exit 1 on `restic forget --prune` (pre-existing rclone timeout/rate problem, but different error), so rolled back immediately. backup-drill failed exit 1 (excluded junk present in archive, offsite `restic check` failed); rolled back immediately. milkshake-ledger-backup failed exit 1 on `restic forget --prune` (backend connection limit <2); rolled back immediately. All three rollback timers are enabled+active again; their candybars disabled. The rollback entries are committed upstream, and the failure modes need a separate fix before re-cutover.
- `crontab:22` is external-side-effect (rclone sync), never manually run. Its first scheduled fire is 2026-10-26T18:30Z (14:30 EDT), pending. `delodocs-inbox-cleanup` fired on the first 17:50Z tick and succeeded (536ms, 10.4 MB peak, invocation `b38a621e-de8e-5ab0-8048-4b0626ad3643`); the CLI `verify` set it to `verified`, pushed in candybar `1774dbd`. The remaining cutover daily jobs await their first fires on 2026-10-27. No daemon code changed, so no container redeploy.

## Spec Change Log

## Review Triage Log

## Verification

**Commands:**
- `uv run pytest tests/test_wave3_dst.py -q` -- all pass
- `uv run ruff check tests/test_wave3_dst.py` -- clean
- `uv run candybar migrate status --wave 7.9` -- every entry cutover/verified, none planned/rolled-back
- `systemctl --user list-timers | grep -cE 'forever-ago-vault|restic-vault|backup-drill|restic-dbdumps|milkshake-ledger|ifvault|inbox-cleanup'` -- 0 (timers retired)
- `crontab -l | grep -c sync-vault-to-drive` -- 0
- `curl -s http://127.0.0.1:8700/candybars` -- 8 new enabled candybars with timezone America/New_York
