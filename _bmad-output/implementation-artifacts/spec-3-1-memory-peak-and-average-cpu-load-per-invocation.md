---
title: 'Story 3.1: Memory peak and average CPU load per invocation'
type: 'feature'
created: '2026-10-26'
status: 'in-progress'
baseline_commit: 'e3b2e62e9fc8624e419ca0df884f2962f361da8a'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-3-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The executor runs each invocation in a cgroup v2 scope but never reads it, so an operator cannot tell which candybars hog memory or burn CPU. The production container also disables scoping (`CANDYBAR_CGROUP_ROOT=""`), so nothing could be measured there anyway.

**Approach:** Read `memory.peak` and the `cpu.stat` `usage_usec` delta from the invocation's scope after the child exits, with average CPU load = usage / wall runtime. Observe both on `{candybar_id, result}` Prometheus series with `invocation_id` exemplars, store them on the `invocations` row (alembic 0002), and carry them in the `completed`/`failed` event `data.metrics`, including on the republish path. Restore real per-invocation scoping in the container: run it privileged, and have the entrypoint nest the daemon in a leaf cgroup and delegate `+memory +cpu` before dropping to uid 10001.

## Boundaries & Constraints

**Always:**
- Labels are exactly `{candybar_id, result}`. Metering never fails an invocation: missing or unreadable files, or no scope, give 0, which is the spine's "unmeasured = zero".
- The executor measures these values; a child can never self-report them (the result file keeps accepting only `token_spend`/`cost_usd`).
- Event schema change is additive and optional within revision 1, made canonical in bloodbank first, then copied byte-identical to `schemas/cron/`.
- Both result series materialize at zero for every new family.
- An explicitly empty `CANDYBAR_CGROUP_ROOT` still disables scoping, and a non-privileged container degrades to unscoped with a log line.

**Never:**
- No GPU (3.2), no token counter or API read surface (3.3).
- No per-invocation label.
- No pre-exec spawn shim: the child joins its scope right after spawn, as in 1.6.
- No new schema revision, subject, or event type.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Scoped success | scope has memory.peak=P, usage_usec delta U, runtime R ms | row/event/metrics: memory_peak_bytes=P, cpu_load=U/(R·1000) | N/A |
| Scoped failure | nonzero exit, scope readable | same measures on the `fail` series, row, and `failed` event | N/A |
| Unscoped | root "" or scope entry failed | memory 0, cpu 0, observed as zero | N/A |
| No memory controller | scope lacks memory.peak | memory 0, cpu still measured from cpu.stat | log once per process |
| Runtime 0 ms | R=0 | cpu_load 0 (no division by zero) | N/A |
| Spawn failure | child never started | measures 0 | existing path |
| Redelivery | key recorded | republished event carries the stored row's measures; no metric change | N/A |
| Canary | child allocates 64 MiB in a real scope | 64 MiB ≤ peak ≤ 64 MiB + 64 MiB | env-gated test |

</frozen-after-approval>

## Code Map

- `src/candybar/executor/runner.py` -- `_enter_scope`/`_leave_scope`/`run_invocation`/`RunOutcome`. Read the scope between `communicate()` finishing and `_leave_scope`. Take the cpu baseline right after `_enter_scope`. `ResultFile.metrics` uses the self-reported model.
- `src/candybar/executor/worker.py` -- `Resolved.metrics`, `TerminalInvocation` construction (line ~257), `terminal_event_for` (completed AND failed must carry metrics), `record_from_row`, `_observe`.
- `src/candybar/executor/metrics.py` -- `ExecutorMetrics.observe`; add families on the same pattern (zero-materialize both results).
- `src/candybar/events/envelope.py` -- `InvocationMetrics` (extra=forbid) is shared with the result file today. Split it: `SelfReportedMetrics` (token_spend, cost_usd) and `InvocationMetrics(SelfReportedMetrics)` (+memory_peak_bytes, +cpu_load). Add `metrics` to `CandybarFailedData`.
- `src/candybar/events/builders.py` -- `build_candybar_failed` gains `metrics`.
- `src/candybar/registry/{models,store}.py` + `deploy/alembic/versions/0002_*` -- two NOT NULL columns, default 0; `TerminalInvocation` gains defaulted fields.
- `schemas/cron/candybar.{completed,failed}.json` + `../bloodbank/schemas/bloodbank/cron/` -- canonical first (`mise run validate:schemas` in bloodbank).
- `deploy/{compose.yml,container-entrypoint.sh}`, `Dockerfile` -- privileged, entrypoint as root, `setpriv` drop.
- Tests: `tests/test_executor.py` (`make_outcome`, plain-fs cgroup doubles), `tests/test_metrics.py` (promtool name guard), `tests/test_events.py`, `tests/test_registry_integration.py` (column set), `deploy/prometheus/candybar-queries.test.yml`, `.github/workflows/ci.yml`.

