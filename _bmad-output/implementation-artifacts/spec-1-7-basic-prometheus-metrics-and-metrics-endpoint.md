---
title: 'Story 1.7: Basic Prometheus metrics and /metrics endpoint'
type: 'feature'
created: '2026-10-26'
status: 'done'
baseline_commit: '99d03defd6c32182959cf69c134399b4515f6107'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The executor records every invocation's result and wall-ms runtime in Postgres, but nothing reaches Prometheus: an operator cannot chart what ran, how long it took, or what failed per candybar.

**Approach:** The executor records a wall-ms runtime histogram and an invocation counter on series labeled `{candybar_id, result}` only, attaching an `invocation_id` exemplar to every observation (AD-9); the process serves them at `GET /metrics` with OpenMetrics content negotiation so exemplars survive the scrape.

## Boundaries & Constraints

**Always:** Series labels are exactly `{candybar_id, result}` (`result` ∈ `success|fail`, the registry vocabulary); per-invocation identity rides ONLY as the `invocation_id` exemplar. Record metrics once per execution — after the terminal row commits (`inserted` is true), never on the redelivery/republish path, never for unknown candybars (no row, no execution, no unbounded ids). Metric recording never fails an invocation (errors logged and swallowed). `/metrics` negotiates via `prometheus_client.exposition.choose_encoder(Accept)` — OpenMetrics (with exemplars) when Prometheus asks for it, text 0.0.4 otherwise. Metrics live on the process-default `CollectorRegistry` so the api role exposes what the executor role records in the same process (AD-2); tests inject their own registry.

**Never:** No memory/CPU/GPU/token-spend families (Epic 3), no `name` or `trigger` label, no per-invocation label of any kind, no new HTTP port or second server, no Prometheus/Grafana deployment changes (Story 1.9), no registry/migration changes.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Success | Invocation recorded `success`, 1234 ms | counter `{id,success}` +1, histogram observes 1234, both with `invocation_id` exemplar | N/A |
| Failure | Invocation recorded `fail` (nonzero exit / spawn failure) | counter `{id,fail}` +1, histogram observes its runtime_ms | N/A |
| Redelivery | Key already recorded | No metric change | N/A |
| Same-key race lost | `record` returns False | No metric change (winner already counted) | N/A |
| Unknown candybar | No registry row | No metric change | N/A |
| Record crash | `record` raises | No metric change; nak (redelivery records once) | Existing nak path |
| Recorder error | prometheus_client raises | Invocation still publishes + acks | Log, swallow |
| OpenMetrics scrape | `Accept: application/openmetrics-text` | 200, OpenMetrics body with `# {invocation_id="..."}` exemplars, `# EOF` | N/A |
| Plain scrape | No/other Accept | 200, `text/plain; version=0.0.4` body | N/A |

</frozen-after-approval>

## Code Map

