---
title: 'Story 5.3: Next-fire poll fallback'
type: 'feature'
created: '2026-10-26'
status: 'in-review'
baseline_commit: 'd974adb1d6f3ff5f7ae5efcd252985fc6fa8d5d3'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-5-context.md'
  - '{project-root}/_bmad-output/specs/spec-cron-framework/holocene-view-contract.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** When the SSE stream (5.4) is unavailable, the Holocene view needs a cheap poll for the single current next-to-fire row. Today the only way to find it is `GET /schedule` over a window, which walks every fire in that window.

**Approach:** Add `GET /schedule/next`, which returns the one `current` row in the `/schedule` row shape. It is computed from the definitions and the future skips: one upcoming-fire lookup per candybar with the dispatcher's own `next_fire` helper, and no window walk. "Current" gets one definition in `api/schedule.py`, which `/schedule` also uses, so the two endpoints cannot disagree.

## Boundaries & Constraints

**Always:**
- `cut` and `current` mean exactly what they mean in 5.2. The row is the earliest non-skipped fire at or after `cut` across all candybars, at or after each candybar's registration minute, with ties broken by name. It is `{candybarId, name, schedule, fireTime, state:"current", skipped:false, noise}`, with no `lastResult` key. It is identical to the `current` row `/schedule` returns for any window that contains that fire.
- Bounded read: one definitions query plus one skips query, limited to skips at or after `cut` (occurrence `fire_time >= cut`, day `day >= cut.date()`). There is no invocations or results query and no window walk. Per candybar, the lookup costs O(1 + occurrence skips + skipped days it passes): a rest-of-day skip jumps to the next UTC midnight instead of stepping through that day's fires.
- Stated latency budget: with 200 registered candybars (mixed schedules, including minutely ones with today day-skipped and one that never fires), the median of 5 requests completes in under 50 ms on the in-memory rig and in under 100 ms on real Postgres.
- 404 problem+json when no candybar has an upcoming fire (empty registry, or every schedule never fires again). 500 problem+json when the database is down. The CPU work runs off the event loop.
- DB access goes only through `registry/store.py`.

**Never:**
- No migration, no new event type, no write path, no query parameters, no cache, and no change to dispatch decisions.
- No SSE (5.4), noise (5.5) or skip (5.6) endpoints.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Next fire | `*/5` A, `*/15` B, now 10:12:30 | A@10:15 (name tie-break over B@10:15), `state:"current"` | n/a |
| Skipped next | occurrence skip A@10:15 | B@10:15 | n/a |
| Day skipped | minutely A with today day-skipped, hourly B | B's next fire; A only if its next-day 00:00 comes first | n/a |
| Boundary | now exactly 10:15:00 | the 10:15 fire is past; next is ≥ 10:16 | n/a |
| Late registration | minutely A with `created_at` 10:20:10 while the clock reads 10:12 (skew), no other candybar | A@10:21 | n/a |
| Never fires | only `0 0 30 2 *` | — | 404 problem+json |
| Empty registry | none | — | 404 problem+json |
| DB down | list fails | — | 500 problem+json |

**Decisions (unattended run; spine and contract silent):** 404 rather than 204 or a null body: every non-200 answer in this API is problem+json, and "no current row" is the absence of the resource. The budget is set at about 5× the locally measured cost (about 45 µs per candybar), so slow CI runners pass; the deterministic croniter-step count is the real boundedness proof. `/schedule` takes its `current` row from the same global-next helper. That change fixes 5.2's rejected review finding #7: the walk over skipped earlier fires is replaced by the day-jumping lookup.

</frozen-after-approval>

## Code Map

- `src/candybar/dispatcher/schedule.py`: `next_fire(schedule, at, skips)` has no production callers yet; only `tests/test_skips.py:150-185` pins it. Add the rest-of-day jump: when `fire.date() in skips.rest_of_day`, re-seed croniter at the next UTC midnight minus one minute. Do not change `fires_between`, `previous_fire` or `matches_tick`, because the dispatcher uses them.
- `src/candybar/registry/store.py:281` `list_skips(session, start, end)`: make `end` optional (`None` = open-ended). Keep the order and validation. The dispatcher and `/schedule` callers stay unchanged.
- `src/candybar/api/schedule.py`: add `next_fire_of(candybar, cut, skips)` (floor = `ceil(created_at)`; `OverflowError` → None), `global_next(candybars, now, skips) -> (fire, candybar) | None` (min by `(fire, name)`), and `next_row(...) -> ScheduleRow | None`. `window_rows` replaces its `earlier`/`current` scan with `global_next`. Reuse `ScheduleRow.to_json`, `first_tick_after`, `_registered_floor`, `NO_SKIPS`. No DB imports.
- `src/candybar/api/app.py`: add `GET /schedule/next` next to `/schedule` (line ~456), in the same pattern: `clock()`, a single session for `list_candybars` and `list_skips(session, cut)`, `group_skips`, `asyncio.to_thread(next_row, ...)`, `problem`. Update the module docstring.
- `tests/test_api_schedule.py`: `Rig`, `skips_in`, `at`, `z`, `DAY`, `brief` (importable). Its `rig` fixture's fake `list_skips` needs `end` to be optional. All 33 tests must still pass unchanged.
- `tests/contract_shapes.py`: `assert_schedule_row`.
- `tests/test_schedule_window_integration.py` and `tests/test_registry_integration.py`: the `migrated_db` and NullPool TestClient live pattern. CI live-Postgres step: `.github/workflows/ci.yml:199`.

