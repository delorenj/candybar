---
title: 'CAND-49: scoped crank monitor-ack activation'
type: 'bugfix'
created: '2026-10-29'
status: 'in-progress'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: 'c78b345608eceeab1a1d8d558b03dc393881f8a5'
---

<frozen-after-approval reason="operator delegated bounded activation with exact candidate">

## Intent

Activate the independently reviewed Hermes monitor acknowledgement repair only for the existing Candybar crank-beat job, and prove its actual execution provenance. Use the existing exec runner's AD-8 boundary as a scoped adapter to the unchanged Hermes cron CLI durable-wait contract. This is execution-image selection, not another fleet installation or scheduler.

## Boundaries & Constraints

Always retain Candybar identity, schedule, timezone, existing Hermes external pin, profile home, delivery, provider/auth and admission caps. Candidate is exact archived commit `3509618461f7831f43ac0c53f219f35492861647`, archive SHA-256 `1bf0e82725e0e2e5faed430d25713f6b3c6582ff1e0c9253c5aba1ca9799e3cb`. Use the one canonical Hermes interpreter, no additional venv or checkout installation. Preserve canonical Candybar result parser/status/token semantics, and independently label refusal/no-agent outcomes so transport completion never certifies crank attendance.

Never restart the shared gateway or executor, globally override CANDYBAR_HERMES, change another Candybar job, change watchdog/policy files owned by the other worker, create recurring clocks, bypass admission, or send test notifications. A real manual invoke of the existing Candybar job and one subsequent scheduled receipt are authorized. No merge approval is granted.

## I/O & Edge-Case Matrix

| Scenario | Input/state | Required behavior | Error handling |
|---|---|---|---|
| CLI terminal completion | durable completed receipt | canonical success/tokens, correlated execution id | retain process exit primacy |
| CLI failure/unknown/malformed | terminal failure or bad stdout | failed adapter, no invented usage | fail closed for absent result |
| economics refusal | no agent constructed | pending/baseline retained by reviewed scheduler; receipt explicitly no-agent | no new quota notification route |
| monitor stable | source no change | distinguish suppression from agent work | no attendance claim |
| wrong job/profile/image | mismatched identity or module hash | refuse before invoking | nonzero + AD-8 fail |
| cutover | existing runner changes | short disable/patch/readback/re-enable; same schedule | restore original runner on failed transaction |

</frozen-after-approval>

## Code Map

- `src/candybar/executor/hermes_runner.py`: reuse `parse_result_line`; status completed maps success and input+output tokens clamp to schema max.
- `src/candybar/executor/runner.py`: existing exec runner meters the complete process tree and accepts result JSON via CANDYBAR_RESULT_FILE; timeout remains CLI wait + 60s.
- `deploy/migration/manifests/crank-beat/candybar.toml`: existing schedule and runner definition; only this manifest changes.
- Canonical Hermes `hermes_cli/cron.py`, `cron_wait.py`: use `cron run --wait`, never trigger primitive directly.
- Reviewed `cron.scheduler`: ordinary CLI run executes run_one_job in the CLI process (not shared gateway); actual entry and exit profiling records PID/module hashes/job snapshot without changing scheduler functions.
- `hermes-single-install-check.py`: all fleet consumers remain canonical; source image is read-only profile runtime data, with no venv, git metadata, PATH symlink or service.

## Tasks & Acceptance

- [ ] `deploy/scoped/crank-beat/launch.py`: stdlib parent/worker adapter, canonical parser and durable per-invocation provenance; no scheduler implementation.
- [ ] `deploy/scoped/crank-beat/activate.py`: exact source promotion and transactional supported REST activation; rollback readback.
- [ ] `tests/test_scoped_crank.py`: parser/exit/identity/admission classification and worker ABI tests.
- [ ] `deploy/migration/manifests/crank-beat/candybar.toml`: narrow exec runner, same job and timeout.
- [ ] `_bmad-output/implementation-artifacts/cand-49-scoped-activation.md`: live correlated receipts, unchanged settings, rollback and limits.

Acceptance: Given reviewed archive, when activated through existing Candybar, then actual scheduler/monitor/economics/agent/gateway module paths and hashes resolve in durable candidate source. Given refusal, when completed, then pending remains and report does not claim a successful crank. Given next scheduled tick, when it fires, then a correlated Candybar/Hermes receipt proves the continuing path. Given rollback, when original runner is restored, then no second clock exists.

## Implementation Notes

Independent upstream candidate review PASS is recorded in active-profile scratch. g33 preflight PASS (legacy enrollment WARN). CodeGraph exists but semantic sidecar absent; source investigation used indexed file locations and direct reads. The operator's delegated activation authority covers the bounded transaction, not fleet rollout. Current Candybar main was clean at start; active CAND-49 owns existing crank migration.

## Verification

`uv run pytest -q tests/test_scoped_crank.py tests/test_hermes_runner.py` plus focused lint/diff checks. Real API invoke and bounded next-tick readback; single-install guard before and after. Snapshot/object hashes and execution ledger are read back, not synthesized.