- `src/candybar/executor/metrics.py` -- NEW. `ExecutorMetrics(registry=REGISTRY)`: `Counter("candybar_invocations", …, labelnames=SERIES_LABELS)` and `Histogram("candybar_invocation_runtime_milliseconds", …, buckets=ms buckets up to 1h)`; `observe(candybar_id, result, invocation_id, runtime_ms)` passes `exemplar={"invocation_id": str(id)}` to both. Module default `EXECUTOR_METRICS`.
- `src/candybar/executor/worker.py` -- `Executor.__init__` gains `metrics: ExecutorMetrics | None` (default `EXECUTOR_METRICS`); call it right after `inserted` is true in `handle_message` (line ~265), guarded. Do not touch `_republish_terminal` / `_fail_unknown_candybar`.
- `src/candybar/executor/__init__.py` -- export `ExecutorMetrics`.
- `src/candybar/api/app.py` -- `create_app(..., metrics_registry=REGISTRY)`; `GET /metrics` using `choose_encoder(request.headers.get("accept",""))`. Reuse `problem()` untouched.
- `src/candybar/daemon.py` -- warn when `executor` runs without `api` (recorded metrics are not exposed in that process).
- `deploy/prometheus/candybar-queries.test.yml` -- NEW promtool `promql_expr_test` proving the documented per-candybar success/fail rate and failure-ratio queries over series in the exporter's exact shape.
- `tests/test_metrics.py` -- NEW: recorder, cardinality guard (every `candybar_*` sample's labels minus `le` == `{candybar_id,result}`; no `invocation_id` label anywhere), exemplars present via OpenMetrics parse, worker integration (once per execution; none on redelivery/race/unknown/record-crash; recorder error swallowed), `/metrics` negotiation via TestClient, promtool-file names match exporter names.
- `tests/test_heartbeat.py`/`tests/test_executor.py` -- reuse fakes (`FakeMsg`, `FakeBus`, `FakeStore`, `make_command`, `make_row`, `make_outcome`) by import; do not change their behavior.
- `.github/workflows/ci.yml` -- run `promtool test rules` via the pinned `prom/prometheus:v3.5.0` image (matches live Prometheus 3.5.0).
- `README.md` -- Metrics section: names, labels, PromQL, exemplar-storage note.

## Tasks & Acceptance

**Execution:**
- [x] `src/candybar/executor/metrics.py` + `__init__.py` -- recorder with fixed label set and exemplars.
- [x] `src/candybar/executor/worker.py` -- record once after commit; guarded.
- [x] `src/candybar/api/app.py` -- negotiated `/metrics`.
- [x] `src/candybar/daemon.py` -- executor-without-api warning.
- [x] `tests/test_metrics.py` -- full I/O matrix + cardinality guard + exposition tests.
- [x] `deploy/prometheus/candybar-queries.test.yml` + `ci.yml` + `README.md` -- PromQL proven by promtool.

**Acceptance Criteria:**
- Given the executor completing invocations, when Prometheus scrapes `/metrics`, then `candybar_invocation_runtime_milliseconds` (histogram) and `candybar_invocations_total` (counter) exist on series labeled `{candybar_id, result}` only.
- Given an OpenMetrics scrape, when the body is parsed, then each observation's bucket/counter sample carries an `invocation_id` exemplar.
- Given any number of invocations, when the cardinality guard runs, then no series carries a per-invocation label.
- Given the documented PromQL, when promtool evaluates it over exporter-shaped series, then it yields per-candybar success and fail rates and the failure ratio.

## Design Notes

Units follow the AC literally (wall ms), so the histogram name carries `_milliseconds` rather than the Prometheus seconds convention; buckets `10,50,100,250,500,1e3,2.5e3,5e3,1e4,3e4,6e4,3e5,6e5,1.8e6,3.6e6`. `/metrics` rides the api role's existing uvicorn server (one port, one Traefik route in 1.9). Prometheus stores exemplars only with `--enable-feature=exemplar-storage`; the live Prometheus (3.5.0) does not set it — wiring that and the scrape job is Story 1.9's deploy scope.

Queries (promtool-proven):
```promql
sum by (candybar_id, result) (rate(candybar_invocations_total[5m]))
sum by (candybar_id) (rate(candybar_invocations_total{result="fail"}[5m]))
  / sum by (candybar_id) (rate(candybar_invocations_total[5m]))
```

Decisions where the spine is silent: `/metrics` lives on the api role, so an executor-only process logs a warning that its series are not exposed. Metrics are observed after the row commits and before the terminal publish, which keeps them consistent with the registry counters: a failed publish is retried from the row and never observed twice. Both result series start at zero.

## Verification

**Commands:**
- `uv run pytest -q` -- all green.
- `uv run ruff check .` -- clean.
- `docker run --rm -v $PWD/deploy/prometheus:/w --entrypoint promtool prom/prometheus:v3.5.0 test rules /w/candybar-queries.test.yml` -- SUCCESS.

**Manual checks (if no CLI):**
- Live smoke: daemon `--roles api,executor` on the live bus, invoke a smoke candybar twice (one success, one fail), throwaway Prometheus with exemplar storage scraping it; `query_exemplars` returns the invocation ids and the rate query returns both result series.

## Implementation Notes

I implemented this inline because this session has no subagent-spawn tool (same as 1.1–1.6). Every matrix row has a passing test in `tests/test_metrics.py` (25 tests). The full unit suite passes (239), ruff is clean, promtool reports SUCCESS, and the live integration tests pass (11) on throwaway postgres:17-alpine and nats:2.10.29 containers. I never pointed the suite at the live database.

Surprises and resolutions:

- **A never-failed candybar returned no data for the failure-ratio query.** promtool caught it on the first run: no `result="fail"` series exists, so the division is empty. `ExecutorMetrics.observe` now creates both result series at zero the first time it sees a candybar. That costs two series per candybar, so the cardinality stays bounded. The ratio now reads 0, and the promtool fixture includes that zero series.
- **Prior-story bug: the registry counters lost updates.** The live smoke ran 5 invocations of one candybar. All 5 rows were written, and `candybar_invocations_total` showed 5, but `total_invoked` was 3. `record_terminal_invocation` (Story 1.6) increments with an ORM read-modify-write (`+= 1`), and the parallel asyncio tasks overwrote each other under READ COMMITTED. The fix is an atomic `UPDATE … SET total = total + 1` in the same transaction, which also locks the candybar row until commit. I committed it separately as `7595742`. A new live regression test sends 12 concurrent records and checks that a duplicate key rolls back its increment. Without the fix the test fails with "assert 1 == 12". With the fix, a second smoke burst of 6 + 4 invocations gave counter deltas that match the metrics exactly.
- **Exemplar retention is set by the protocol.** prometheus_client keeps only the most recent exemplar per counter or bucket between scrapes. Every observation still carries its `invocation_id`. Prometheus keeps one exemplar per scrape per series, and the full per-invocation record stays in `invocations` (AD-9).

Live smoke on throwaway NATS + Postgres + prom/prometheus:v3.5.0 with `--enable-feature=exemplar-storage`:

- I registered two candybars through `candybar register` and published 9 manual invoke commands.
- The target was `up`. `sum by (candybar_id,result)` returned all four series, and the success candybar's fail series read 0.
- The failure ratio was 1 for the failing candybar and 0 for the other. The p95 was 487.5 ms for the 300 ms job.
- `query_exemplars` returned 3 distinct `invocation_id`s per series.

I tore all of it down afterwards.

## Spec Change Log

## Review Triage Log

I ran the review inline because no subagent-spawn tool was available (same deviation as 1.1–1.6). It applied the blind, edge-case (path tracing plus claims), and verification-gap checks to `/tmp/story-1-7-diff.txt` (41.8 kB). Finding floor N = min(floor(sqrt(41.8) + 1), 10) = 7.

- `api/role.py` and `api/__init__.py` docstrings still said "POST /candybars only", so the role docs no longer matched the endpoint set. Verdict: low. Patched as a direct correction.
- README's CI paragraph did not mention the new promtool step. Verdict: low. Patched as a direct correction.
- Verification gap: `record_terminal_invocation` now runs the UPDATE before the INSERT, and no test checked that a duplicate-key failure rolls back the counter bump. Verdict: medium. Patched: the live race test now records a duplicate key, expects DuplicateInvocationError, and checks that the counters are unchanged at (12, 8).
- Verification gap: no test checked that an `Executor` built without `metrics=` (as the executor role builds it) records into the instance that `/metrics` serves. Verdict: low. Patched with `test_executor_defaults_to_process_metrics`, a one-assertion test.
- Histogram/counter exemplars keep only the last observation per bucket between scrapes, which could look like a breach of "each observation carries an exemplar". Verdict: false. Every `observe`/`inc` call passes an exemplar, and keeping only the latest is how OpenMetrics exemplars work. AD-9 puts the full per-invocation record in the `invocations` table.
- Unknown-candybar failures and redeliveries produce no metric, so failures of an unregistered id are not counted. Verdict: false. The frozen matrix requires this: an unregistered id has no row and would add unbounded label values. The `failed` fact is still emitted.
- `/metrics` encodes synchronously on the event loop. Verdict: low. Rejected because collection covers tens of series per candybar and takes microseconds, and the fix would add thread offload.
- Live Prometheus has no `exemplar-storage` flag and no candybar scrape job. Verdict: medium. Deferred, because the frozen intent excludes deploy changes and this belongs to Story 1.9. Recorded in deferred-work.md.
- On SIGTERM the executor role's `nc.drain()` waits out the 30s DrainTimeoutError. Verdict: medium. Deferred because it is pre-existing (1.6) and was found during the smoke. Recorded in deferred-work.md.
- Found during implementation rather than review: the prior-story lost-update race in the registry counters. Verdict: high. Fixed in `7595742` with a regression test; see Implementation Notes.

Zero bad_spec or intent_gap entries, so no loopback was needed.

## Independent Review (2026-10-26)

Fresh-context reviewer (not the implementer), bmad-code-review over the story's commits and the CURRENT tree (later epics reshaped shared modules). No subagent capability in this session, so the blind / edge-case / verification-gap / acceptance layers ran inline. Evidence classes: implemented + tested = unit suite 749 passed (`uv run pytest -q`), ruff clean; deployed = container rebuilt and redeployed per deploy/README.md after the patches, `/healthz` 200, next tick `tick:29883724` fired `deploy-smoke` (success, 19ms).

### Review Findings

- No `decision-needed`, `patch` or `defer` findings. Series carry exactly `{candybar_id, result}`, every counter/histogram observation stamps the `invocation_id` exemplar, metrics are observed once after the terminal row commits (never on redelivery), and `/metrics` negotiates OpenMetrics via `choose_encoder` (tests/test_metrics.py). The live scrape + exemplar storage landed with 1.9.
- Rejected: counters restart from zero on each redeploy — false as a defect: Prometheus `rate()` handles counter resets by design.
