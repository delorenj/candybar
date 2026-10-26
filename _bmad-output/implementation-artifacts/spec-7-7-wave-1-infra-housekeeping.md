---
title: 'Story 7.7: Wave 1 — infra housekeeping (10 jobs)'
type: 'chore'
created: '2026-10-26'
status: 'in-progress'
route: 'dispatch'
baseline_commit: '8576544b215cbf3d0b98df755e4ace6d226301ad'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The ten delorenj/infra host-housekeeping systemd user timers (cache-prune ×3, docker-prune, state-maintain, user-logrotate, zellij-ark, gnome-shell-restart, srvls-metrics, srvls-snapshot) still run on their own timers; epic 7 moves every migrate-classified job onto candybar, wave 1 being the lowest-risk internal-only set that proves the systemd runner in production.

**Approach:** For each job, register it as a `systemd`-runner candybar with the inventory's cron + timezone via the committed migration ledger and `candybar migrate` (plan → cutover → verify); retire only the `.timer` (the `.service`, its OnFailure alerting and hardening stay); commit the source-of-truth change in the owning repo first so no re-install resurrects a timer. `srvls` learns to read the candybar registry API so its inventory does not under-report.

## Boundaries & Constraints

**Always:**
- Drive every cutover through `candybar migrate {plan,cutover,verify,rollback}` against `deploy/migration/ledger.yaml`; commit and push the ledger after each mutation batch. Never leave a job on two schedulers or on none.
- Preserve wall-clock semantics: crons and IANA zones exactly as the inventory records (jitter and interval-timer phase are dropped, recorded as `deviations`; `OnBootSec` starts dropped, deviation noted).
- Commit+push the owning repo (delorenj/infra, delorenj/srvls) BEFORE cutover and record the sha in `source_of_truth.commit` (the CLI refuses otherwise).
- Untracked units (gnome-shell-restart, srvls-metrics, srvls-snapshot) get a tracked home in `~/code/infra/systemd/user/` first; live files become symlinks like the other infra units.
- Internal/no-side-effect jobs get ONE manual `candybar run` after cutover and must succeed; scheduled-fire verification via `candybar migrate verify` as fires occur (fast jobs this session, slow ones pending).

**Never:**
- No daemon code changes in this story → no container redeploy, no executor-unit restart beyond what 7.2-7.6 already installed.
- Never edit a `.service` ExecStart; never add a runner `on_failure` hook to a unit that already has `OnFailure=` (the CLI blocks it).
- Never hand-roll registration SQL or toggle timers outside the ledger; never `candybar run` an external-side-effect job (none in this wave).
- No `.bak` files, no plaintext secrets.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| cutover guard | `*/2` zellij-ark | guard = period/4 = 30s; `--wait` polls to a window | refusal exit 1 → retry with `--wait 120` |
| interval timer | zellij-ark, srvls-metrics (OnUnitActiveSec) | cron `*/2` / `*/5` UTC; phase and boot start dropped, deviation recorded | n/a |
| untracked unit | gnome-shell-restart/srvls-* plain files in ~/.config/systemd/user | moved to infra repo, live path becomes symlink, daemon-reload, timer keeps running natively until its cutover | systemd resolves the symlink chain |
| overlap | unit active at fire | runner refuses start; skipped/failure recorded, no double run | AD-20 path |
| rollback demo | zellij-ark after cutover | `migrate rollback` restores timer, one native fire observed, then re-cutover | rollback restores native first, works with daemon down |
| metering | systemd-runner runs | MemoryPeak/CPUUsageNSec read via unit holder drop-in (proven by canary) | missing peak → verify warns |

</frozen-after-approval>

## Code Map

- `deploy/migration/ledger.yaml` + `deploy/migration/README.md` — the committed ledger and its schema; this story adds the first 10 entries.
- `src/candybar/migration/{ops,ledger,native,client}.py`, `src/candybar/cli/migrate.py` — the migrate CLI; do not modify. `plan --write` fills disable/rollback + resolved unit_file; cutover = register disabled → retire native → enable; verify = first scheduled/misfire invocation after cutover_at.
- `src/candybar/executor/systemd_runner.py` — the systemd runner auto-installs `<unit>.d/50-candybar.conf` (AD-8 env) and the holder reference; no per-job plumbing needed.
- `~/code/infra/systemd/user/` — tracked unit files, hand-symlinked into `~/.config/systemd/user/`; no installer script exists, so the source-of-truth change is a retired notice header in each `.timer` (keep `[Install]` so path-based rollback `enable --now <path>` still works) plus importing the 3 untracked units.
- `~/code/srvls/srvls` — single-file Python inventory; add `collect_candybar()` (urllib GET `$CANDYBAR_API_URL`/candybars, default `http://127.0.0.1:8700`, 5s timeout, silent on unreachable) into `collect_all()`; type `candybar`. README lines ~117-145 hold the hand-copy unit snippet → point at infra's tracked files.
- `_bmad-output/planning-artifacts/cron-inventory.md` — the authoritative per-job record (ids verbatim).
- Foundation, confirmed live 2026-10-26: timezone column (7.1), host executor `candybar-executor.service` active (7.2), systemd-runner canary `deploy-smoke-systemd` succeeding with metering every 15 min (7.3), ledger CLI (7.5), watchdog green (7.6).

