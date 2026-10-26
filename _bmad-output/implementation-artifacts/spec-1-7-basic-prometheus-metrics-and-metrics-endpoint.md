---
title: 'Story 1.7: Basic Prometheus metrics and /metrics endpoint'
type: 'feature'
created: '2026-10-26'
status: 'in-progress'
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
- [ ] `src/candybar/executor/metrics.py` + `__init__.py` -- recorder with fixed label set and exemplars.
- [ ] `src/candybar/executor/worker.py` -- record once after commit; guarded.
- [ ] `src/candybar/api/app.py` -- negotiated `/metrics`.
- [ ] `src/candybar/daemon.py` -- executor-without-api warning.
- [ ] `tests/test_metrics.py` -- full I/O matrix + cardinality guard + exposition tests.
- [ ] `deploy/prometheus/candybar-queries.test.yml` + `ci.yml` + `README.md` -- PromQL proven by promtool.

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

## Verification

**Commands:**
- `uv run pytest -q` -- all green.
- `uv run ruff check .` -- clean.
- `docker run --rm -v $PWD/deploy/prometheus:/w --entrypoint promtool prom/prometheus:v3.5.0 test rules /w/candybar-queries.test.yml` -- SUCCESS.

**Manual checks (if no CLI):**
- Live smoke: daemon `--roles api,executor` on the live bus, invoke a smoke candybar twice (one success, one fail), throwaway Prometheus with exemplar storage scraping it; `query_exemplars` returns the invocation ids and the rate query returns both result series.

## Implementation Notes

## Spec Change Log

## Review Triage Log
