# Epic 7 Cutover Report

Generated 2026-10-26T18:28:44Z from `deploy/migration/ledger.yaml` (70 entries).

- Verified by first scheduled fire: **28**
- Cut over, first scheduled fire pending: **36** (overnight backups, daily jobs, four Hermes jobs, weekly cache-prune)
- Rolled back (native scheduler restored): **6**

Rollback procedure: `.venv/bin/candybar migrate rollback <ledger-id> --reason '...'` from the repo root; if candybar is down, run the entry's ledger `rollback` commands first, then rerun the CLI when the API recovers.

## Verified — first scheduled fire succeeded
| ledger id | name | schedule | wave | note |
|---|---|---|---|---|
| `systemd:hindsight-capture-sweep.timer` | hindsight-capture-sweep | `*/10 * * * *` | 7.10 |  |
| `systemd:hindsight-write-probe.timer` | hindsight-write-probe | `4 * * * *` | 7.10 |  |
| `systemd:mobile-deploy-hub-catchup.timer` | mobile-deploy-hub-catchup | `*/2 * * * *` | 7.11 |  |
| `systemd:mobile-deploy-hub-macwatch.timer` | mobile-deploy-hub-macwatch | `1-59/2 * * * *` | 7.11 |  |
| `systemd:telnyx-s3-intake` | telnyx-s3-intake | `* * * * *` | 7.11 |  |
| `systemd:newapi-claude-token` | newapi-claude-token | `*/30 * * * *` | 7.13 |  |
| `systemd:newapi-oauth-connections` | newapi-oauth-connections | `*/5 * * * *` | 7.13 |  |
| `systemd:newapi-staff-sso` | newapi-staff-sso | `7 * * * *` | 7.13 |  |
| `systemd:newapi-vm-health` | newapi-vm-health | `*/5 * * * *` | 7.13 |  |
| `systemd:ipm-internal-dashboard.timer` | ipm-internal-dashboard | `1-59/5 * * * *` | 7.14 |  |
| `systemd:ipm-metrics.timer` | ipm-metrics | `*/5 * * * *` | 7.14 |  |
| `systemd:sync-jim-dropbox` | sync-jim-dropbox | `*/10 * * * *` | 7.14 |  |
| `systemd:dev-journal-reconcile.timer` | dev-journal-reconcile | `0 * * * *` | 7.15 |  |
| `hermes:33god-pm:9b5dadbd1e4d` | jev-rollup-cadence | `*/10 * * * *` | 7.15 |  |
| `hermes:33god-pm:f3db72089593` | aperture-lane-beat | `0 */2 * * *` | 7.17 |  |
| `hermes:33god-pm:777c511a009c` | crank-beat | `*/5 * * * *` | 7.17 |  |
| `systemd:cache-prune-codex-tmp.timer` | cache-prune-codex-tmp | `17 * * * *` | 7.7 |  |
| `systemd:gnome-shell-restart.timer` | gnome-shell-restart | `0 * * * *` | 7.7 |  |
| `systemd:srvls-metrics` | srvls-metrics | `*/5 * * * *` | 7.7 |  |
| `systemd:zellij-ark` | zellij-ark | `*/2 * * * *` | 7.7 |  |
| `systemd:bloodbank-agent-hooks-health.timer` | bloodbank-agent-hooks-health | `*/5 * * * *` | 7.8 |  |
| `systemd:candystore-project-sync.timer` | candystore-project-sync | `5 * * * *` | 7.8 |  |
| `systemd:delonet-n8n-workflows-sync.timer` | delonet-n8n-workflows-sync | `*/15 * * * *` | 7.8 |  |
| `systemd:hermes-single-install-guard.timer` | hermes-single-install-guard | `*/15 * * * *` | 7.8 |  |
| `systemd:holocene-pad-roster.timer` | holocene-pad-roster | `*/2 * * * *` | 7.8 |  |
| `systemd:naipkins-registry-snapshot` | naipkins-registry-snapshot | `*/5 * * * *` | 7.8 |  |
| `systemd:skillex-hermes-resync` | skillex-hermes-resync | `7,22,37,52 * * * *` | 7.8 |  |
| `systemd:delodocs-inbox-cleanup.timer` | delodocs-inbox-cleanup | `*/10 * * * *` | 7.9 |  |

