# CAND-50 independent HOLD repair — verification

Status: implemented and locally verified; **CAND-50 remains in progress pending parent independent review**. Scope excludes CAND-49 activation paths.

## Corrections

- F1: a durable `.pending.json` outbox contains the exact transition payload, route and candidate state. File and directory are fsynced before transport. Each invocation takes the existing resolved-state lock, replays old pending work before detection, commits its candidate after confirmation, then detects current health. Transient alerts therefore survive recovery and recovery is separately ordered. Persistent rejection, interruption, malformed/unreadable pending data and route mismatch never silently discard pending work. Delivery is **at least once**, not exactly once; acceptance before local commit/removal can duplicate.
- F2: the protected rollback exception binds exact native timer, canonical ledger id and API UUID. Audit reads this checkout's ledger fresh and serializes against migration mutation using its directory lock; live mode refreshes registry while holding the same lock. Only rolled-back, `disable_pending=false`, exact disabled registry/runner evidence passes. Enabled owner is duplicate-clock failure; absent, malformed or unavailable evidence is unknown. Policy/env/inventory ledger overrides cannot fabricate canonical evidence.
- Existing availability-first rollback is intentionally unchanged: native-first restore during API-only outage can leave both clocks enabled. It is not a strict single-clock guarantee. Documentation and tests require failure/actionable unknown until disable completion.

## Executed checks

- Focused transition/doctor/drift/migration suite: **145 passed, 1 skipped**.
- Independent probes, separate scratch copy with defect assertions changed to normative correctness: **12 passed**. Original reviewer file unchanged.
- Exact checked-in CI scheduling-policy shell block: **68 passed**, clean/repo pass, rogue fixture exits 1. No hosted CI claim.
- Ruff, `git diff --check`, deploy hygiene, and AD-22 independent-watchdog gate: pass.
- Read-only live doctor: **exit 2 / unknown**, repo pass; **9 violations, 1 unknown**. Inventory counts: 81 registry, 54 user timers, 26 system timers, 11 crontab, 15 Hermes. The unknown is external Hermes job `777c511a009c` without an enabled Candybar owner; left untouched. Exact rolled-back allowance timer evidence independently verified disabled; no provider/quota/model contents sent.

## Runtime boundary

Read-only API runner readback points both enabled watchdog/drift jobs at this checkout's `.venv/bin/python -m candybar.transition_exec`; editable imports resolve to corrected source. Each exec loads source on its next invocation, so no exec daemon/gateway restart or scheduler mutation is required. Legacy watchdog `061c4dbedc1c` remains disabled, paused and external. No live trigger, alert send, induced failure, detector-state mutation or legacy enable was performed. This is source/install-path evidence, not a claimed post-repair live delivery receipt.

## Evidence

Absolute directory: `/home/delorenj/.hermes/profiles/33god-pm/cache/scratch/cand50-repair-o4h474yp`.

- `final-focused-tests.txt`, `final-independent-corrected.txt`
- `test_independent_cand50_corrected.py` (normative correction of reviewer probes)
- `ci-policy-block.sh`, `ci-policy-block.txt`, `final-lint-hygiene.txt`
- `live-doctor.json`, `live-doctor-summary.json`, `runtime-readback.json`, `repair-report.json`

Committed regression tests: `tests/test_cand50_repairs.py`. Full suite not rerun. Parent performs independent re-review before acceptance; no approval/merge claim.