## Tasks & Acceptance

**Execution:**
- [ ] `~/code/infra/systemd/user/*.timer` — retired-notice headers on the 7 tracked wave-1 timers; import gnome-shell-restart/srvls-metrics/srvls-snapshot units; re-link live; commit+push — no re-install can resurrect a retired timer without seeing the notice
- [ ] `~/code/srvls/srvls` + `README.md` — candybar collector + unit-snippet pointer to infra; commit+push — inventory must not under-report after cutover
- [ ] `deploy/migration/ledger.yaml` — 10 entries via `plan --write`; commit+push — the cutover record
- [ ] cutover ×10, manual run ×10, rollback demo on zellij-ark, verify fast jobs — the wave itself
- [ ] `_bmad-output/implementation-artifacts/sprint-status.yaml` + this spec — status updates; commit+push

**Acceptance Criteria:**
- Given stories 7.1-7.6 done, when this wave lands, then every listed job has a ledger entry and runs under candybar with the inventory's cron + tz, its unit file unchanged apart from the runner drop-in, its timer disabled and removed from timers.target.wants
- Given the untracked units, when the wave lands, then they live in delorenj/infra `systemd/user/` and all owning-repo changes are committed and pushed (infra, srvls)
- Given srvls, when the wave lands, then its table/json/prom/md output includes candybar-registry jobs (committed to delorenj/srvls)
- Given a cut-over job, when its first scheduled fire occurs, then a matching invocation row with full metering verifies it; rollback is demonstrated on zellij-ark (restore timer, one native fire, re-cutover)

## Implementation Notes

## Spec Change Log

## Review Triage Log

## Design Notes

Rollback-vs-retirement tension: stripping `[Install]` from a retired `.timer` would block `enable` (good) but also breaks the ledger's path-based rollback. Resolution: keep `[Install]`, add a prominent retired notice, and let `verify`'s native-active-again check catch any resurrection. The 10 jobs and their crons/zones/runners:

| id | candybar | cron | tz | runner unit | jitter dropped |
|---|---|---|---|---|---|
| systemd:cache-prune-codex-tmp.timer | cache-prune-codex-tmp | `17 * * * *` | America/New_York | cache-prune-codex-tmp.service | 3m |
| systemd:cache-prune-daily.timer | cache-prune-daily | `23 5 * * *` | America/New_York | cache-prune-daily.service | 10m |
| systemd:cache-prune.timer | cache-prune | `7 5 * * 0` | America/New_York | cache-prune.service | 15m |
| systemd:docker-prune.timer | docker-prune | `41 5 * * *` | America/New_York | docker-prune.service | 10m |
| systemd:state-maintain | state-maintain | `40 5 * * *` | America/New_York | state-maintain.service | 15m |
| systemd:user-logrotate | user-logrotate | `17 5 * * *` | America/New_York | user-logrotate.service | 10m |
| systemd:zellij-ark | zellij-ark | `*/2 * * * *` | UTC | zellij-ark-adopt.service | interval; phase/OnBootSec dropped; AccuracySec 15s |
| systemd:gnome-shell-restart.timer | gnome-shell-restart | `0 * * * *` | America/New_York | gnome-shell-restart.service | — |
| systemd:srvls-metrics | srvls-metrics | `*/5 * * * *` | UTC | srvls-metrics.service | 20s; interval phase dropped |
| systemd:srvls-snapshot | srvls-snapshot | `10 4 * * *` | America/New_York | srvls-snapshot.service | — |

## Verification

**Commands:**
- `uv run candybar migrate status --wave 7.7 --probe` — all entries cutover/verified, native=retired
- `systemctl --user list-timers --all | grep -E 'cache-prune|docker-prune|state-maintain|user-logrotate|zellij-ark|gnome-shell|srvls'` — no wave-1 timer listed
- `curl -s http://127.0.0.1:8700/candybars` — 10 new candybars, enabled, correct schedule/timezone
- `~/code/infra/bin/srvls | grep candybar` — registry jobs appear in the inventory
- `git unpushed` — clean for candybar, infra, srvls
