---
title: 'Story 5.2: Schedule window endpoint with exact skip-aware fire times'
type: 'feature'
created: '2026-10-26'
status: 'done'
route: 'dispatch'
baseline_commit: '598721d5254b1b776a9340bbc7a7bbbccde46c85'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-5-context.md'
  - '{project-root}/_bmad-output/specs/spec-cron-framework/holocene-view-contract.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The Schedule tab of the delegated Holocene view needs ordered past/current/upcoming rows for a time window, with exact upcoming fire times that honor operator skips, and past results. The API has no schedule read at all: fire times are computed only inside the dispatcher.

**Approach:** Add `GET /schedule?from=<ts>&to=<ts>`, which returns the contract row `{candybarId, name, schedule, fireTime, state, lastResult?, skipped, noise}` for every fire of every registered candybar in the window, ordered by fire time. Fires are computed with the dispatcher's own croniter UTC helpers (`dispatcher/schedule.py`), with registry skips applied through the same `SkipSet`. Past rows take `lastResult` from the `invocations` row for that fire's idempotency key. The pure row computation lives in one module so 5.3 and 5.4 can reuse it.

## Boundaries & Constraints

**Always:**
- The window is half-open, `from <= fireTime < to`. Both are required ISO 8601 timestamps with an offset; any offset is converted to UTC and a naive timestamp is a 422. Rows are ordered by `(fireTime, name)`, and the response is a bare JSON array.
- Rows come from the CURRENT definition: every fire of each candybar's `schedule` in the window, at or after its registration minute (`ceil(created_at)`). Manual invocations are not schedule rows.
- `cut = floor_minute(now) + 1 minute`, where `now` is the API's wall clock (injectable). A fire before `cut` is `past`; a fire at or after `cut` is `upcoming`, except the single `current` row.
- `current` is the global next-to-fire: the earliest non-skipped fire at or after `cut` across all candybars, with ties broken by row order. At most one row is `current`. It is exactly one whenever that fire lies in the window. A skipped fire is never `current`. A window that ends at or before `cut`, or that starts after an earlier non-skipped fire, has no `current` row.
- `skipped` is true when an operator skip (`occurrence` or `rest_of_day`) covers the fire and no invocation was recorded for it. A skip stored after a fire already ran does not rewrite history.
- `lastResult` (`success`|`fail`) appears only on past rows that have a terminal invocation keyed `candybar:<id>:tick:<tick_id of fireTime>`, the key that scheduled and misfire runs share. Otherwise the key is omitted, never null.
- Every timestamp is ISO 8601 UTC. Every error is RFC 7807: 422 for missing, unparsable or naive bounds, `to <= from`, a span over 31 days, or more than 20,000 rows; 500 when the database is down.
- DB access goes only through `registry/store.py`. The CPU-bound fire computation runs off the event loop, because the daemon's roles share one loop.

**Never:**
- No migration, no new event type, no write path, and no change to the dispatcher's dispatch decisions.
- No `/schedule/next` (5.3), SSE (5.4), noise endpoints (5.5) or skip endpoints (5.6).
- No pagination, filtering, or history reconstructed under an earlier schedule.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Day window | `*/5` A + `*/15` B, now 10:12:30, window 10:00–10:30 | fires ≤10:10 past; A@10:15 current (name tie-break over B@10:15); rest upcoming | n/a |
| Skip on next fire | occurrence skip A@10:15 | A@10:15 `skipped:true` upcoming; B@10:15 current | n/a |
| Rest of day | day skip on the date | every fire that date `skipped:true`; current is the next day's first non-skipped fire if in window | n/a |
| Past results | scheduled success, misfire fail, manual run | `lastResult` on the two keyed fires; no row for the manual run; missing result → key omitted | n/a |
| Skip after run | skip covers a past fire that has a result | `skipped:false`, `lastResult` present | n/a |
| Past window | `to` ≤ cut | all past, no current | n/a |
| Future window | unskipped fire between cut and `from` | no current; if all such fires are skipped, the first window candidate is current | n/a |
| Registered mid-window | created 10:07 | no rows before 10:10 for it | n/a |
| Odd bounds | `from` 10:00:30, `to` 10:30:30, `-04:00` offset | first fire 10:05, 10:30 included, offset converted | n/a |
| Empty registry | none | `[]` | n/a |
| Bad bounds | missing/naive/garbage, `to<=from`, >31d, >20,000 rows | — | 422 problem+json |

