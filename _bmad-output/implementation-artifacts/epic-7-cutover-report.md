# Epic 7 Cutover Report

Generated 2026-10-26; first-fire sweep updated 2026-10-27T14:45Z from `deploy/migration/ledger.yaml` (70 entries).

- Verified by first scheduled fire: **62** (51 success, 11 failing on documented native parity)
- Cut over, first scheduled fire pending: **2**
- Rolled back (native scheduler restored): **6**

Rollback: `.venv/bin/candybar migrate rollback <ledger-id> --reason '...'` from the repo root; if candybar is down, run the entry's ledger `rollback` commands first, then rerun the CLI.

## Verified
| ledger id | name | schedule | tz | wave | first-fire result |
|---|---|---|---|---|---|
| `systemd:hindsight-capture-sweep.timer` | hindsight-capture-sweep | `*/10 * * * *` | UTC | 7.10 | success |
| `systemd:hindsight-memory-analysis.timer` | hindsight-memory-analysis | `0 5 * * *` | America/New_York | 7.10 | success |
| `systemd:hindsight-write-probe.timer` | hindsight-write-probe | `4 * * * *` | UTC | 7.10 | success |
| `systemd:gruvato-acoustid-check.timer` | gruvato-acoustid-check | `7 */3 * * *` | America/New_York | 7.11 | success |
| `systemd:mobile-deploy-hub-catchup.timer` | mobile-deploy-hub-catchup | `*/2 * * * *` | America/New_York | 7.11 | success |
| `systemd:mobile-deploy-hub-macwatch.timer` | mobile-deploy-hub-macwatch | `1-59/2 * * * *` | America/New_York | 7.11 | success |
| `systemd:telnyx-s3-intake` | telnyx-s3-intake | `* * * * *` | America/New_York | 7.11 | success |
| `systemd:intelliforia-client-health.timer` | intelliforia-client-health | `30 5 * * *` | America/New_York | 7.12 | success |
| `systemd:intelliforia-pull-prod.timer` | intelliforia-pull-prod | `15 4 * * *` | America/New_York | 7.12 | success |
| `systemd:intelliforia-staging-track-main.timer` | intelliforia-staging-track-main | `5 7 * * 1-5` | America/New_York | 7.12 | success |
| `systemd:aai-free-scout.timer` | aai-free-scout | `37 4 * * *` | America/New_York | 7.13 | fail |
| `systemd:newapi-claude-token` | newapi-claude-token | `*/30 * * * *` | UTC | 7.13 | fail |
| `systemd:newapi-db-pull` | newapi-db-pull | `5 8 * * *` | America/New_York | 7.13 | success |
| `systemd:newapi-oauth-connections` | newapi-oauth-connections | `*/5 * * * *` | UTC | 7.13 | success |
| `systemd:newapi-openrouter-sync` | newapi-openrouter-sync | `47 3 * * *` | America/New_York | 7.13 | success |
| `systemd:newapi-staff-sso` | newapi-staff-sso | `7 * * * *` | UTC | 7.13 | success |
| `systemd:newapi-token-watch` | newapi-token-watch | `30 8 * * *` | America/New_York | 7.13 | success |
| `systemd:newapi-vm-health` | newapi-vm-health | `*/5 * * * *` | UTC | 7.13 | success |
| `systemd:activity-report@james-brennan.timer` | activity-report-james-brennan | `30 3 * * *` | America/New_York | 7.14 | success |
| `systemd:ipm-internal-dashboard.timer` | ipm-internal-dashboard | `1-59/5 * * * *` | America/New_York | 7.14 | success |
| `systemd:ipm-metrics.timer` | ipm-metrics | `*/5 * * * *` | America/New_York | 7.14 | success |
| `systemd:james-brennan-billing-ledger.timer` | james-brennan-billing-ledger | `17 6 * * *` | America/New_York | 7.14 | success |
| `systemd:james-brennan-daily-update.timer` | james-brennan-daily-update | `0 3 * * *` | America/New_York | 7.14 | success |
| `systemd:james-brennan-nightly-fix.timer` | james-brennan-nightly-fix | `23 4 * * *` | America/New_York | 7.14 | success |
| `systemd:james-brennan-nightly-suite.timer` | james-brennan-nightly-suite | `11 3 * * *` | America/New_York | 7.14 | success |
| `systemd:sync-jim-dropbox` | sync-jim-dropbox | `*/10 * * * *` | UTC | 7.14 | fail |
| `systemd:dev-journal-ensure-nightly.timer` | dev-journal-ensure-nightly | `15 6 * * *` | America/New_York | 7.15 | success |
| `hermes:33god-pm:8da3f24263f9` | dev-journal-nightly | `0 6 * * *` | America/New_York | 7.15 | success |
| `systemd:dev-journal-reconcile.timer` | dev-journal-reconcile | `0 * * * *` | America/New_York | 7.15 | fail |
| `hermes:33god-pm:9b5dadbd1e4d` | jev-rollup-cadence | `*/10 * * * *` | America/New_York | 7.15 | success |
| `systemd:sync-daily-reports-to-s3` | sync-daily-reports-to-s3 | `30 6 * * *` | America/New_York | 7.15 | success |
| `systemd:honda-hunt-daily-check.timer` | honda-hunt-daily-check | `0 9 * * *` | America/New_York | 7.16 | fail |
| `systemd:pr-crusher` | pr-crusher | `0 3 * * *` | America/New_York | 7.16 | fail |
| `systemd:tolt-forge-daily` | tolt-forge-daily | `0 16 * * *` | America/New_York | 7.16 | fail |
| `systemd:tolt-items-daily` | tolt-items-daily | `0 3 * * *` | America/New_York | 7.16 | fail |
| `hermes:33god-pm:f3db72089593` | aperture-lane-beat | `0 */2 * * *` | UTC | 7.17 | success |
| `hermes:33god-pm:777c511a009c` | crank-beat | `*/5 * * * *` | America/New_York | 7.17 | success |
| `hermes:delodocs-pm:fc39bd8b33c8` | delodocs-triage-second-pass | `0 9 * * *` | America/New_York | 7.17 | success |
| `hermes:dumply:8aecd027dbd0` | dumply-daily-story | `30 20 * * *` | America/New_York | 7.17 | success |
| `hermes:peter-griffin:3684c5aa8eac` | token-optimization | `23 6 * * *` | America/New_York | 7.17 | success |
| `hermes:peter-griffin:b240575fae0c` | token-usage-report | `53 5 * * *` | America/New_York | 7.17 | success |
| `systemd:cache-prune-codex-tmp.timer` | cache-prune-codex-tmp | `17 * * * *` | America/New_York | 7.7 | success |
| `systemd:cache-prune-daily.timer` | cache-prune-daily | `23 5 * * *` | America/New_York | 7.7 | success |
| `systemd:docker-prune.timer` | docker-prune | `41 5 * * *` | America/New_York | 7.7 | success |
| `systemd:gnome-shell-restart.timer` | gnome-shell-restart | `0 * * * *` | America/New_York | 7.7 | success |
| `systemd:srvls-metrics` | srvls-metrics | `*/5 * * * *` | UTC | 7.7 | success |
| `systemd:srvls-snapshot` | srvls-snapshot | `10 4 * * *` | America/New_York | 7.7 | success |
| `systemd:state-maintain` | state-maintain | `40 5 * * *` | America/New_York | 7.7 | success |
| `systemd:user-logrotate` | user-logrotate | `17 5 * * *` | America/New_York | 7.7 | success |
| `systemd:zellij-ark` | zellij-ark | `*/2 * * * *` | UTC | 7.7 | success |
| `systemd:bloodbank-agent-hooks-health.timer` | bloodbank-agent-hooks-health | `*/5 * * * *` | UTC | 7.8 | success |
| `systemd:candystore-project-sync.timer` | candystore-project-sync | `5 * * * *` | UTC | 7.8 | success |
| `systemd:delonet-n8n-workflows-sync.timer` | delonet-n8n-workflows-sync | `*/15 * * * *` | UTC | 7.8 | success |
| `systemd:hermes-single-install-guard.timer` | hermes-single-install-guard | `*/15 * * * *` | UTC | 7.8 | success |
| `systemd:holocene-pad-roster.timer` | holocene-pad-roster | `*/2 * * * *` | UTC | 7.8 | success |
| `systemd:naipkins-registry-snapshot` | naipkins-registry-snapshot | `*/5 * * * *` | America/New_York | 7.8 | success |
| `systemd:skillex-hermes-resync` | skillex-hermes-resync | `7,22,37,52 * * * *` | America/New_York | 7.8 | fail |
| `systemd:delodocs-inbox-cleanup.timer` | delodocs-inbox-cleanup | `*/10 * * * *` | America/New_York | 7.9 | success |
| `systemd:forever-ago-vault.timer` | forever-ago-vault | `0 3 * * *` | America/New_York | 7.9 | success |
| `systemd:restic-dbdumps` | restic-dbdumps | `15 9 * * *` | America/New_York | 7.9 | fail |
| `systemd:restic-vault` | restic-vault | `30 4 * * *` | America/New_York | 7.9 | success |
| `crontab:22` | sync-vault-to-drive | `30 */2 * * *` | America/New_York | 7.9 | fail |

