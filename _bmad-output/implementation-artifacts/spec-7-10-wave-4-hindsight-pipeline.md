---
title: 'Story 7.10: Wave 4 — Hindsight pipeline'
type: 'feature'
created: '2026-10-26'
status: 'in-progress'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '8576544'
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-7-context.md'
  - '{project-root}/deploy/migration/README.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Four Hindsight pipeline jobs (capture sweep, write probe, memory analysis, template sweep) still run on systemd user timers. Epic 7 moves every migrate-classified job onto candybar; this wave moves the memory-pipeline four so they are inventoried, metered and ledger-reversible like everything else.

**Approach (AD-18/AD-21):** each job becomes a systemd-runner candybar — the `.service` stays byte-identical (keeping `EnvironmentFile`, `TimeoutStartSec`, hardening and in-script ntfy alerting), only its `.timer` retires. Cutovers go through `candybar migrate` against `deploy/migration/ledger.yaml`; each owning repo's install path is changed first so a re-install cannot resurrect the timer.

## Boundaries & Constraints

**Always:** cron + timezone from the inventory verbatim (`*/10 * * * *` UTC capture-sweep; `4 * * * *` UTC write-probe; `0 5 * * *` America/New_York memory-analysis; `30 4 * * *` America/New_York template-sweep); runner `{kind: systemd, unit: <name>.service}` with the unit file unchanged; native jitter (AccuracySec / RandomizedDelaySec) dropped and recorded as an accepted deviation; interval timers keep their interval, phase not preserved; source-of-truth edits land in delorenj/bloodbank (hook-hub README + timer header), delorenj/agents (`~/.agents` hooks/hindsight timer header) and delorenj/DeLoContainers (stacks/ai/hindsight systemd dir), each committed and pushed BEFORE cutover with the sha recorded in the ledger; cutover order is register-disabled → retire timer → enable, driven only by `candybar migrate`; `hindsight-template-sweep` is `native_failing: true` (missing vault item 'Hypercharm', exit 1 — pre-existing) and verifies on result parity; internal jobs each get ONE manual `candybar run` post-cutover (success expected for the three healthy jobs; template-sweep must fail with the identical op/psql error = parity); the write-probe's infra-doctor 2h deadman must keep passing (the script and its stamp are untouched).

**Never:** editing any `.service` ExecStart/Environment; deleting `.timer` files (rollback enables by path); manually running a job with external side effects (all four are internal — manual runs allowed); hand-registering via SQL or `candybar register` outside the ledger; redeploying the candybar container (no daemon code changes in this story); touching other agents' dirty files in the shared owning repos (stage only the files this story edits); fixing the template-sweep vault bug itself (record, don't repair).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| capture-sweep cutover | next native fire ≤2 min away (10-min period) | `cutover --wait` polls until the guard window opens, then completes | refusal exits 1; retry after the native fire |
| cutover mid-jitter | memory-analysis/template-sweep RandomizedDelaySec pending | refused while a jittered native run may still be pending | none — last native runs are hours old, window open all afternoon |
| healthy job manual run | `candybar run hindsight-capture-sweep` (and write-probe, memory-analysis) | invocation `success`, metered; memory-analysis resolves HINDSIGHT_DB_PASSWORD via its unchanged op-run flow | fail → `candybar migrate rollback <id>` immediately |
| template-sweep manual run | known op failure | invocation `fail` with the same 'Hypercharm' vault error as native = parity | a DIFFERENT failure (missing unit, env lost) → rollback |
| enable after retire | candybar enabled=true post-cutover | next cron fire dispatches via host executor; timer stays disabled | both-schedulers check: `candybar migrate verify` fails loudly if the timer resurrects |
| re-install of owning repo | installer/README re-run | links/enables only the `.service`; timer stays retired | header comment in each `.timer` names the candybar successor |

</frozen-after-approval>

## Code Map

- `deploy/migration/ledger.yaml` -- gains four entries (ids `systemd:hindsight-{capture-sweep,write-probe,memory-analysis,template-sweep}.timer`, wave `7.10`); driven by `uv run candybar migrate plan --write / cutover / verify / status` — never hand-edited mid-flow except writing the initial entries.
- `deploy/migration/README.md` -- the ledger schema and per-kind defaults; the procedure this story follows verbatim.
- Native units (symlinked into `~/.config/systemd/user/` from these repo paths — only the timers retire, services stay):
  - `/home/delorenj/code/33GOD/bloodbank/services/hook-hub/systemd/hindsight-capture-sweep.{timer,service}` + README.md:552-557 install snippet (source-of-truth edit).
  - `/home/delorenj/.agents/hooks/hindsight/systemd/hindsight-write-probe.{timer,service}` (`~/.agents` IS the delorenj/agents checkout — remote verified; the epic note "live dir is not the repo" does not apply here; record that correction in the ledger entry).
  - `/home/delorenj/docker/stacks/ai/hindsight/systemd/hindsight-{memory-analysis,template-sweep}.{timer,service}` (~/docker = delorenj/DeLoContainers checkout, dirty tree belongs to other agents — stage only these files).
- `src/candybar/migration/ops.py` -- cutover guards (native-fire proximity, jitter window, service-running, source-of-truth-commit-required); read-only reference.
- Foundation (verified live this session, do not touch): `candybar-executor.service` active; `deploy-smoke-systemd` canary 8/8 metered successes; `timezone` column live in the API.

## Tasks & Acceptance