**Decisions (unattended run; spine and contract silent):** wall clock rather than the dispatcher cursor defines past and current, because countdowns are computed client-side from `fireTime`. `current` is global, which is what 5.3 and 5.4 mean by "the next to fire". An absent `lastResult` key is how TypeScript models `lastResult?`. `skipped` reflects operator skips only; fires dropped by misfire coalescing are past with no result. The registration floor follows 2.2's rule that fires before `created_at` are never missed. The 31-day and 20,000-row caps bound CPU, since a day view of ten minutely crons is 14,400 rows.

</frozen-after-approval>

## Code Map

- `src/candybar/dispatcher/schedule.py`: reuse `fires_between` (call it with no skips and mark each fire with `SkipSet.covers`), `ceil_minute`, `group_skips`, `NO_SKIPS` and `ONE_MINUTE`. Do not change it.
- `src/candybar/events/vocabulary.py`: add `scheduled_idempotency_key(candybar_id, tick_id)`. `builders.build_candybar_invoke` (line ~293) switches to it, so the API and the dispatcher cannot drift. Use `tick_id_for_minute` for the tick id.
- `src/candybar/registry/store.py`: reuse `list_candybars` and `list_skips(session, start, end)` (minute-aligned; occurrence rows plus day rows). Add `fire_results(session, keys) -> dict[str, str]`: one `idempotency_key = ANY(:keys)` query on the unique index, as one array parameter (psycopg allows only 65,535 parameters, so IN-expansion would break), and `{}` without a query when no keys are given. Export it from `registry/__init__.py`.
- `src/candybar/api/schedule.py` (new): `WindowCandybar`, `ScheduleRow` (with `covered` and `last_result`; `skipped` is a property), `window_bounds`, `window_rows` and `attach_results`, plus the `MAX_WINDOW` and `MAX_ROWS` caps and `WindowTooLarge`. Pure, with no DB imports (the sole-writer guard). Load skips for `[min(start, cut), end)` so the earlier-fire check before a future window honors skips.
- `src/candybar/api/app.py`: `create_app(..., now=None)` and `GET /schedule`. Follow these patterns: `problem`, `utc_iso`, and the 5.1 route style; `asyncio.to_thread` for `window_rows`; `AwareDatetime` query params (with `alias="from"`). Existing routes stay untouched.
- `tests/contract_shapes.py`: add a parser for the schedule row, covering `?` optional fields and quoted unions taken from the contract, with `noise` read from the "rows include `noise: bool`" line. Add `assert_schedule_row`. The 5.1 exports stay unchanged.
- `tests/test_api_definitions.py`: the fake-store rig pattern, which monkeypatches functions in the app module.
- `tests/test_registry_integration.py`: the `migrated_db` fixture and the NullPool TestClient pattern for live tests.

## Tasks & Acceptance

**Execution:**
- [x] `src/candybar/events/vocabulary.py`, `src/candybar/events/builders.py`: add the key helper and use it in the builder (single source for the key format).
- [x] `src/candybar/registry/store.py`, `src/candybar/registry/__init__.py`: add `fire_results` (the past-row result read).
- [x] `src/candybar/api/schedule.py`: the pure window computation, reused by 5.3 and 5.4.
- [x] `src/candybar/api/app.py`: the `GET /schedule` route, the `now` injection, and the docstring (the story surface).
- [x] `tests/contract_shapes.py`, `tests/test_api_schedule.py`: one unit test per matrix row, the contract row-shape test on every row, and a dispatcher-equivalence test. That test drives the real `Dispatcher` over each tick minute of a window, with the same skips, and checks that its commands equal the non-skipped rows and its `skipped` facts equal the skipped rows. Also check that `fire_results` is asked only for past keys. This is the AC proof.
- [x] `tests/test_schedule_window_integration.py` and `.github/workflows/ci.yml` (live Postgres step): real candybars, invocations and skips on Postgres, read through the real route. This exercises the ANY-array query and the contract shape on real rows.
- [x] `README.md`: a schedule window section.