## Tasks & Acceptance

**Execution:**
- [x] `src/candybar/dispatcher/schedule.py`: the day-jump in `next_fire` (bounded lookup).
- [x] `src/candybar/registry/store.py`: open-ended `list_skips` (the future-skips read).
- [x] `src/candybar/api/schedule.py`: `next_fire_of`, `global_next`, `next_row`; `window_rows` uses `global_next` (one definition of current).
- [x] `src/candybar/api/app.py`: the `GET /schedule/next` route and docstring.
- [x] `tests/test_api_schedule_next.py`: one test per matrix row and the contract shape on every row. Also: an equivalence test over randomized registries and skips (`next_row` equals the `current` row of `window_rows` over `[cut, cut+31d)`, and equals the brute-force minimum over `fires_between`); a croniter-step count proving the bounded work; the latency-budget test; the read-shape test (one skips read from `cut`, open-ended; no results read; `window_rows` and `fires_between` never called).
- [x] `tests/test_skips.py`: pins the `next_fire` day-jump (consecutive skipped days; occurrence plus day skips on one date).
- [x] `tests/test_schedule_next_integration.py` and `.github/workflows/ci.yml`: live Postgres, with real candybars and both skip kinds. Exactly 2 SQL statements, neither touching `invocations`; the row equals `/schedule`'s current row; the live latency budget.
- [x] `README.md`: a `/schedule/next` subsection.

**Acceptance Criteria:**
- Given the schedule window machinery, when `GET /schedule/next` is called, then it returns the single current row in the `/schedule` row shape (checked against the contract parse), and that row equals the `current` row of `/schedule` for a window containing it.
- Given 200 candybars, including day-skipped minutely ones, when it is polled, then it answers with 2 bounded queries and no window walk, under the stated budget.

## Implementation Notes

- This ran under the workflow harness's no-questions directive. Checkpoint 1 was auto-approved ("Approve and continue"). The spec is about 1,120 words, over the 1,600-token target; it has one goal, so it was kept whole. This session has no subagent tool, so implementation and review ran inline, the same deviation recorded for 1.1 to 5.2.
- g33 preflight passed. The ecosystem route came back as execution (Momo) and UI (Holocene); no board was written. No event types or schemas were added: the route only reads.
- `epic-5-context.md` was valid, so it was loaded instead of the raw planning docs. Continuity came from the done 5.2 spec: `api/schedule.py`, the `Rig` test pattern, `contract_shapes.py` and the `migrated_db` live pattern.
- What was built:
  - `next_fire` gained the rest-of-day jump.
  - `list_skips(session, start, end=None)` is now open-ended when `end` is omitted; the dispatcher and `/schedule` calls are unchanged.
  - `api/schedule.py` gained `next_fire_of`, `global_next` and `next_row`. `window_rows` now takes `current` from `global_next`.
  - `GET /schedule/next` route; a `contract_route_line` helper; README subsection; CI live-Postgres step.
- Tests:
  - `tests/test_api_schedule_next.py` has 15 tests: every matrix row with a `/schedule` current-row cross-check, read shape, croniter step count, latency budget, randomized equivalence and the default clock.
  - `tests/test_skips.py` has 7 parametrized day-jump tests, each checked against stepping every fire.
  - `tests/test_schedule_next_integration.py` is the live test.
- The spec asked the randomized equivalence test for windows over `[cut, cut+31d)`. Over 31 days a minutely candybar is 44,640 rows, which is too slow for a unit test. It uses 6-hour windows instead, starting 2 hours before `cut`, 1 minute before, at `cut`, 30 minutes after and 3 hours after. These also cover the future-window case (no `current` row when an earlier fire comes first), and the brute-force check looks 400 days ahead.
- Numbers I measured:
  - The budget registry takes 12.6 ms median on the in-memory route, against the 50 ms budget.
  - The live route takes 20 ms median against real Postgres, against the 100 ms budget, with a new NullPool connection per request.
  - One `next_fire` lookup takes 25 to 50 µs; a schedule that never fires takes about 310 µs.
- Mutation checks:
  - Disabling the day-jump fails the step-count test (83,299 steps against the bound of 699) and the latency-budget test.
- No earlier story broke:
  - 624 unit tests pass and ruff is clean.
  - On throwaway postgres:17-alpine, the live-Postgres suites (registry, both skip stores, window, next) gave 15 passed and 2 skipped; the 2 skips need the bus.
  - On throwaway nats:2.10.29 with postgres:17-alpine, the bus suites (dispatcher, fault injection, misfire, both skip roles, manual invoke) passed 9 of 9. The refactored `list_skips` serves the dispatcher unchanged.
  - The 33 tests of 5.2 pass unchanged on the refactored `window_rows`.