**Execution:**
- [x] delorenj/bloodbank `services/hook-hub/README.md` + `hindsight-capture-sweep.timer` -- install snippet links only the `.service`; timer file gains a retired-by-candybar header -- so re-install cannot resurrect the timer -- commit+push, record sha
- [x] delorenj/agents `hooks/hindsight/systemd/hindsight-write-probe.timer` -- retired-by-candybar header comment -- same -- commit+push only this file
- [x] delorenj/DeLoContainers `stacks/ai/hindsight/systemd/hindsight-{memory-analysis,template-sweep}.timer` (+ short `systemd/README.md`) -- retired headers + install note -- commit+push only these files
- [ ] `deploy/migration/ledger.yaml` -- four planned entries with full candybar/native blocks, deviations, `native_failing` flag on template-sweep, source_of_truth shas; `plan --write` each to fill `unit_file` + default disable/rollback -- commit. Entries and commands are committed, but plan defaults were filled via the same locked ledger helpers after another wave held the CLI directory lock and concurrently replaced the ledger, not through `plan --write` itself.
- [x] `candybar migrate cutover` x4 (capture-sweep with `--wait` for its 10-min guard window) -- each ends with timer disabled, candybar enabled; template-sweep subsequently rolled back after non-parity failure
- [ ] Manual `candybar run` x4 -- three healthy jobs must record `success` with metering; template-sweep must record `fail` carrying the same op 'Hypercharm' error (parity). It failed on a different op lookup and was rolled back, so this acceptance is unmet.
- [x] Write-probe deadman: after its run, `~/.local/state/hindsight-write-probe` stamp is fresh and infra-doctor's `hindsight-write-probe-deadman` stays OK
- [ ] `candybar migrate verify` for jobs whose first scheduled fire landed in-session (capture-sweep verified; write-probe pending at 18:04Z); memory-analysis stays `cutover` pending first fire; template-sweep is `rolled-back`
- [ ] sprint-status: `7-10-wave-4-hindsight-pipeline: review`; commit+push candybar (defer completion status until write-probe's scheduled fire is verified)

**Acceptance Criteria:**
- Given the four timers enabled at start, when the wave lands, then all four have ledger entries, their candybars fire on the recorded cron+tz, and `systemctl --user list-timers` shows none of the four timers
- Given a re-install from any owning repo's docs, when its install steps run, then only the `.service` is linked/enabled and no timer is resurrected
- Given template-sweep fails natively, when it runs under candybar, then it fails identically (parity) and keeps its native failure alerting path
- Given the write-probe migrates, when its script runs under candybar, then the infra-doctor 2h deadman keeps passing

## Verification

**Commands:**
- `uv run candybar migrate status --wave 7.10` -- all four entries `cutover`/`verified`, none `planned`
- `systemctl --user list-timers --all | grep hindsight` -- no hindsight timers remain enabled
- `curl -s http://127.0.0.1:8700/candybars` -- four new enabled candybars with the recorded cron+tz and systemd runner
- `git unpushed` -- candybar, bloodbank, agents, DeLoContainers all clean of this story's commits
- `curl -s https://candybar.delo.sh/healthz` -- 200 (container untouched; executor needs no restart for runner-less registrations)

## Implementation Notes

- Foundation verified live before any cutover: executor unit active, container `/healthz` 200, `deploy-smoke-systemd` scheduled invocations successful and metered; the API exposes `timezone` and `enabled`. No daemon code changed or container redeployed.
- Source-of-truth commits pushed before cutover: bloodbank `be782321a009ca569c9e9e31cab135a874bee156`, agents `aa35242c1cfa7a6000fa44503ab53f032e33a527`, DeLoContainers `ec2767a10356a259274aee9160d92b027403f03b`. Their `.service` files are unchanged. The agents live path `~/.agents` is itself the git checkout, contrary to the epic AC note; ledger records the correction.
- All four entries were added and planned with exact by-path rollback commands, then cut over using `candybar migrate`; the write-probe cutover waited through the near-fire guard and its native in-progress run. Capture-sweep's first scheduled fire at 17:30Z succeeded with metering and was verified in the ledger; another scheduled fire succeeded at 17:41Z. Manual runs of capture-sweep, write-probe and memory-analysis succeeded with metering. Write-probe's `last-success` stamp was touched at 13:28:59 EDT and its infra-doctor 2h deadman remains configured.
- Template-sweep was already failing natively at 04:50 EDT because `op run` could not retrieve the missing `DeLoSecrets/Hypercharm` item. Its one manual candybar run also failed, but on a **different** lookup: `op run` reported two items titled `DeLoSecrets/Hindsight` at 13:28:53 EDT. That is not the identical failure required for result parity. Immediately ran `candybar migrate rollback`; the native timer is again enabled and active, the candybar is disabled, and the ledger says `rolled-back` with the reason. No second manual run; fixing the vault contents is outside this story.
- Three jobs remain migrated: capture-sweep `verified`, write-probe and memory-analysis `cutover` pending their first scheduled fires at 18:04Z and 2026-10-27 09:00Z respectively. The fourth remains on its native timer. The wave is partial, not complete.
- The `plan --write` CLI could not be used to fill defaults: another wave held the directory lock, then replaced the ledger between this wave's initial append and the retry. The entries were re-appended under the exact same directory lock, and `unit_file_of`/`default_commands`/`LedgerEntry.record`/`save_ledger` were used to fill the by-path commands atomically. A subsequent read-only `candybar migrate plan` validated all four entries and showed the correct active native states and guards. No native scheduler was touched by that recovery.

## Spec Change Log

## Review Triage Log
