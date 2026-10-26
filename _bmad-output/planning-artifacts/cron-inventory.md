---
title: Cron inventory — Epic 7 migration onto Candybar
created: '2026-10-26'
host: big-chungus
local_timezone: America/New_York
dst_note: 'US DST ends Sun 2026-11-01 (01:00-01:59 local repeats); next spring-forward 2027-03-14 (02:00-02:59 local does not exist)'
sources:
  - user crontab (crontab -l), /etc/crontab, /etc/cron.d, /etc/anacrontab + /etc/cron.{daily,weekly,monthly,yearly}
  - systemctl --user list-timers --all + unit files under ~/.config/systemd/user, /etc/systemd/user, /usr/lib/systemd/user
  - Hermes cron jobs.json for every profile under ~/.hermes/profiles/*/cron
consumers:
  - architecture addendum (ARCHITECTURE-SPINE.md, AD-16..AD-22)
  - epics.md Epic 7 (stories 7.1-7.17)
---

# Cron inventory — Epic 7 "Cron migration onto Candybar"

Every scheduled job found on big-chungus on 2026-10-26, classified with the orchestrator's
migration policy. Ids are stable and are used verbatim by the Epic 7 wave stories and the
migration ledger (`deploy/migration/ledger.yaml`, Story 7.5).

## Migration policy (as applied)

- **EXCLUDE** (stay on their current scheduler, reason recorded):
  - **(a)** OS/vendor-owned: `snap.*`, `ubuntu-*`, `systemd-tmpfiles-clean`, `launchpadlib-cache-clean`, `/etc/cron.d` entries, anacron vendor scripts.
  - **(b)** Independent liveness guards: anything that detects or recovers an outage of something Candybar itself depends on (docker, NATS, Postgres, the candybar container, hermes-gateway watchdog, deadman switches). A scheduler cannot be its own watchdog.
  - **(c)** Sub-minute cadence (Candybar's resolution is one minute).
  - **(d)** Dead/broken entries and paused or finished Hermes jobs: reported, not migrated.
- **MIGRATE** everything else, including Hermes agent-mode jobs and jobs with external side effects.
- Wall-clock semantics are preserved: systemd `OnCalendar` without an explicit zone and user cron are evaluated in `America/New_York`; relative timers (`OnUnitActiveSec`, Hermes `every Nm`) are timezone-invariant intervals and map to UTC.
- Never manually `candybar run` a job with external side effects to test it; its first scheduled fire is the proof.

## Schedule translation rules

| Native construct | Candybar translation | Notes |
| --- | --- | --- |
| `OnCalendar=` (no zone) / user cron line | cron as written, `timezone: America/New_York` | systemd and cron both evaluate local wall time |
| `OnCalendar=... America/New_York` | same, explicit zone | |
| `OnUnitActiveSec=N` / `OnBootSec` / Hermes `every Nm` | `*/N` minute (or hourly/2-hourly) cron, `timezone: UTC` | phase is not preserved; only the interval matters |
| `RandomizedDelaySec=` | dropped (fires at the exact minute) | accepted deviation, recorded per job in the ledger |
| `Persistent=true` | AD-6 misfire coalesce-to-latest (24h cap) | equivalent catch-up after downtime |
| `Persistent=false` | AD-6 still runs the latest missed fire | accepted deviation, recorded per job |
| `OnFailure=` / hardening / `EnvironmentFile=` / `TimeoutStartSec=` | kept: the `.service` still runs under systemd (systemd runner, AD-18) | only the `.timer` is retired |
| Hermes agent job | Hermes runner (`hermes -p <profile> cron run --wait`, AD-19) | delivery, sessions, `context_from`, monitor gates stay in Hermes |
| Hermes no-agent script | exec runner on the script directly (AD-18) | `failure_deliver` reproduced by the candybar `on_failure` hook |

## Summary

| Classification | Count |
| --- | --- |
| migrate | 70 |
| exclude (a) OS/vendor | 20 (incl. `ubuntu-2604-reminder`, also (d)) |
| exclude (b) liveness guard | 7 |
| exclude (c) sub-minute | 1 |
| exclude (d) dead/disabled/paused/finished | 19 |
| **total** | **117** |

Waves (Epic 7): 7.7 infra housekeeping (10) · 7.8 33GOD platform sweeps (7) · 7.9 backups and vault (8) · 7.10 Hindsight (4) · 7.11 product pollers (4) · 7.12 IntelliForia (4) · 7.13 AutomaticAI gateway (9) · 7.14 James Brennan / IPM (8) · 7.15 Dev Journal and reporting (5) · 7.16 personal LLM jobs (5) · 7.17 Hermes agent jobs (6).

Runner legend: **S** = systemd runner (start the existing `.service`), **E** = exec runner (argv on the host), **H** = Hermes runner.
Side-effect legend: **int** = internal only, **ext** = external side effects, **LLM** = invokes a model.

## Jobs to migrate (70)

| id | source | native → cron (tz) | owner repo / source of truth | effects | runner | wave |
| --- | --- | --- | --- | --- | --- | --- |
| `systemd:cache-prune-codex-tmp.timer` | systemd | `*-*-* *:17:00` +3m jitter → `17 * * * *` (America/New_York) | delorenj/infra `systemd/user/cache-prune-codex-tmp.{timer,service}`, symlinked | int | S | 7.7 |
| `systemd:cache-prune-daily.timer` | systemd | `05:23` +10m → `23 5 * * *` (America/New_York) | delorenj/infra `systemd/user/cache-prune-daily.*`, symlinked; OnFailure=cache-prune-alert | int | S | 7.7 |
| `systemd:cache-prune.timer` | systemd | `Sun 05:07` +15m → `7 5 * * 0` (America/New_York) | delorenj/infra `systemd/user/cache-prune.*`, symlinked | int | S | 7.7 |
| `systemd:docker-prune.timer` | systemd | `05:41` +10m → `41 5 * * *` (America/New_York) | delorenj/infra `systemd/user/docker-prune.*`, symlinked | int | S | 7.7 |
| `systemd:state-maintain` | systemd | `05:40` +15m → `40 5 * * *` (America/New_York) | delorenj/infra `systemd/user/state-maintain.*` (+alert), symlinked by hand, no installer | int | S | 7.7 |
| `systemd:user-logrotate` | systemd | `05:17` +10m → `17 5 * * *` (America/New_York) | delorenj/infra `systemd/user/user-logrotate.*` (+alert), symlinked by hand | int | S | 7.7 |
| `systemd:zellij-ark` | systemd | `OnUnitActiveSec=2min` → `*/2 * * * *` (UTC) | delorenj/infra `systemd/user/zellij-ark.timer` + `zellij-ark-adopt.service`, symlinked by hand | int | S | 7.7 |
| `systemd:gnome-shell-restart.timer` | systemd | `hourly` → `0 * * * *` (America/New_York) | none: hand-written `~/.config/systemd/user/gnome-shell-restart.*` (needs a tracked home in delorenj/infra) | int | S | 7.7 |
| `systemd:srvls-metrics` | systemd | `OnUnitActiveSec=5min` → `*/5 * * * *` (UTC) | delorenj/srvls script; unit hand-copied from srvls README, untracked | int | S | 7.7 |
| `systemd:srvls-snapshot` | systemd | `04:10` → `10 4 * * *` (America/New_York) | delorenj/srvls script, output in delorenj/infra; unit untracked | int | S | 7.7 |
| `systemd:bloodbank-agent-hooks-health.timer` | systemd | `OnUnitActiveSec=5min` → `*/5 * * * *` (UTC) | delorenj/bloodbank `services/agent-hooks/ops/systemd/install-timer.sh` | int | S | 7.8 |
| `systemd:candystore-project-sync.timer` | systemd | `OnUnitActiveSec=1h` → `5 * * * *` (UTC) | delorenj/candystore `ops/systemd/*`, `mise run project:sync:install` | int | S | 7.8 |
| `systemd:holocene-pad-roster.timer` | systemd | `OnUnitActiveSec=2min` → `*/2 * * * *` (UTC) | delorenj/holocene `services/pad-roster/*.{timer,service}`, symlinked | int | S | 7.8 |
| `systemd:hermes-single-install-guard.timer` | systemd | `OnUnitActiveSec=15min` → `*/15 * * * *` (UTC) | delorenj/hermes-agent-template `scripts/systemd/*` (mirror in flume templates) | int | S | 7.8 |
| `systemd:skillex-hermes-resync` | systemd | `*:07,22,37,52` +30s → `7,22,37,52 * * * *` (America/New_York) | delorenj/skillex `scripts/install-hermes-resync.sh` (must stop enabling the timer; the `.path` trigger stays) | int | S | 7.8 |
| `systemd:naipkins-registry-snapshot` | systemd | `*:0/5` +30s → `*/5 * * * *` (America/New_York) | delorenj/naipkins `systemd/user/*`, README lines 165-170 enable step | int | S | 7.8 |
| `systemd:delonet-n8n-workflows-sync.timer` | systemd | `OnUnitActiveSec=15min` → `*/15 * * * *` (UTC) | delorenj/delonet-n8n-workflows `systemd/*`, symlinked | int (git push) | S | 7.8 |
| `systemd:forever-ago-vault.timer` | systemd | `03:00` +15m → `0 3 * * *` (America/New_York) | none: hand-written units (needs tracked home, e.g. DeLoContainers `systemd/user`); README template in delorenj/forever-ago | int | S | 7.9 |
| `systemd:restic-vault` | systemd | `04:30` +20m → `30 4 * * *` (America/New_York); must follow forever-ago-vault | none: hand-written units (needs tracked home next to restic-dbdumps) | int | S | 7.9 |
| `systemd:backup-drill.timer` | systemd | `*-*-01 09:00` +1h → `0 9 1 * *` (America/New_York) | none: hand-written units + `~/.local/bin/backup-drill` | int | S | 7.9 |
| `systemd:restic-dbdumps` | systemd | `09:15` +10m → `15 9 * * *` (America/New_York); after cron.daily db-backup | delorenj/DeLoContainers `systemd/user/restic-dbdumps.*`, symlinked, no installer. PRE-EXISTING FAILURE (Drive 500s) | int | S | 7.9 |
| `systemd:milkshake-ledger-backup.timer` | systemd | `02:30` +10m → `30 2 * * *` (America/New_York); in the 2027-03-14 spring-forward gap | delorenj/DeLoContainers `systemd/user/milkshake-ledger-backup.*`, symlinked | int | S | 7.9 |
| `systemd:ifvault-nightly.timer` | systemd | `01:40 America/New_York` +10m → `40 1 * * *` (America/New_York); in the 2026-11-01 repeated hour | IntelliForia/intelliforia `infra/vault/systemd/*`, `mise run vault:backup:install`. PRE-EXISTING FAILURE (rclone) | int | S | 7.9 |
| `systemd:delodocs-inbox-cleanup.timer` | systemd | `*:0/10` → `*/10 * * * *` (America/New_York) | DeLoDocs vault (not git) `_vault/Workflows/systemd/*`; live unit drifted (+`--hold-pdfs-in Wax`) | int | S | 7.9 |
| `crontab:22` | crontab | `30 */2 * * *` user cron → `30 */2 * * *` (America/New_York) | delorenj/zshyzsh `scripts/sync-vault-to-drive.sh`; schedule = hand-edited crontab line 22; stale disabled `sync-vault.timer` duplicate; stale skillex `folder-curator/references/drive-sync.md` | ext (rclone to gdrive) | E (timeout ≥ 101m) | 7.9 |
| `systemd:hindsight-capture-sweep.timer` | systemd | `OnUnitActiveSec=10min` → `*/10 * * * *` (UTC) | delorenj/bloodbank `services/hook-hub/systemd/*`, symlinked (hook-hub README) | int, LLM (server-side extraction) | S | 7.10 |
| `systemd:hindsight-write-probe.timer` | systemd | `OnUnitActiveSec=1h` → `4 * * * *` (UTC) | delorenj/agents (`~/.agents`) `hooks/hindsight/systemd/*`, symlinked; watched by infra-doctor 2h deadman | int, LLM | S | 7.10 |
| `systemd:hindsight-memory-analysis.timer` | systemd | `05:00` +15m → `0 5 * * *` (America/New_York) | delorenj/DeLoContainers `stacks/ai/hindsight/systemd/*`, symlinked | int, LLM | S | 7.10 |
| `systemd:hindsight-template-sweep.timer` | systemd | `04:30` +30m → `30 4 * * *` (America/New_York) | delorenj/DeLoContainers `stacks/ai/hindsight/systemd/*`, symlinked. PRE-EXISTING FAILURE (op run + psql) | int, LLM | S | 7.10 |
| `systemd:telnyx-s3-intake` | systemd | `*:*:00` +5s → `* * * * *` (America/New_York) | delorenj/HeyMa `components/telnyx-s3-intake/deploy/*`, `install -m 644` per README:116-121; single-instance | int | S | 7.11 |
| `systemd:gruvato-acoustid-check.timer` | systemd | `00/3:07` +2m → `7 */3 * * *` (America/New_York) | delorenj/gruvato `deploy/gruvato-acoustid-check.*` (+alert), symlinked | int | S | 7.11 |
| `systemd:mobile-deploy-hub-catchup.timer` | systemd | `*:0/2` → `*/2 * * * *` (America/New_York) | delorenj/mobile-deploy-hub `tools/catchup/units/install.sh` (`enable --now`, re-run by `tools/hosts.mjs` on EVERY hub release) | ext (installs builds on device) | S | 7.11 |
| `systemd:mobile-deploy-hub-macwatch.timer` | systemd | `*:1/2`, Persistent=false → `1-59/2 * * * *` (America/New_York) | delorenj/mobile-deploy-hub `tools/catchup/units/install.sh` | ext (cancels CI jobs) | S | 7.11 |
| `systemd:intelliforia-pull-prod.timer` | systemd | `04:15` +15m → `15 4 * * *` (America/New_York) | IntelliForia/intelliforia scripts; unit hand-maintained in `~/.config/systemd/user` (needs tracked home in intelliforia `infra/`) | ext (staging droplet refresh) | S | 7.12 |
| `systemd:intelliforia-client-health.timer` | systemd | `05:30` +5m → `30 5 * * *` (America/New_York); waits on pull-prod | IntelliForia/intelliforia `infra/client-health/install.sh` | int | S | 7.12 |
| `systemd:intelliforia-client-health-monday.timer` | systemd | `Mon 08:30 America/New_York`, Persistent=false → `30 8 * * 1` (America/New_York) | IntelliForia/intelliforia `infra/client-health/install.sh` | int | S | 7.12 |
| `systemd:intelliforia-staging-track-main.timer` | systemd | `Mon..Fri 07:05` → `5 7 * * 1-5` (America/New_York) | IntelliForia/intelliforia `infra/staging/systemd/*` (manual copy) | ext (ships to staging) | S | 7.12 |
| `systemd:newapi-allowances` | systemd | `OnUnitActiveSec=5m` → `*/5 * * * *` (UTC) | AutomaticAI-io/client-portal `gateway/ops/systemd/*`, `gateway/ops/install-systemd.sh` | int | S | 7.13 |
| `systemd:newapi-vm-health` | systemd | `OnUnitActiveSec=5m` → `*/5 * * * *` (UTC); SuccessExitStatus=1 | AutomaticAI-io/client-portal `gateway/ops/install-systemd.sh` | ext (ntfy) | S | 7.13 |
| `systemd:newapi-db-pull` | systemd | `08:05` +10m → `5 8 * * *` (America/New_York); after the 07:35 dump | AutomaticAI-io/client-portal `gateway/ops/install-systemd.sh` | int | S | 7.13 |
| `systemd:newapi-claude-token` | systemd | `OnUnitActiveSec=30min` → `*/30 * * * *` (UTC) | AutomaticAI-io/client-portal; enable step at `gateway/ops/README.md:374`. PRE-EXISTING FAILURE (219/219 exit 1) | ext (prod gateway channel) | S | 7.13 |
| `systemd:newapi-oauth-connections` | systemd | `OnUnitActiveSec=5m` → `*/5 * * * *` (UTC) | AutomaticAI-io/client-portal `gateway/ops/install-systemd.sh` | ext (1Password + prod channels) | S | 7.13 |
| `systemd:newapi-openrouter-sync` | systemd | `03:47` +10m → `47 3 * * *` (America/New_York) | AutomaticAI-io/client-portal; `gateway/ops/OPENROUTER-PASSTHROUGH.md:171-174` enable step | ext (live billing prices) | S | 7.13 |
| `systemd:newapi-staff-sso` | systemd | `OnUnitActiveSec=1h` → `7 * * * *` (UTC) | AutomaticAI-io/client-portal; enable step at `gateway/ops/README.md:374` | ext (prod staff rows) | S | 7.13 |
| `systemd:newapi-token-watch` | systemd | `08:30` +10m → `30 8 * * *` (America/New_York) | AutomaticAI-io/client-portal; enable step at `gateway/ops/README.md:356` | ext (GitHub secrets, vault) | S | 7.13 |
| `systemd:aai-free-scout.timer` | systemd | `04:37` +10m → `37 4 * * *` (America/New_York) | AutomaticAI-io/client-portal `gateway/ops/systemd/aai-free-scout.*`, symlinked | ext, LLM (signups, deploys gateway) | S | 7.13 |
| `systemd:ipm-metrics.timer` | systemd | `*:0/5:00` → `*/5 * * * *` (America/New_York) | none committed: hand-installed units + `~/.local/share/ipm-observability/` (uncommitted work in `~/docker/.worktrees/feat-ipm-call-observability`); needs tracked home | int | S | 7.14 |
| `systemd:ipm-internal-dashboard.timer` | systemd | `*:0/5:30` → `1-59/5 * * * *` (America/New_York), +1 min keeps it after ipm-metrics | AutomaticAI-io/james-brennan `devops/internal-dashboard/install.sh` | int | S | 7.14 |
| `systemd:james-brennan-billing-ledger.timer` | systemd | `06:17` → `17 6 * * *` (America/New_York) | AutomaticAI-io/james-brennan `scripts/install-billing-ledger-timer.sh` | int | S | 7.14 |
| `systemd:sync-jim-dropbox` | systemd | `OnUnitActiveSec=10min` → `*/10 * * * *` (UTC) | delorenj/zshyzsh script; unit untracked (needs tracked home). PRE-EXISTING FAILURE (exit 2, folder-curator CLI drift) | int | S | 7.14 |
| `systemd:activity-report@james-brennan.timer` | systemd | `03:30 America/New_York` (drop-in) → `30 3 * * *` (America/New_York) | delorenj/skillex `all-skills/activity-report` (`activity-report install-timer`); schedule from james-brennan `.project.json`; hand-added `dry.conf` | int, LLM | S | 7.14 |
| `systemd:james-brennan-nightly-suite.timer` | systemd | `03:11 America/New_York` → `11 3 * * *` (America/New_York) | AutomaticAI-io/james-brennan `scripts/install-nightly-fix-timer.sh` | ext (dispatches CI) | S | 7.14 |
| `systemd:james-brennan-daily-update.timer` | systemd | `03:00` → `0 3 * * *` (America/New_York) | AutomaticAI-io/james-brennan `scripts/install-daily-update-timer.sh` | ext, LLM (client portal rows) | S | 7.14 |
| `systemd:james-brennan-nightly-fix.timer` | systemd | `04:23 America/New_York` → `23 4 * * *` (America/New_York); after nightly-suite | AutomaticAI-io/james-brennan `scripts/install-nightly-fix-timer.sh` | ext, LLM (navi-pm fix agent) | S | 7.14 |
| `hermes:33god-pm:8da3f24263f9` | hermes | `0 6 * * *` (profile tz) → `0 6 * * *` (America/New_York) | delorenj/infra `scripts/dev-journal-hermes-nightly.sh` + skillex `delonet-daily-report`; job record in 33GOD PM runtime `cron/jobs.json` (gitignored); wrapper unversioned | ext, LLM (vault, S3, Slack) | E (no-agent script) | 7.15 |
| `systemd:dev-journal-ensure-nightly.timer` | systemd | `06:15 America/New_York` → `15 6 * * *` (America/New_York) | delorenj/infra `systemd/user/dev-journal-ensure-nightly.*`, symlinked | ext, LLM | S | 7.15 |
| `systemd:dev-journal-reconcile.timer` | systemd | `hourly` → `0 * * * *` (America/New_York) | delorenj/infra `systemd/user/dev-journal-reconcile.*`, symlinked. PRE-EXISTING FAILURE (exit 3) | int | S | 7.15 |
| `systemd:sync-daily-reports-to-s3` | systemd | `06:30 America/New_York` → `30 6 * * *` (America/New_York) | delorenj/infra `systemd/user/sync-daily-reports-to-s3.*`, symlinked by hand | int | S | 7.15 |
| `hermes:33god-pm:9b5dadbd1e4d` | hermes | `*/10 * * * *` (profile tz) → `*/10 * * * *` (America/New_York); script needs `TZ=America/New_York` | delorenj/agents `tools/jev-clairic` (snapshot `hermes/cron-jobs.json` + `install.sh`); job record in 33GOD PM runtime | int (failure_deliver telegram) | E (no-agent script) | 7.15 |
| `systemd:tolt-forge-daily` | systemd | `16:00` +60s → `0 16 * * *` (America/New_York) | delorenj/tower-of-lost-things `foundry/cron/tolt-forge-daily.*`, `systemctl --user link` (ops/README.md:167). PRE-EXISTING FAILURE (weekly limit) | ext, LLM (lands to main) | S | 7.16 |
| `systemd:tolt-items-daily` | systemd | `03:00` +120s → `0 3 * * *` (America/New_York) | delorenj/tower-of-lost-things `foundry/cron/tolt-items-daily.*`; ops/README.md:167 instruction must change. PRE-EXISTING FAILURE (2/3) | ext, LLM (fal spend, push) | S | 7.16 |
| `systemd:pr-crusher` | systemd | `03:00` +15m → `0 3 * * *` (America/New_York) | delorenj/pr-crusher `config/systemd/*`, `scripts/install.sh` line 89 `enable --now`. PRE-EXISTING FAILURE (0/5) | ext, LLM (PR comments) | S | 7.16 |
| `systemd:honda-hunt-daily-check.timer` | systemd | `09:00 America/New_York` → `0 9 * * *` (America/New_York) | DeLoDocs vault (not git) `Projects/project-fuckudeer/scripts/systemd/*`. PRE-EXISTING FAILURE (codex token) | int, LLM | S | 7.16 |
| `systemd:delodocs-wiki-self-tuning-weekly.timer` | systemd | `Mon 08:45` → `45 8 * * 1` (America/New_York) | DeLoDocs vault (not git) `_vault/Workflows/systemd/*`. PRE-EXISTING FAILURE (hermes timeout) | int, LLM | S | 7.16 |
| `hermes:33god-pm:777c511a009c` | hermes | `*/5 * * * *` (profile tz) → `*/5 * * * *` (America/New_York) | job record 33GOD PM runtime `cron/jobs.json`; snapshot delorenj/agents `tools/jev-clairic/hermes/cron-jobs.json`; deadman = excluded crank-watchdog | ext, LLM (send-keys into panes) | H | 7.17 |
| `hermes:33god-pm:f3db72089593` | hermes | `every 120m` → `0 */2 * * *` (UTC) | job record 33GOD PM runtime (gitignored); monitor script unversioned | ext, LLM (commit+push, dispatch) | H | 7.17 |
| `hermes:delodocs-pm:fc39bd8b33c8` | hermes | `0 9 * * *` (profile tz) → `0 9 * * *` (America/New_York) | DeLoDocs `agents/hermes/pm/runtime/cron/jobs.json` (not git) | int, LLM | H | 7.17 |
| `hermes:dumply:8aecd027dbd0` | hermes | `30 20 * * *` (profile tz) → `30 20 * * *` (America/New_York) | `~/.hermes/profiles/dumply/cron/jobs.json` (unversioned) | ext, LLM (Telegram with Carrie, Plane) | H | 7.17 |
| `hermes:peter-griffin:b240575fae0c` | hermes | `53 5 * * *` (profile tz) → `53 5 * * *` (America/New_York) | `~/.hermes/profiles/peter-griffin/cron/jobs.json` (unversioned) | ext, LLM (report) | H | 7.17 |
| `hermes:peter-griffin:3684c5aa8eac` | hermes | `23 6 * * *` (profile tz) → `23 6 * * *` (America/New_York); gated on b240575fae0c manifest < 2h | `~/.hermes/profiles/peter-griffin/cron/jobs.json` (unversioned) | ext, LLM (one prod change) | H | 7.17 |

## Excluded jobs (47)

| id | source | native schedule | class | reason |
| --- | --- | --- | --- | --- |
| `crontab:29` | crontab | `*/10 * * * *` | (b) | traefik-deathwatch restarts dead Traefik upstreams (the candybar container included) and Traefik itself; a scheduler cannot be its own watchdog |
| `anacron:cron.daily/db-backup` | other | anacron cron.daily ~07:35 | (b) | nightly pg_dump of the host Postgres holding candybar's own registry; migrating reintroduces the coupling the script avoids; root/anacron-owned; missed nights caught by infra-doctor 36h deadman |
| `systemd:hermes-gateway-watchdog.timer` | systemd | `OnUnitActiveSec=60s` | (b) | restarts a dead/wedged hermes-gateway (the Hermes runner depends on it); named in policy |
| `systemd:infra-doctor-deadman.timer` | systemd | `*:0/5` | (b) | deadman for infra-doctor; named in policy |
| `systemd:infra-doctor.timer` | systemd | `OnUnitActiveSec=60s` | (b) | synthetic probes over docker, Postgres and endpoints candybar depends on |
| `systemd:op-token-guard` | systemd | `OnBootSec=3min` + daily | (b) | self-heals the 1Password token plumbing the host executor depends on; boot-relative trigger not expressible in candybar |
| `hermes:33god-pm:061c4dbedc1c` | hermes | every 15m | (b) | crank-watchdog: deadman for the crank beat (`777c511a009c`) and gateway/scheduler silence; must stay independent of candybar once candybar drives the crank beat |
| `systemd:asm-sweep.timer` | systemd | `OnUnitActiveSec=15s` | (c) | 15s cadence, below candybar's 1-minute resolution; also an agent liveness sweeper |
| `anacron:cron.daily` | other | anacron.timer 07..23:30 → cron.daily | (a) | vendor scripts (0anacron, apport, apt-compat, dpkg, locate, logrotate, man-db, sysstat, browser/slack apt keepers); db-backup listed separately |
| `anacron:cron.weekly` | other | anacron cron.weekly | (a) | 0anacron, man-db (no-op under systemd) |
| `anacron:cron.monthly` | other | anacron cron.monthly | (a) | 0anacron only |
| `anacron:cron.yearly` | other | anacron cron.yearly | (a) | 0anacron only |
| `crontab:/etc/crontab:19` | crontab | `17 * * * *` root | (a) | cron.hourly run-parts; directory empty (no-op) |
| `crontab:/etc/crontab:20` | crontab | `25 6 * * *` root | (a) | cron.daily fallback; no-op while anacron installed |
| `crontab:/etc/crontab:21` | crontab | `47 6 * * 7` root | (a) | cron.weekly fallback; no-op |
| `crontab:/etc/crontab:22` | crontab | `52 6 1 * *` root | (a) | cron.monthly fallback; no-op |
| `crontab:/etc/cron.d/anacron:5` | crontab | `30 7-23 * * *` root | (a) | anacron package; no-op under systemd |
| `crontab:/etc/cron.d/e2scrub_all:1` | crontab | `30 3 * * 0` root | (a) | e2fsprogs; no-op under systemd |
| `crontab:/etc/cron.d/e2scrub_all:2` | crontab | `10 3 * * *` root | (a) | e2fsprogs; no-op under systemd |
| `crontab:/etc/cron.d/php:14` | crontab | `9,39 * * * *` root | (a) | php-common sessionclean; no-op under systemd |
| `crontab:/etc/cron.d/sysstat:6` | crontab | `5-55/10 * * * *` root | (a) | sysstat collect; no-op under systemd |
| `crontab:/etc/cron.d/sysstat:9` | crontab | `59 23 * * *` root | (a) | sysstat rotate; no-op under systemd |
| `systemd:launchpadlib-cache-clean.timer` | systemd | `OnUnitActiveSec=1d` | (a) | python3-launchpadlib package unit |
| `systemd:snap.firmware-updater.firmware-notifier` | systemd | every 3h | (a) | snapd-generated unit |
| `systemd:systemd-tmpfiles-clean` | systemd | `04:20` (our drop-in) | (a) | stock systemd unit; only the drop-in is ours |
| `systemd:ubuntu-insights-collect` | systemd | monthly | (a) | ubuntu-insights package |
| `systemd:ubuntu-insights-upload` | systemd | weekly | (a) | ubuntu-insights package |
| `systemd:ubuntu-2604-reminder` | systemd | `*-04-16..23 09:00` | (a)+(d) | ubuntu-* and finished (event passed; no-op from 2027). Recommend deleting the unit and script |
| `crontab:15` | crontab | `# 0 */2 * * *` git-checkpoint ~/d | (d) | commented out since 2026-07-20 |
| `crontab:16` | crontab | `# 0 */2 * * *` git-checkpoint ~/.claude | (d) | commented out since 2026-07-20 |
| `crontab:17` | crontab | `# 0 */2 * * *` git-checkpoint ~/.openclaw | (d) | commented out since 2026-07-20 |
| `crontab:18` | crontab | `# 0 */2 * * *` git-checkpoint ~/.config/opencode | (d) | commented out since 2026-07-20 |
| `crontab:19` | crontab | `# 0 */2 * * *` git-checkpoint ~/docker | (d) | commented out since 2026-07-20 |
| `crontab:20` | crontab | `# 15 0 * * *` git-checkpoint ~/code/33GOD | (d) | commented out since 2026-07-20 |
| `crontab:21` | crontab | `# 15 0 * * *` git-checkpoint ~/code/drumjangler | (d) | commented out since 2026-07-20 |
| `crontab:25` | crontab | `#0 * * * *` poliwale wallets | (d) | commented out (whale bot block) |
| `crontab:26` | crontab | `#*/10 * * * *` poliwale cycle | (d) | commented out |
| `crontab:27` | crontab | `#0 */2 * * *` poliwale digest | (d) | commented out |
| `systemd:vexa-bot-image-guard` | systemd | unknown (dangling) | (d) | unit symlinks point at deleted files; cleanup: remove symlinks + `timers.target.wants` link |
| `systemd:vexa-output-health` | systemd | unknown (dangling) | (d) | symlinks into a deleted worktree; cleanup: symlinks, wants link, orphan `vexa-output-health.service.d/` |
| `hermes:33god-pm:aec0b33a1d07` | hermes | every 5m (288/288) | (d) | Board Cranker loop finished (completed 2026-09-05) |
| `hermes:33god-pm:10e403479c01` | hermes | every 10m (0/12) | (d) | HOLOC-12 supervision paused since 2026-10-11, never ran |
| `hermes:33god-pm:e3a3143a80e7` | hermes | once 2026-10-21 07:00 | (d) | one-shot ROI analysis, completed |
| `hermes:pile-of-dumb-things-pm:b57a1f0a168c` | hermes | `0 16 * * *` | (d) | foundry-daily paused since 2026-10-24 (systemd `tolt-forge-daily` is the live Foundry driver) |
| `hermes:rererere:0593d5261d65` | hermes | every 30m (10/10) | (d) | Damian overnight continuation, completed 2026-10-21 |
| `hermes:rererere:a27e97e13e70` | hermes | once 2026-10-21 08:30 | (d) | one-shot briefing, completed |
| `hermes:rererere:28df06f1ea3d` | hermes | every 5m (4/12) | (d) | IPM bug review paused since 2026-10-25 |