**Acceptance Criteria:**
- Given candybars with schedules, invocation history and active skips, when `GET /schedule?from&to` is called, then the rows are ordered by fire time, every row matches the contract field list parsed from `holocene-view-contract.md`, and at most one row is `current`. It is exactly one whenever the next fire lies in the window.
- Given the same skips, the upcoming fire times and `skipped` flags equal what the dispatcher would publish for those ticks (commands vs `skipped` facts).
- Given recorded scheduled or misfire invocations, the past rows carry their `lastResult` from the `invocations` table.

## Implementation Notes

- This ran under the workflow harness's no-questions directive. Checkpoint 1 was auto-approved ("Approve and continue"). The spec is about 1450 words, over the 1600-token target; it has one goal, so it was kept whole. This session has no subagent tool, so implementation and all three review layers ran inline, the same deviation recorded for 1.1 to 5.1.
- g33 preflight passed. The ecosystem route came back as execution, owned by Momo, and no board was written. No event types or schemas were added: this is a read-only route.
- `epic-5-context.md` was valid (no planning artifact is newer), so it was loaded instead of the raw planning docs. Continuity came from the done 5.1 spec: its fake-store rig, `contract_shapes.py`, the `migrated_db` live pattern, and `utc_iso`/problem+json.
- Implementation commit `0cd1c0e`:
  - New `api/schedule.py`: `window_bounds`, `window_rows`, `past_keys`, `attach_results`, `fire_key` and `ScheduleRow.to_json`. It is reusable by 5.3 and 5.4.
  - New route `GET /schedule`, plus `create_app(now=...)`.
  - New `registry.fire_results`.
  - `vocabulary.scheduled_idempotency_key` is now used by `build_candybar_invoke`, with the same key format and the same error.
  - The contract parser now covers optional fields, quoted unions and the `noise` row extension.
  - Tests: 33 unit tests (after review patch `5af7e2d`) in `tests/test_api_schedule.py` and a live test in `tests/test_schedule_window_integration.py` (CI live-Postgres step). README updated.
- Numbers I measured:
  - croniter costs about 10 µs per fire. About 18,700 rows take 167 ms to compute in the worker thread.
  - The work that stays on the loop (keys, results, JSON) takes about 40 ms. That is why only `window_rows` goes through `asyncio.to_thread`.
- Mutation checks:
  - Replacing the `= ANY(text[])` lookup with `.in_()` fails the live test: psycopg refuses more than 65,535 parameters.
  - Making the default clock naive fails `test_default_clock_is_the_utc_wall_clock`.
- No earlier story broke. Every existing suite passes unchanged: unit tests, the live-Postgres suites, and the NATS+PG dispatcher, misfire, skip, fault-injection and manual-invoke suites on throwaway containers.

## Spec Change Log

## Review Triage Log

The layers ran inline: blind-hunter, edge-case-hunter (with the deletion and claims checks) and verification-gap. The blind-hunter floor was N = min(floor(sqrt(52.5) + 1), 10) = 8.