## Cut over — first fire pending
| ledger id | name | schedule | tz | wave | first-fire result |
|---|---|---|---|---|---|
| `systemd:intelliforia-client-health-monday.timer` | intelliforia-client-health-monday | `30 8 * * 1` | America/New_York | 7.12 |  |
| `systemd:cache-prune.timer` | cache-prune | `7 5 * * 0` | America/New_York | 7.7 |  |

## Rolled back
| ledger id | name | schedule | tz | wave | first-fire result |
|---|---|---|---|---|---|
| `systemd:hindsight-template-sweep.timer` | hindsight-template-sweep | `30 4 * * *` | America/New_York | 7.10 |  |
| `systemd:newapi-allowances` | newapi-allowances | `*/5 * * * *` | UTC | 7.13 | success |
| `systemd:delodocs-wiki-self-tuning-weekly.timer` | delodocs-wiki-self-tuning-weekly | `45 8 * * 1` | America/New_York | 7.16 |  |
| `systemd:backup-drill.timer` | backup-drill | `0 9 1 * *` | America/New_York | 7.9 |  |
| `systemd:ifvault-nightly.timer` | ifvault-nightly | `40 1 * * *` | America/New_York | 7.9 |  |
| `systemd:milkshake-ledger-backup.timer` | milkshake-ledger-backup | `30 2 * * *` | America/New_York | 7.9 |  |

## Native-parity failures (migrated; failing exactly as they did on their native scheduler)
aai-free-scout, dev-journal-reconcile, honda-hunt-daily-check, newapi-claude-token, pr-crusher, restic-dbdumps, skillex-hermes-resync, sync-jim-dropbox, sync-vault-to-drive, tolt-forge-daily, tolt-items-daily.

Root causes worth fixing at the source (not candybar defects): Google Drive API quota exhausted (`sync-vault-to-drive` has not completed a sync since at least 2026-10-14; `restic-dbdumps` failing daily since at least 2026-10-23), expired codex refresh token (`honda-hunt-daily-check`), pr-crusher credential broker rejecting `prc_github_read_token`, flaky provider signup (`aai-free-scout`, 2 of 3 native runs failed), skillex unowned desk entries (`skillex-hermes-resync`).

## Excluded (47 jobs — stayed on their current scheduler)
OS/vendor-owned, liveness guards of candybar's own dependencies, sub-minute cadence (asm-sweep 15s), dead/paused entries. Full table: `_bmad-output/planning-artifacts/cron-inventory.md`
