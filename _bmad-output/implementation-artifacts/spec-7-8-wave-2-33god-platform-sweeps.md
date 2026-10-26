---
title: 'Story 7.8: Wave 2 — 33GOD platform sweeps'
type: 'feature'
created: '2026-10-26'
status: 'in-review'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '8576544'
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-7-context.md'
  - '{project-root}/deploy/migration/README.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Seven 33GOD-platform janitorial jobs (bloodbank hook health, candystore project sync, holocene pad roster, hermes install-drift guard, skillex desk resync, naipkins registry snapshot, n8n workflow export) still run on systemd user timers; wave 2 moves them onto candybar so the platform dogfoods its own scheduler.

**Approach:** all seven become `systemd`-runner candybars (the `.service` units stay; only the `.timer`s retire), driven end-to-end by `candybar migrate` over `deploy/migration/ledger.yaml`: ledger entry -> owning-repo source-of-truth fix (commit+push, sha recorded) -> `plan --write` -> `cutover` -> one manual run -> `verify` on the first scheduled fire. No daemon code changes, no redeploy.

## Boundaries & Constraints

**Always:** cron + tz per `cron-inventory.md`; interval timers (`OnUnitActiveSec`) keep their interval with phase not preserved (recorded deviation); every cutover via the ledger CLI, never hand-rolled; each owning repo's installer/doc stops re-enabling its timer BEFORE cutover (cutover refuses otherwise); one manual `candybar run` per job (all internal side effects) must succeed — `skillex-hermes-resync` exits 1 by design today, so it verifies on result parity (`native_failing: true`); a job that fails on candybar beyond parity is rolled back via the ledger immediately.

**Never:** touching `skillex-hermes-resync.path` (event trigger, stays on systemd); migrating any job not listed; redeploying the candybar container (no daemon code changes; other waves run concurrently); manually running a job before its candybar is registered; staging other agents' working-tree edits (flume, holocene are dirty).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| interval timer cutover | `*/5`, `*/2`, `*/15` jobs | candybar fires on the minute grid; phase shift recorded as deviation | cutover guard = period/4 |
| wall-clock timer | skillex `7,22,37,52` + naipkins `*/5`, America/New_York | same minute marks under candybar; 30s jitter dropped (deviation) | cutover waits out pending jitter window |
| skillex parity | exits 1 natively (desks refused) | first candybar fire may fail; `verify` passes on parity | a NEW failure mode (not exit 1 from the script) rolls back |
| copied units | bloodbank timer, hermes-guard timer+service are copies, not symlinks | rollback enables by resolved path under `~/.config/systemd/user/` | `plan --write` fills `unit_file` |
| candystore hourly | `5 * * * *` | first scheduled fire up to 59 min after cutover | manual run proves execution; scheduled fire may land after the story window -> pendingFirstFire |

</frozen-after-approval>

## Code Map

