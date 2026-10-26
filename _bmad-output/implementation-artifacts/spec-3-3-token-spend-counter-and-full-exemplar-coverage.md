---
title: 'Story 3.3: Token spend counter and full exemplar coverage'
type: 'feature'
created: '2026-10-26'
status: 'done'
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
- [x] `src/candybar/executor/metrics.py` -- add the counter family and the `token_spend` observe kwarg.
- [x] `src/candybar/executor/worker.py` -- thread `token_spend` into `_observe`.
- [x] `src/candybar/registry/store.py`, `registry/__init__.py` -- add the read functions.
- [x] `src/candybar/api/app.py` -- add the two GET routes and the row serializer.
- [x] `deploy/prometheus/candybar-queries.test.yml`, `README.md` -- add the token queries and the API docs.
- [x] tests -- every matrix row, the exemplar coverage guard over all families, and the live-PG read round trip.

**Acceptance Criteria:**
- Given an SDK candybar reporting token_spend, when it completes, then `candybar_token_spend_total{candybar_id,result}` rises by exactly that amount and carries that invocation's `invocation_id` exemplar in an OpenMetrics scrape.
- Given any observation, when scraped as OpenMetrics, then the runtime, memory, token-spend, CPU and GPU families (and the invocation counter) each carry that `invocation_id` exemplar.
- Given a recorded invocation, when `GET /invocations/{id}` is called, then all five measures and both token fields match the stored row.

## Implementation Notes

- Planning gates (unattended run, recorded):
  - The spec is about 1.5k tokens, under 1600.
  - There were no Open Questions. Every choice was settled from the code, the spine, or the view contract; the contract defines no invocation route, so the shapes are recorded in Design Notes.
  - Checkpoint 1 was auto-approved ("Approve and continue") under the workflow harness's no-questions directive.
  - Subagents are unavailable in this session, so implementation and review ran inline, the same deviation recorded for 1.1–3.2.
- g33 preflight PASS. The route was analysis/BMAD artifacts. No event types or schema revisions were added.
- Investigation: token_spend/cost_usd were already parsed (`SelfReportedMetrics`), resolved (fail-side included), stored (`invocations.token_spend`/`cost_usd`, migration 0001) and carried on events (1.6). This story only adds the Prometheus series and the API read, with no migration.
- Implementation commit `1e4d683`.
  - The counter is `candybar_token_spend_total`. `EXEMPLAR_LESS_FAMILIES` names the one family that cannot carry an exemplar, the memory gauge, and points at its twin.
  - The store gained `get_invocation`/`list_invocations` (`INVOCATION_LIST_MAX` 500).
  - The API gained `GET /invocations/{id}` and `GET /candybars/{id}/invocations`.
  - Also in this commit: the promtool token-burner group, the README, and tests. `tests/test_token_spend.py` includes a test that runs a real python-SDK child through the real runner. The other test files are `tests/test_api_invocations.py` and a live-PG round trip through the real routes.
- Review patches:
  - `ef7df23`: bounds the self-reported metrics.
  - `1736eda`: serializes API timestamps in UTC.
- Verification:
  - Unit tests: 506 passed, 33 skipped (the suite includes concurrent stories). Ruff is clean.
  - promtool reports SUCCESS.
  - Registry integration 9/9 on a throwaway postgres:17-alpine.
  - Executor, manual-invoke, fast-stop and python-SDK integration 5/5 on throwaway nats:2.10.29 + PG.
- Throwaway daemon smoke (api+executor roles):
  - An SDK job registered and was run twice, each reporting 1234 tokens and $0.0125. The counter showed success 2468, fail 0.
  - All six exemplar-capable families carried the same latest `invocation_id`.
  - `GET /invocations/<exemplar id>` returned 200, and the list came back newest first.
- Live redeploy (`docker compose up -d --build`, twice: once after the implementation, once after the UTC patch):
  - `candybar run` of `hello-node` (node SDK, 42 tokens) gave `candybar_token_spend_total` success 42 with that invocation's exemplar.
  - Live Prometheus reports the series (84 after a second run) and stores the exemplars (`query_exemplars`: 3 hello-node invocation ids at 42; deploy-smoke at 0).
  - `GET https://candybar.delo.sh/invocations/<id>` returns the full record in UTC.

## Spec Change Log

## Review Triage Log

Review layers ran inline (blind-hunter, edge-case-hunter, verification-gap), because this session cannot spawn subagents. The diff reviewed is commit `1e4d683`, 45.4 kB, so the blind-hunter floor is N = min(floor(sqrt(45.4)+1), 10) = 7.