## Cross-cutting facts the wave stories depend on

- **Ordering chains** (preserved by cron minute, same wave): `forever-ago-vault` 03:00 → `restic-vault` 04:30 → `backup-drill` (monthly restore); `anacron db-backup` ~07:35 (excluded) → `newapi-db-pull` 08:05 and `restic-dbdumps` 09:15; `intelliforia-pull-prod` 04:15 → `intelliforia-client-health` 05:30 (ExecStartPre waits) and `intelliforia-staging-track-main` (stands down while pull-prod is active); `ipm-metrics` → `ipm-internal-dashboard` (+1 min); `james-brennan-nightly-suite` 03:11 → `james-brennan-nightly-fix` 04:23; `hermes:peter-griffin:b240575fae0c` 05:53 → `hermes:peter-griffin:3684c5aa8eac` 06:23; Dev Journal `hermes:33god-pm:8da3f24263f9` 06:00 → `dev-journal-ensure-nightly` 06:15 → `sync-daily-reports-to-s3` 06:30.
- **DST-sensitive**: `systemd:ifvault-nightly.timer` 01:40 local sits in the 2026-11-01 repeated hour (must fire once); `systemd:milkshake-ledger-backup.timer` 02:30 local sits in the 2027-03-14 gap (must fire once at 03:00 EDT). Every `America/New_York` job above shifts its UTC instant by one hour on 2026-11-01.
- **Long runners** (executor `in_progress` heartbeats every 25s already cover the 1h ack_wait): `restic-dbdumps`/`restic-vault` (6h), `tolt-forge-daily` (9h), `pr-crusher` (2h), `state-maintain` (2h), `crontab:22` (~101 min).
- **No-overlap required**: `telnyx-s3-intake`, `mobile-deploy-hub-catchup`, `crontab:22` (flock), every systemd oneshot (systemd never overlaps a unit with itself).
- **Inventory consumers that would under-report after migration**: `systemd:srvls-metrics`/`srvls-snapshot` (srvls inventories crontab/systemd/docker/pm2) and the Dev Journal `fleet_health` collector (reads `systemctl --user list-timers` and Hermes `jobs.json`). Both must learn to read the candybar registry in their wave.
- **Pre-existing failures** (migration preserves behaviour; parity, not success, is the verification bar; fixes go to the owning repo's backlog): `newapi-claude-token`, `pr-crusher`, `restic-dbdumps`, `skillex-hermes-resync` (exit 1 by design), `sync-jim-dropbox`, `tolt-forge-daily`, `tolt-items-daily`, `honda-hunt-daily-check`, `delodocs-wiki-self-tuning-weekly`, `ifvault-nightly`, `hindsight-template-sweep`, `dev-journal-reconcile`, `activity-report@james-brennan`.
- **Untracked units that need a tracked home before cutover** (so the ledger can point at a commit): `gnome-shell-restart`, `srvls-metrics`, `srvls-snapshot`, `forever-ago-vault`, `restic-vault`, `backup-drill`, `intelliforia-pull-prod`, `ipm-metrics`, `sync-jim-dropbox`.