| # | Layer | Finding | Verdict | Evidence / route |
|---|-------|---------|---------|------------------|
| 1 | verification-gap | `create_app`'s default clock (no `now` injected) is the production path, and no test exercises it. A naive `datetime.now()` would make every `/schedule` return 500 in production. | medium | Pre-verified (every route test injected `now`). Patched: `test_default_clock_is_the_utc_wall_clock` checks that `current` is the first minute after the real clock, straddle-safe. A mutation to a naive clock fails it. |
| 2 | verification-gap | The tick-id guard moved from `build_candybar_invoke` into `scheduled_idempotency_key`, and no test pins it; no test anywhere covered the old inline check either. | low | Pre-verified (searched for `invalid tick_id`/malformed ids: no hits). Patched: `test_scheduled_key_rejects_a_malformed_tick_id` covers both the helper and the builder. |
| 3 | verification-gap | The equivalence test has no schedule that restricts both day-of-month and day-of-week. Those are exactly the semantics where croniter `match` (dispatcher) and iteration (rows) could diverge. | low | Pre-verified. Patched: `echo` = `45 11 26 * 6` (the 26th OR Saturday) joins the dispatcher-equivalence test and must fire on Monday the 26th. |
| 4 | blind-hunter | Validation errors on `/schedule` query parameters get the shared handler's title "Invalid request body". | low | Real, but the handler predates this story (1.4), and `tests/test_cli_run.py:77` pins the title. Deferred to deferred-work.md. |
| 5 | blind-hunter + claims | `AwareDatetime` in lax mode also accepts a bare unix-epoch number (read as UTC), while the spec and README say ISO 8601 with an offset. | low | Real. It is unlikely in everyday use (clients send ISO strings), and refusing it needs a custom strict validator. Rejected. |
| 6 | blind-hunter | Row keys, results and JSON for a large window still run on the event loop. | low | Measured at about 40 ms for about 18,700 rows and bounded by the 20,000-row cap. The CPU-heavy croniter walk is off the loop. Rejected. |
| 7 | blind-hunter | The earlier-fire check before a far-future window walks every skipped fire between `cut` and `from`; the row cap does not bound it. | low | Real, but the walk stops at the first fire that is not skipped. It is long only behind hundreds of operator day-skips. Rejected. |
| 8 | blind-hunter | Past rows show the current `schedule`/`name`/`noise`, so runs under a schedule that was later PATCHed disappear from the past rows. | false | The frozen intent excludes it ("Never: … history reconstructed under an earlier schedule"). The runs remain readable at `/candybars/{id}/invocations`. |
| 9 | blind-hunter | A malformed schedule stored in the registry would make the whole window return 500. | false | Not reachable: register and PATCH both validate through `CandybarManifest`, and no other write path exists. The dispatcher has the same exposure. |
| 10 | blind-hunter | Candybars/skips and results are read in two sessions, so they are not one snapshot. | false | The only thing that can change between the reads is a fire finishing, which correctly adds a result. No delete path exists in `src/`. |
| 11 | blind-hunter | `fire_key` raises for a fire before 1970 (negative epoch minute). | false | Rows start at the candybar's registration minute, and `created_at` comes from the daemon's clock, so no pre-1970 fire can become a row. |
| 12 | blind-hunter | `window_rows`'s `limit=MAX_ROWS` default is bound at import. | false | No harm is named. The route uses the constant, and the tests pass `limit` explicitly. |
| 13 | edge-case-hunter | The `end <= start` comparison and subtraction in `window_bounds` run before the `OverflowError` guard. An extreme bound such as `0001-01-01T00:00+01:00` could therefore get a 500 instead of a 422. | low | Real, for absurd inputs only, and it is still a problem+json 500. The common case (year-9999 `to`) is a 422 and is tested. Rejected. |
| 14 | edge-case-hunter | `lastResult` could carry a value outside `success`/`fail`. | false | The `invocations_result_check` CHECK constraint (migration 0001) limits `result` to NULL, `success` or `fail`, and `fire_results` drops NULLs. |
| 15 | edge-case-hunter | A registration in the first second of a minute, while that tick's dispatch is still running, could be dispatched for that minute, but the row floor (`ceil(created_at)`) hides it. | low | Rare. The run is still recorded and readable at `/invocations`. Rejected. |
| 16 | edge-case-hunter (deletion) | The inline `TICK_ID_RE` check and key f-string removed from `build_candybar_invoke`. | false | Both are re-established in `scheduled_idempotency_key`, with the same message and format. `test_events.py:204` pins the key, and #2 now pins the guard. |
| 17 | edge-case-hunter (claims) | Claims checked: dispatcher helpers and SkipSet are reused; `lastResult` comes by idempotency key; the computation is pure; the builder adopts the key helper; one ANY-array query, with no query when there are no keys; compute is off the loop; the equivalence test exists; only past keys are looked up; the caps are 31 days inclusive and more than 20,000 rows; `lastResult` is omitted when unknown; manual runs are excluded. | — | None falsified except the epoch-number leniency (#5). |

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all pass
- `uv run ruff check src tests` -- expected: clean
- `CANDYBAR_TEST_PG=1 DATABASE_URL=<throwaway pg> uv run pytest tests/test_schedule_window_integration.py tests/test_registry_integration.py -q` -- expected: all pass (throwaway postgres:17-alpine, never the live DB)