## Tasks & Acceptance

**Execution:**
- [ ] `src/candybar/executor/metering.py` -- new: best-effort `enable_controllers(root)`, `read_memory_peak(scope)`, `read_cpu_usage_usec(scope)`, `cpu_load(delta_usec, runtime_ms)` -- pure, unit-testable.
- [ ] `src/candybar/executor/runner.py` -- enable controllers once per root, take the baseline, measure, and add `RunOutcome.memory_peak_bytes`/`cpu_load` (defaults 0).
- [ ] `src/candybar/events/{envelope,builders,__init__}.py` -- the metrics model split and `metrics` on failed.
- [ ] `../bloodbank/schemas/bloodbank/cron/candybar.{completed,failed}.json` then `schemas/cron/` -- additive optional properties.
- [ ] `src/candybar/registry/models.py`, `store.py`, `deploy/alembic/versions/0002_invocation_resource_metering.py` -- columns plus the round trip.
- [ ] `src/candybar/executor/worker.py` -- thread the measures through the record, events, republish, and observe.
- [ ] `src/candybar/executor/metrics.py` -- the gauge, memory histogram, and CPU-load histogram.
- [ ] deploy files + `ci.yml` -- container cgroup nesting. The CI boot smoke runs privileged and asserts delegated controllers; a CI step runs the real-cgroup canary as root.
- [ ] tests + promtool fixture + README Metrics table/queries.

**Acceptance Criteria:**
- Given a real delegated cgroup root, when a canary allocating 64 MiB completes, then its row, event, and gauge report a peak inside the matrix band, and a CPU-burning canary reports cpu_load in [0.5, 1.5].
- Given an OpenMetrics scrape after an invocation, then the memory histogram and CPU-load histogram buckets carry `invocation_id` exemplars, and label sets are exactly `{candybar_id, result}`.
- Given the live container redeployed, when the deploy-smoke candybar fires, then its new invocations row has memory_peak_bytes > 0.

## Design Notes

prometheus-client 0.26 (and OpenMetrics 1.0) allows exemplars only on counters and histogram buckets. That is why the families are shaped as follows:
- `candybar_invocation_memory_peak_bytes` is a **gauge**. It holds the latest invocation's peak, which is the spine's "memory max gauge"; `max_over_time` finds hogs.
- `candybar_invocation_memory_bytes` is a **histogram** of per-invocation peaks. It carries the exemplars.
- `candybar_invocation_cpu_load_ratio` is a **histogram**. It is a strict superset of the client's Summary, which only exposes `_sum`/`_count`, and it is the "CPU load summary" that can carry exemplars.

`cpu_load` is the average number of busy cores over wall runtime (1.0 = one core).

## Verification

**Commands:**
- `uv run ruff check . && uv run pytest -q` -- expected: all pass.
- `sudo -E env PYTHONDONTWRITEBYTECODE=1 CANDYBAR_TEST_CGROUP_ROOT=/sys/fs/cgroup/candybar-test .venv/bin/python -m pytest -p no:cacheprovider tests/test_cgroup_metering_integration.py` -- expected: canary bands pass.
- Throwaway postgres:17 + nats:2.10.29 containers: the PG/NATS integration suites -- expected: pass (never the live DB).
- promtool on the fixture -- SUCCESS; bloodbank `mise run validate:schemas` -- consistent.

## Implementation Notes

- Planning gates (unattended run, recorded): spec is ~1.9k tokens (> 1600) — kept full: the container-scoping task is the precondition that makes the metering real in production, not a separable goal. No Open Questions: every choice was settleable from code/spine and is recorded in Intent/Design Notes. Checkpoint 1 auto-approved (Approve and continue) per the workflow harness's no-questions directive.
- Concurrency: stories 2.1/4.1/4.2 run in the SAME working tree. Shared files (`worker.py`, `builders.py`, `test_executor.py`) are staged as HEAD + this story's edits only; new tests live in `tests/test_metering.py`.

## Spec Change Log

## Review Triage Log