## Spec Change Log

## Review Triage Log

The layers ran inline because this session has no subagent tool: blind-hunter, edge-case-hunter (with the deletion and claims checks) and verification-gap. The blind-hunter floor was N = min(floor(sqrt(40.1) + 1), 10) = 7. Verification-gap reported no gaps: each changed behavior has a test that fails on its smallest regression. That covers the day-jump (brute-force parametrized tests, step count), the open-ended `list_skips` (a live store assertion and the 2-statement count), `window_rows`'s current (5.2's 33 tests plus the randomized test) and the route (matrix tests, default clock, live test).

| # | Layer | Finding | Verdict | Evidence / route |
|---|-------|---------|---------|------------------|
| 1 | edge-case-hunter (claims) | The Tasks line says the equivalence test uses `[cut, cut+31d)` windows; the test uses 6-hour windows at random offsets, and only 40 of its 80 trials contained the next fire. | low | Real: half the trials asserted only "no current". Patched: every trial with a next fire also builds a window that contains it and asserts that window's `current` is that row. The 6-hour windows stay, because they cover the future-window case. Not 31 days, since a minutely candybar is 44,640 rows. |
| 2 | blind-hunter | The README hard-codes measured latencies (about 13 ms and 20 ms), which go stale on other machines. | low | Patched with a one-sentence deletion. The measurements stay in the Implementation Notes. |
| 3 | blind-hunter | The poll endpoint sends no `Cache-Control: no-store`, so an intermediary could serve a stale `current`. | false | Cloudflare caches by file extension by default, and `/schedule/next` has none. Traefik does not cache. With no `Last-Modified`, browsers have nothing to compute heuristic freshness from. `/schedule` has the same headers. |
| 4 | blind-hunter | `window_rows` now runs `global_next` even for an all-past window, where no row can be current. | low | Real, but the cost is one lookup per candybar per request (about 45 µs each). Skipping it would add a branch. Rejected. |
| 5 | blind-hunter | The "nothing will fire" 404 has the same status as an unknown route. | low | The two are distinguishable by the problem `title` ("No upcoming fire" vs "Not Found"). 404 is a frozen-intent decision. Rejected. |
| 6 | blind-hunter | Wall-clock latency assertions in the default suite can flake on a loaded runner. | low | The AC requires "asserted under a stated latency budget". The median of 5 runs with about 4× headroom (12.6 ms against 50 ms; 20 ms against 100 ms). The deterministic step count is the real boundedness proof. Rejected. |
| 7 | blind-hunter + edge-case-hunter | The clock is read before the DB read, so a slow DB can return a fire that has just passed. | low | Same ordering and exposure as `/schedule` (5.2). The window is only the DB latency, and the next poll corrects it. Rejected. |
| 8 | blind-hunter | The open-ended skip read grows without bound. | false | Only skips at or after `cut` are read, so history never grows it. Future skips are operator-written, and no write API exists yet (5.6). |
| 9 | blind-hunter | `next_fire_of` swallows `OverflowError` with no log. | false | Unreachable. `cut` comes from the wall clock, and croniter raises `CroniterBadDateError` (→ None) for a never-firing schedule long before year 9999. |
| 10 | blind-hunter | The tests import `Rig` from `test_api_schedule.py`, which couples the two files. | low | Developer-only. A change to `Rig` fails loudly in both files. The same precedent exists: `migrated_db` and `contract_shapes` are imported the same way. Rejected. |
| 11 | edge-case-hunter | The `next_fire` day-jump on 9999-12-31 raises `OverflowError` (date + 1 day) instead of returning None. | false | Its only production caller, `next_fire_of`, catches it, and no wall-clock `cut` reaches year 9999. |
| 12 | edge-case-hunter (deletion) | The removed `earlier` scan and index-based `current` in `window_rows`, and `_fires_in` losing its `skips` parameter. | false | Both are re-established through `global_next`. The equivalence is argued in its docstring and pinned by the randomized test, using window-bounded skip reads. `_fires_in` has no other caller in `src/` or `tests/`. 5.2's 33 tests pass unchanged. |
| 13 | edge-case-hunter (claims) | Other claims checked: one definition of current; two statements; no window walk; future skips only; lookup cost O(1 + skips); 404/500 problem bodies; work off the loop; no migration, event or write. | — | None falsified except #1. |
## Verification

**Commands:**
- `uv run pytest -q`: expected all pass.
- `uv run ruff check src tests`: expected clean.
- `CANDYBAR_TEST_PG=1 DATABASE_URL=<throwaway pg> uv run pytest tests/test_schedule_next_integration.py tests/test_schedule_window_integration.py tests/test_registry_integration.py -q`: expected all pass (throwaway postgres:17-alpine only).
