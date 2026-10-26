---
title: 'Story 3.3: Token spend counter and full exemplar coverage'
type: 'feature'
created: '2026-10-26'
status: 'in-progress'
route: 'dispatch'
baseline_commit: '8c45a74cce1b167f6dacf1d4eadc342d67e30dcf'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-3-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** A candybar's self-reported `metrics.token_spend` already reaches the `invocations` row and the terminal event, but Prometheus never sees it, so token burn per candybar cannot be queried. Nothing guards that every CAP-3 family carries the `invocation_id` exemplar. And the per-invocation record a scraped exemplar points at cannot be read back through the API.

**Approach:**
- Add a `candybar_token_spend_total` counter on `{candybar_id, result}`. The executor increments it once per execution, by the resolved `token_spend` (0 for bare executables, non-LLM jobs and invalid result files), always with the `invocation_id` exemplar.
- Add a guard test: every exemplar-capable family (counter or histogram) carries the exemplar on every observation, and the only family without one is the memory gauge, whose histogram twin carries it.
- Add read routes for the stored record: `GET /invocations/{invocation_id}`, and `GET /candybars/{candybar_id}/invocations?limit=N` (newest first). Each returns runtime, memory peak, CPU load, GPU load, token spend and `cost_usd`, plus the row's identity, trigger and result fields.

## Boundaries & Constraints

**Always:**
- Labels stay `{candybar_id, result}`, and both result series materialize at zero on first sight.
- The counter moves once per execution, at the same point as the other families: after the terminal row commits. It never moves on redelivery or republish, on a lost same-key race, or for an unknown candybar.
- A metric failure never fails an invocation.
- A failed invocation's self-reported tokens still count, on `result="fail"`, because the tokens were really spent.
- The API speaks problem+json: an unknown id returns 404, a malformed id or bad `limit` returns 422, and a database failure returns 500.
- `cost_usd` is null when not reported and a JSON number when it is.

**Never:**
- No new migration or event schema change; the columns and event fields already exist (1.4, 1.6).
- No cost counter, because the spine's AD-9 family list names only the token-spend counter.
- No per-invocation label.
- No Epic 5 surface (`GET /candybars`, `/schedule`, SSE).
- No change to what a child writes (AD-8).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| LLM job | result file `metrics.token_spend: 1200, cost_usd: 0.03`, success | counter success +1200 with the exemplar; the row and the API show 1200 and 0.03 | N/A |
| Bare executable | no result file, exit 0 | counter +0, exemplar still set; API tokenSpend 0, costUsd null | N/A |
| Failed LLM job | exit 1, result file reports 500 tokens | counter fail +500 | N/A |
| Invalid result file | exit 0, unparsable JSON | counter fail +0 | N/A |
| Redelivery | key already recorded | counter unchanged | N/A |
| API, known id | `GET /invocations/<id>` | 200 with the full record, camelCase | N/A |
| API, unknown or in-flight id | no terminal row yet | 404 problem+json | N/A |
| API, list | `GET /candybars/<id>/invocations?limit=2` | 200, newest 2 by `startedAt` | 404 if the candybar is unknown; 422 if limit < 1 or > 500 |

</frozen-after-approval>

## Code Map

- `src/candybar/executor/metrics.py` -- add the `TOKEN_SPEND_METRIC = "candybar_token_spend"` Counter (exposed as `_total`), an `observe(token_spend: int = 0)` kwarg (clamped at 0), `inc(token_spend, exemplar=...)`, and an entry in `_families()`. Update the module docstring and `__all__`.
- `src/candybar/executor/worker.py` `_observe` -- pass `token_spend=rec.token_spend`. `resolve_outcome` already picks the metrics, including the fail path. Do not change the record/publish ordering.
- `src/candybar/registry/store.py` -- add `get_invocation(session, id)` and `list_invocations(session, candybar_id, limit)`. The list is ordered `started_at desc, id desc` and served by the existing `invocations_candybar_started` index. Export both from `registry/__init__.py`.
- `src/candybar/api/app.py` -- add `invocation_row_json(row)` and the two GET routes, reusing `problem()`, `get_candybar` and `session_factory`; `limit` is a `Query(50, ge=1, le=500)`. Update the docstring.
- `deploy/prometheus/candybar-queries.test.yml` -- add token-burner queries (rate per candybar, topk), checked by the existing name/label drift guard in `tests/test_metrics.py`.
- `tests/test_metrics.py` `test_cardinality_guard...` -- add the new family to the expected set.
- `README.md` Metrics section -- add the counter row and queries, and document the invocation read routes.
- New tests: `tests/test_token_spend.py`, covering the counter, the full exemplar coverage guard, and the worker paths, reusing the fakes from `test_executor`/`test_metrics`; and `tests/test_api_invocations.py`, which monkeypatches the store lookups as in `test_api_invoke.py`. Extend `tests/test_registry_integration.py` with a live-PG round trip of the read functions.

## Tasks & Acceptance

**Execution:**
- [ ] `src/candybar/executor/metrics.py` -- add the counter family and the `token_spend` observe kwarg.
- [ ] `src/candybar/executor/worker.py` -- thread `token_spend` into `_observe`.
- [ ] `src/candybar/registry/store.py`, `registry/__init__.py` -- add the read functions.
- [ ] `src/candybar/api/app.py` -- add the two GET routes and the row serializer.
- [ ] `deploy/prometheus/candybar-queries.test.yml`, `README.md` -- add the token queries and the API docs.
- [ ] tests -- every matrix row, the exemplar coverage guard over all families, and the live-PG read round trip.

**Acceptance Criteria:**
- Given an SDK candybar reporting token_spend, when it completes, then `candybar_token_spend_total{candybar_id,result}` rises by exactly that amount and carries that invocation's `invocation_id` exemplar in an OpenMetrics scrape.
- Given any observation, when scraped as OpenMetrics, then the runtime, memory, token-spend, CPU and GPU families (and the invocation counter) each carry that `invocation_id` exemplar.
- Given a recorded invocation, when `GET /invocations/{id}` is called, then all five measures and both token fields match the stored row.

## Implementation Notes

## Spec Change Log

## Review Triage Log

## Design Notes

- The counter is incremented even by 0, so every observation carries an exemplar, as the AC says literally. A zero exemplar only overwrites the previous one within the same `{candybar_id, result}` series, and a candybar's reporting style is stable across its runs.
- The record route is top-level (`/invocations/{id}`) because an exemplar gives an operator only the invocation_id.
- In-flight invocations have no row (the terminal row is written first, 1.6), so they return 404. "Running" status belongs to Epic 5's SSE.

## Verification

**Commands:**
- `uv run ruff check . && uv run pytest -q` -- expected: all pass.
- promtool fixture (`docker run ... prom/prometheus:v3.5.0 test rules`) -- expected: SUCCESS.
- Registry integration on a throwaway postgres:17-alpine -- expected: pass.
