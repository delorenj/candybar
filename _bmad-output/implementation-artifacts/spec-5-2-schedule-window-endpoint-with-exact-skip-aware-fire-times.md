---
title: 'Story 5.2: Schedule window endpoint with exact skip-aware fire times'
type: 'feature'
created: '2026-10-26'
status: 'in-progress'
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
- [ ] `src/candybar/events/vocabulary.py`, `src/candybar/events/builders.py`: add the key helper and use it in the builder (single source for the key format).
- [ ] `src/candybar/registry/store.py`, `src/candybar/registry/__init__.py`: add `fire_results` (the past-row result read).
- [ ] `src/candybar/api/schedule.py`: the pure window computation, reused by 5.3 and 5.4.
- [ ] `src/candybar/api/app.py`: the `GET /schedule` route, the `now` injection, and the docstring (the story surface).
- [ ] `tests/contract_shapes.py`, `tests/test_api_schedule.py`: one unit test per matrix row, the contract row-shape test on every row, and a dispatcher-equivalence test. That test drives the real `Dispatcher` over each tick minute of a window, with the same skips, and checks that its commands equal the non-skipped rows and its `skipped` facts equal the skipped rows. Also check that `fire_results` is asked only for past keys. This is the AC proof.
- [ ] `tests/test_schedule_window_integration.py` and `.github/workflows/ci.yml` (live Postgres step): real candybars, invocations and skips on Postgres, read through the real route. This exercises the ANY-array query and the contract shape on real rows.
- [ ] `README.md`: a schedule window section.

**Acceptance Criteria:**
- Given candybars with schedules, invocation history and active skips, when `GET /schedule?from&to` is called, then the rows are ordered by fire time, every row matches the contract field list parsed from `holocene-view-contract.md`, and at most one row is `current`. It is exactly one whenever the next fire lies in the window.
- Given the same skips, the upcoming fire times and `skipped` flags equal what the dispatcher would publish for those ticks (commands vs `skipped` facts).
- Given recorded scheduled or misfire invocations, the past rows carry their `lastResult` from the `invocations` table.

## Implementation Notes

## Spec Change Log

## Review Triage Log

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all pass
- `uv run ruff check src tests` -- expected: clean
- `CANDYBAR_TEST_PG=1 DATABASE_URL=<throwaway pg> uv run pytest tests/test_schedule_window_integration.py tests/test_registry_integration.py -q` -- expected: all pass (throwaway postgres:17-alpine, never the live DB)