| # | Layer | Finding | Verdict | Evidence / route |
|---|---|---|---|---|
| E1 | edge | A result file with `cost_usd` of `Infinity` or `1e400` passes validation: `json.loads` accepts it and pydantic's float allows inf. The row stores it, and then `GET /invocations/{id}` returns 500, because a JSON response cannot carry inf. | medium | Reproduced by `ResultFile.model_validate` returning `cost_usd=inf`. **patch** (`ef7df23`): `allow_inf_nan=False` on `SelfReportedMetrics.cost_usd`, so the run counts as an invalid result file. Parametrized test included. |
| E2 | edge | `token_spend` above 2^63-1 passes validation but fails the BIGINT insert. The executor naks it, and the redelivery re-executes the job every second, forever. | high | Pre-existing since 1.6, same root cause as E1 (unbounded self-report), and the fix is trivial. **patch** (`ef7df23`): `le=TOKEN_SPEND_MAX`. Tests cover the overflow case being rejected and int64 max being accepted. |
| L1 | live smoke | The live Postgres session zone is America/New_York, so `startedAt`/`finishedAt` (and the 1.4 `createdAt`) were emitted at `-04:00`. AD-13 and the view contract require UTC. | medium | Observed on the live API. Throwaway PG runs in UTC, which is why the integration test missed it. **patch** (`1736eda`): `utc_iso()` in both serializers, with unit tests using a -04:00 aware datetime. Live re-check: `+00:00`. |
| B1 | blind | `inc(0)` on a zero-token run overwrites the series' last (non-zero) token exemplar. | low | By design: the frozen AC requires an exemplar on every observation. The overwrite stays within one `{candybar_id, result}` series, and a candybar's reporting style is stable across runs (live: hello-node always 42, deploy-smoke always 0). **reject**. |
| B2 | blind | The SDK test set `PYTHONPATH` to the entire parent `sys.path`, which is unneeded: the shebang is the venv interpreter with the SDK installed. | low | Direct deletion. **patch** (`ef7df23`). The test still passes. |
| B3 | blind | The coverage guard reads the private `_name` and `_families()`. | low | prometheus-client is pinned (0.26.0), and `_families()` is the class's own list. **reject**. |
| B4 | blind | There is no offset or cursor pagination; only the newest 500 rows are reachable per candybar. | low | Window queries over history belong to Epic 5 (`/schedule`). An exemplar resolves by id regardless. **reject**. |
| B5 | blind | There is no CLI command to fetch an invocation record. | low | Not in the AC. curl against the route is documented. **reject**. |
| B6 | blind | Equal `started_at` rows have an arbitrary order. | false | The tiebreak `id desc` makes the order deterministic. |
| B7 | blind | The README says "every family carries the exemplar", but the gauge does not. | false | The next sentence states the gauge exception and its histogram twin. |
| B8 | blind | `stderrTail` on the API exposes unredacted stderr that events redact. | false | `redacted: true` on events means truncated to 500 characters. The row stores the same truncated tail, and no scrubbing exists anywhere. |
| C1 | claims | "Served by the existing `invocations_candybar_started` index" | false | `WHERE candybar_id = ? ORDER BY started_at DESC` is that composite index scanned backward. The `id` tiebreak only sorts ties. |
| V | verification-gap | No verification gaps found. | n/a | Tests catch the realistic regressions. Dropping the `token_spend` kwarg in `_observe` fails `test_llm_job_counts_spend_and_records_cost` (asserts 1200). A route or serializer regression against real rows fails `test_invocation_reads_round_trip_through_the_api`, which CI runs (`CANDYBAR_TEST_PG=1` step). A new metric family added without an exemplar fails `test_every_family_is_exemplar_covered_or_has_a_covered_twin`. |

### Independent review (fresh context, 2026-10-26)

A second reviewer, not the implementer, reviewed commits `1e4d683` + `ef7df23` + `1736eda` (diff 1235 lines) against the current tree. Layers ran inline because no subagent capability was available.
- AC 1 holds. Live, `hello-node` reports 42 tokens per run on `candybar_token_spend_total`.
- AC 2 holds. A live OpenMetrics scrape shows the runtime, memory, CPU, GPU, token and invocation families carrying `invocation_id` exemplars, and the coverage guard test passes.
- AC 3 holds. The live `GET /candybars/{id}/invocations` and `GET /invocations/{id}` return all five measures plus `tokenSpend`/`costUsd`, with UTC timestamps.
- Epic 3 suites: 215 passed, 3 skipped.

| # | Layer | Finding | Verdict | Evidence / route |
|---|---|---|---|---|
| R1 | edge | `_record` can raise after the commit actually landed (connection lost on the response). The command naks, redelivery republishes from the row, and that execution's tokens are never counted | low | It undercounts by one execution and never double counts, which matches the frozen "never on republish" rule. **reject**. |
| R2 | edge | A stopped (SIGTERM) child that already wrote a result file has its self-reported tokens dropped (`resolve_outcome` stopped path) | low | Pre-existing 1.6 precedence, and the run is a fail by executor stop. **reject**. |
| R3 | blind | `GET /candybars/{id}/invocations` has no cursor past the newest 500 | low | Same as B4. Exemplar lookups are by id. **reject**. |

Verdict: **done**. All ACs hold, and no defect needs a patch.

## Design Notes

- The counter is incremented even by 0, so every observation carries an exemplar, as the AC says literally. A zero exemplar only overwrites the previous one within the same `{candybar_id, result}` series, and a candybar's reporting style is stable across its runs.
- The record route is top-level (`/invocations/{id}`) because an exemplar gives an operator only the invocation_id.
- In-flight invocations have no row (the terminal row is written first, 1.6), so they return 404. "Running" status belongs to Epic 5's SSE.
- The list route is `GET /candybars/{id}/invocations` with `?limit=` (default 50, at most 500). It returns `{candybarId, invocations: [...]}`, newest first. Records use camelCase, matching `candybar_row_json`, and every timestamp is converted to UTC.
- A self-reported value outside what can be stored (BIGINT) or serialized (finite) makes the result file invalid, so the run is recorded once as failed. It is never a failed insert, which would re-execute the job.

## Verification

**Commands:**
- `uv run ruff check . && uv run pytest -q` -- expected: all pass.
- promtool fixture (`docker run ... prom/prometheus:v3.5.0 test rules`) -- expected: SUCCESS.
- Registry integration on a throwaway postgres:17-alpine -- expected: pass.