- `deploy/migration/ledger.yaml` -- gains 7 entries (wave `7.8`), driven by `uv run candybar migrate {plan,cutover,verify,status}`; the CLI locks the ledger dir, retry if a concurrent wave holds it.
- `src/candybar/migration/{ops,ledger,native}.py` -- the cutover machinery (story 7.5); do not modify.
- Owning repos (source of truth, each committed+pushed before its cutover):
  - `~/code/33GOD/bloodbank` `services/agent-hooks/ops/systemd/install-timer.sh` -- drop the `enable --now` of the timer (keep installing both unit files; scheduling is candybar's now).
  - `~/code/33GOD/candystore` `mise.toml` task `project:sync:install` -- drop the timer enable line (keep the symlinks + one-shot start).
  - `~/code/33GOD/holocene` `services/pad-roster/install.sh` -- drop `enable --now holocene-pad-roster.timer`.
  - `~/code/33GOD/hermes-agent-template` `scripts/systemd/hermes-single-install-guard.timer` header comment + `docs/fleet-upgrade.md` drift-guard paragraph -- timer retired; mirror the unit note in `~/code/33GOD/flume` `templates/hermes-agent/scripts/systemd/hermes-single-install-guard.timer` (stage only that file; tree is dirty with other work).
  - `~/code/skillex` `scripts/install-hermes-resync.sh` -- `ENABLED` drops the timer (path stays), status check stops requiring the timer enabled, header comment updated.
  - `~/code/naipkins` `README.md` install block -- drop the `enable --now` line.
  - `~/code/delonet-n8n-workflows` `README.md` Sync block -- drop the `enable --now` line.
- Live facts (verified this session): all 7 timers active; all services oneshot and last-run `success` except skillex (`exit-code 1`, by design); systemd runner proven live by `deploy-smoke-systemd` (17:00Z fire, metered); executor auto-installs the `50-candybar.conf` env drop-in per unit.

## Tasks & Acceptance

**Execution:**
- [x] 7x owning-repo source-of-truth edits above -- commit + push each; record the sha in the ledger entry's `source_of_truth.commit`
- [x] `deploy/migration/ledger.yaml` -- 7 entries with cron/tz/deviations/native_failing per inventory; `candybar migrate plan --write` fills `unit_file` + default disable/rollback; commit
- [x] 7x `candybar migrate cutover <id> --wait 600` -- register disabled -> retire timer -> enable; commit ledger after each batch
- [x] 7x manual `candybar run <id>` then read the invocation row -- success (skillex: exit 1 parity); roll back any real failure via `candybar migrate rollback`
- [ ] `candybar migrate verify <id>` per job once its first scheduled fire lands; commit ledger; jobs whose first fire is far out go to pendingFirstFire
- [ ] `sprint-status.yaml` 7-8 -> review; this spec -> done; commit + push (candybar repo)

**Acceptance Criteria:**
- Given wave 1's foundation (7.1-7.6 done), when this wave lands, then every listed job has a ledger entry and runs under candybar with the inventory's cron + tz, its unit file unchanged apart from the runner's auto-installed drop-in, and its timer disabled (`systemctl --user list-timers` shows none of the seven)
- When skillex's installer is re-run, then it enables only the `.path`, never the timer; the other six repos' installers/docs likewise cannot resurrect their timers
- When `skillex-hermes-resync` fires on candybar, then its exit-1-by-design verifies on result parity
- Given any migrated job, when its failure occurs, then the unit's existing result path (journal + candybar event) reports it; no job is left on two schedulers or none

## Verification

**Commands:**
- `uv run candybar migrate status --wave 7.8 --probe` -- expected: all entries cutover/verified, native=retired
- `uv run candybar migrate status --wave 7.8 --probe` -- expected: all seven native=retired (systemd may list disabled timers with NEXT `-` even without `--all`)
- `curl -s http://127.0.0.1:8700/candybars` -- the 7 names present, enabled, correct tz
- `cd <owning repo> && git log --oneline -1` per repo -- the source-of-truth commit is on origin

## Implementation Notes

- Seven owning-repo source-of-truth commits pushed: bloodbank `3bced422fdedb333a994eab751ad9ad548bfc3de`; candystore `175a54dc5ab1990e83005afbd1052c1b513449ce`; holocene `95529e9eab7e48bb223364acd5a18da8b4760988`; hermes-agent-template `fb2d8fd78e6b3e958254cd4bf3e1a574436bf022` (flume submodule pointer bumped in `24634ee`); skillex `7a0becd7fa06bad73289bf60a2835b76686d6cbb` + installer test `e19780b`; naipkins `14db6532f824b88299120b1d5ac353b1d73f2051`; delonet-n8n-workflows `2be096eccf868baf0afe3432de64de742289b011`.
- The ledger plan (commit `6a011dc`) was authored against a stale shared checkout during concurrent wave commits; it dropped 23 other-wave records on origin. Another agent restored the union in `1365145`. My seven entries, their full source-of-truth commits, and all other waves' records are intact. Future wave writes must merge by inventory id, not replace the whole shared ledger from a stale tree.
- All seven cutovers used `candybar migrate cutover` with `--wait` as needed. Each registered disabled, retired its native timer, then enabled the candybar. The `.service` units stayed and the executor installed its own `50-candybar.conf` drop-in. `skillex-hermes-resync.path` remains active/enabled.
- Every job received one `candybar run` manual invocation; six succeeded and skillex failed with exit 1 exactly as it did natively. Holocene's first manual invocation was coalesced with a scheduled fire, so a second manual invocation was sent and succeeded.
- First scheduled fires verified in the ledger: bloodbank-agent-hooks-health `b43cf1ab-fdff-5161-8a87-dd702d9dbd6e`, holocene-pad-roster `2dbcf9e2-159a-5743-99cb-93c2c1a92f3c`, naipkins-registry-snapshot `1146c109-c204-5495-b7e1-6fadc1599412`, hermes-single-install-guard `6dfcb340-9451-528c-9e20-5b4b693bd9f8`, delonet-n8n-workflows-sync `8db145e7-efca-5ff9-8eb4-f1b7d4d8d734`, skillex-hermes-resync `a51b1b36-a331-5ac7-bdc4-08a83fa6e10a` (fail exit 1, result parity with native). Candystore's first scheduled fire is 18:05Z; pending at 17:54Z.
- Seven manifests committed at `deploy/migration/manifests/<name>/candybar.toml` (commit `811f768`), validated by `load_manifest`. No daemon code was changed and no container redeploy was made.
- Verification: `/healthz` 200; executor active; seven candybars enabled and seven timers retired (`migrate status --wave 7.8 --probe`). Live first-fire rows carry nonzero memory metering. `skillex-hermes-resync.path` still active. No job was rolled back.

## Spec Change Log

## Review Triage Log