## Cut over — first fire pending
| ledger id | name | schedule | wave | note |
|---|---|---|---|---|
| `systemd:hindsight-memory-analysis.timer` | hindsight-memory-analysis | `0 5 * * *` | 7.10 |  |
| `systemd:gruvato-acoustid-check.timer` | gruvato-acoustid-check | `7 */3 * * *` | 7.11 |  |
| `systemd:intelliforia-client-health.timer` | intelliforia-client-health | `30 5 * * *` | 7.12 |  |
| `systemd:intelliforia-client-health-monday.timer` | intelliforia-client-health-monday | `30 8 * * 1` | 7.12 |  |
| `systemd:intelliforia-pull-prod.timer` | intelliforia-pull-prod | `15 4 * * *` | 7.12 |  |
| `systemd:intelliforia-staging-track-main.timer` | intelliforia-staging-track-main | `5 7 * * 1-5` | 7.12 |  |
| `systemd:aai-free-scout.timer` | aai-free-scout | `37 4 * * *` | 7.13 |  |
| `systemd:newapi-db-pull` | newapi-db-pull | `5 8 * * *` | 7.13 |  |
| `systemd:newapi-openrouter-sync` | newapi-openrouter-sync | `47 3 * * *` | 7.13 |  |
| `systemd:newapi-token-watch` | newapi-token-watch | `30 8 * * *` | 7.13 |  |
| `systemd:activity-report@james-brennan.timer` | activity-report-james-brennan | `30 3 * * *` | 7.14 |  |
| `systemd:james-brennan-billing-ledger.timer` | james-brennan-billing-ledger | `17 6 * * *` | 7.14 |  |
| `systemd:james-brennan-daily-update.timer` | james-brennan-daily-update | `0 3 * * *` | 7.14 |  |
| `systemd:james-brennan-nightly-fix.timer` | james-brennan-nightly-fix | `23 4 * * *` | 7.14 |  |
| `systemd:james-brennan-nightly-suite.timer` | james-brennan-nightly-suite | `11 3 * * *` | 7.14 |  |
| `systemd:dev-journal-ensure-nightly.timer` | dev-journal-ensure-nightly | `15 6 * * *` | 7.15 |  |
| `hermes:33god-pm:8da3f24263f9` | dev-journal-nightly | `0 6 * * *` | 7.15 |  |
| `systemd:sync-daily-reports-to-s3` | sync-daily-reports-to-s3 | `30 6 * * *` | 7.15 |  |
| `systemd:honda-hunt-daily-check.timer` | honda-hunt-daily-check | `0 9 * * *` | 7.16 |  |
| `systemd:pr-crusher` | pr-crusher | `0 3 * * *` | 7.16 |  |
| `systemd:tolt-forge-daily` | tolt-forge-daily | `0 16 * * *` | 7.16 |  |
| `systemd:tolt-items-daily` | tolt-items-daily | `0 3 * * *` | 7.16 |  |
| `hermes:delodocs-pm:fc39bd8b33c8` | delodocs-triage-second-pass | `0 9 * * *` | 7.17 |  |
| `hermes:dumply:8aecd027dbd0` | dumply-daily-story | `30 20 * * *` | 7.17 |  |
| `hermes:peter-griffin:3684c5aa8eac` | token-optimization | `23 6 * * *` | 7.17 |  |
| `hermes:peter-griffin:b240575fae0c` | token-usage-report | `53 5 * * *` | 7.17 |  |
| `systemd:cache-prune.timer` | cache-prune | `7 5 * * 0` | 7.7 |  |
| `systemd:cache-prune-daily.timer` | cache-prune-daily | `23 5 * * *` | 7.7 |  |
| `systemd:docker-prune.timer` | docker-prune | `41 5 * * *` | 7.7 |  |
| `systemd:srvls-snapshot` | srvls-snapshot | `10 4 * * *` | 7.7 |  |
| `systemd:state-maintain` | state-maintain | `40 5 * * *` | 7.7 |  |
| `systemd:user-logrotate` | user-logrotate | `17 5 * * *` | 7.7 |  |
| `systemd:forever-ago-vault.timer` | forever-ago-vault | `0 3 * * *` | 7.9 |  |
| `systemd:restic-dbdumps` | restic-dbdumps | `15 9 * * *` | 7.9 |  |
| `systemd:restic-vault` | restic-vault | `30 4 * * *` | 7.9 |  |
| `crontab:22` | sync-vault-to-drive | `30 */2 * * *` | 7.9 |  |

## Rolled back
| ledger id | name | schedule | wave | note |
|---|---|---|---|---|
| `systemd:hindsight-template-sweep.timer` | hindsight-template-sweep | `30 4 * * *` | 7.10 | candybar 01a1793e-46ae-724c-9c34-eed9b832445b disabled |
| `systemd:newapi-allowances` | newapi-allowances | `*/5 * * * *` | 7.13 | candybar 01a1793a-d2a0-7070-b267-4f6d3e41a3b5 disabled |
| `systemd:delodocs-wiki-self-tuning-weekly.timer` | delodocs-wiki-self-tuning-weekly | `45 8 * * 1` | 7.16 | candybar 01a17938-9a3b-71bc-b1b9-c0ceadfc86fe disabled |
| `systemd:backup-drill.timer` | backup-drill | `0 9 1 * *` | 7.9 | candybar 01a17948-223b-7507-9eb4-2664bf0e787c disabled |
| `systemd:ifvault-nightly.timer` | ifvault-nightly | `40 1 * * *` | 7.9 | candybar 01a1794a-aaf6-7d74-9978-64c569f0cff2 disabled |
| `systemd:milkshake-ledger-backup.timer` | milkshake-ledger-backup | `30 2 * * *` | 7.9 | candybar 01a1794a-a6ad-77ae-9993-72cdf80a0f62 disabled |

## Excluded (47 jobs — stayed on their current scheduler)
20 OS/vendor-owned, 7 liveness guards of candybar's own dependencies (hermes-gateway-watchdog, infra-doctor-deadman, docker-prune, …), sub-minute cadence (asm-sweep 15s), dead/paused entries. Full table: `_bmad-output/planning-artifacts/cron-inventory.md`

## Known parity failures (migrated; failing exactly as before migration)
skillex-hermes-resync (exit 1 — unowned desk entries need human triage), newapi-claude-token (exit 1), sync-jim-dropbox (exit 2), dev-journal-reconcile (exit 3). Candybar now surfaces these instead of journalctl.
