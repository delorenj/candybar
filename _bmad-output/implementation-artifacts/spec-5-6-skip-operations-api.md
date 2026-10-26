---
title: 'Story 5.6: Skip operations API'
type: 'feature'
created: '2026-10-26'
status: 'in-review'
baseline_commit: 'd8c96e5947937b840c71e966422da658c15163cb'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-5-context.md'
  - '{project-root}/_bmad-output/specs/spec-cron-framework/holocene-view-contract.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The Holocene view's skip controls need the contract's skip writes: `POST /candybars/{id}/skip` `{fireTime}` (skip one occurrence) and `POST /candybars/{id}/skip-rest-of-day` `{date}` (suppress that UTC date). Today skip rows exist (Epic 2) but only test code can write them; the store docstrings name the API's 404 for a fireless target as Story 5.6's job.

**Approach:** Two api-role routes validate the body, check the target is a real fire of the candybar (404 otherwise), publish one `bloodbank.cron.candybar.skipped` fact FIRST (the spine's state rule — the same fact the dispatcher would emit for the fire, so the SSE stream's existing `_on_skipped` produces `skip.applied` with no new stream code), then store the skip row in the same request flow. The dispatcher's per-tick skip read and the `/schedule` computation honor the new rows with no changes to either.

## Boundaries & Constraints

**Always:**
- Bodies are exactly the contract's: `{"fireTime": "<ts>"}` and `{"date": "<YYYY-MM-DD>"}`, extra keys forbidden, `null` and wrong types refused. Validation failures are 422 problem+json.
- Both routes require a registered candybar: unknown id → 404, malformed id → 422 (FastAPI's UUID parse), all problem+json.
- `/skip` answers 404 "no such fire" when `fireTime` is not a fire of the candybar's current schedule at or after its registration minute (`is_fire_of`). `/skip-rest-of-day` answers 404 when the schedule never fires on that UTC date. Both checks run BEFORE any publish or write, off the event loop (`asyncio.to_thread`), using the dispatcher's own helpers.
- The published fact is `bloodbank.cron.candybar.skipped` with `reason = "occurrence"` / `"rest_of_day"` (the Epic 2 skip kinds the dispatcher and stream already switch on) and `tick_id` naming the target: the fire's tick for `/skip`, the first fire's tick of that UTC day for `/skip-rest-of-day`. The event id is the store's deterministic skip id (`occurrence_skip_id` / `rest_of_day_skip_id`), so a retry of either call broker-dedupes against its own first publish, and the dispatcher's later per-fire fact (`uuid5(DISPATCH_NAMESPACE, "skipped:<kind>:<id>:<tick>")`, a different id) arrives as the stream's idempotent patch for the same row.
- Write order is fact first, then the row, in the same request flow (spine state rule): bus down → 503 with nothing stored; DB down → 500 with nothing stored. The store call is `add_occurrence_skip` / `add_rest_of_day_skip` unchanged — idempotent, first note wins. A repeat of a stored skip answers success (200) and publishes nothing.
- Success is 200 `{ "skipped": true }` plus `fireTime` or `date` echoed back (ISO 8601 UTC for `fireTime`; `date` as given). Timestamps with an offset are converted to UTC; a naive `fireTime` is 422.
- `fireTime` must be minute-aligned (the store's `_require_tick_minute` rule); an off-minute time is 422.
- `date` must parse as `YYYY-MM-DD` and name a real calendar date; anything else is 422, and a `datetime`-shaped value never reaches the store as a `datetime`.

**Never:** No skip deletion or listing endpoints (no contract surface for them). No changes to the dispatcher, executor, registry store, schemas, or `stream.py`. No new Bloodbank types: `skipped` with a kind reason is already the established emission. No batch skip route, no CLI, no auth. No 404 exemption for past fires or past days: skipping an already-fired minute stores inertly (the 2.3/2.4 precedent), only a fire that never exists is a 404.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Skip one | POST /skip `{fireTime}` on a `*/5` candybar, a real future fire | 200 `{skipped,fireTime}`; row stored; one fact reason=occurrence tick=that fire | n/a |
| Skip day | POST /skip-rest-of-day `{date}` on an hourly candybar | 200 `{skipped,date}`; one fact reason=rest_of_day, tick=the day's 00:00 fire | n/a |
| Skip reflected | after both | `/schedule` rows for the target read `skipped: true`, recomputed upcoming times | n/a |
| Stream event | skip stored while a stream is open | one `skip.applied` SSE event whose row reads `skipped: true` | n/a |
| Repeat | POST /skip twice, same fireTime | second call: 200, nothing published, store's first note kept | n/a |
| Unknown id | either route, random UUID | 404 problem+json | problem+json |
| Malformed id | `not-a-uuid` | 422 problem+json | problem+json |
| No such fire | `{fireTime: 10:07}` on `*/5` | 404; nothing published or stored | problem+json |
| Fireless day | `{date}` of a Sunday on a Mon-Fri schedule | 404; nothing published or stored | problem+json |
| Bad body | missing/null/extra key, wrong type | 422 problem+json; nothing published or stored | problem+json |
| Naive fireTime | `"2026-10-26T10:00:00"` | 422 problem+json | problem+json |
| Off-minute | `"2026-10-26T10:00:30Z"` | 422 problem+json | problem+json |
| Bad date | `"2026-13-40"`, `"2026-10-26 10:00"` | 422 problem+json | problem+json |
| Offset time | `"2026-10-26T12:00:00+02:00"` on a `0 10 * * *` candybar | 200; stored fire is 10:00 UTC | n/a |
| Bus down | sink raises a nats error | 503; no row stored | problem+json |
| DB down | store call raises | 500; no row stored | problem+json |
| Skip a past fire | fireTime before now that IS a fire | 200, stored inertly; `/schedule` past row unaffected if it ran | n/a |

</frozen-after-approval>

## Code Map

- `src/candybar/api/app.py` -- add `SkipRequest` / `SkipDayRequest` pydantic models (extra=forbid) and the two routes next to the noise routes. Reuse `problem`, `BUS_ERRORS`, the exception handlers, `get_candybar` (registry), `utc_iso`. Models: `fireTime: AwareDatetime`; `date: date` parsed from the contract's YYYY-MM-DD (pydantic's `date` type accepts only the ISO date form). Update the module docstring.
- `src/candybar/api/schedule.py` -- `is_fire_of` (already exported, used by `stream.py`) is the /skip fire-existence check; add `fires_on_day(candybar, day) -> datetime | None` returning the first fire of the UTC day (the fact's tick), reusing `fires_between`. DB-free; routes call both via `asyncio.to_thread` (croniter cost, same as `/schedule`).
- `src/candybar/registry/__init__.py` -- `add_occurrence_skip`, `add_rest_of_day_skip`, `occurrence_skip_id`, `rest_of_day_skip_id`, `UnknownCandybarError` are already exported. No store change.
- `src/candybar/events/builders.py::build_candybar_skipped` -- takes `event_id=`; pass the deterministic skip id so a retried POST broker-dedupes on `Nats-Msg-Id`.
- `src/candybar/events/vocabulary.py` -- `tick_id_for_minute(epoch)` builds the fire's `tick:<epoch>` tick_id.
- `src/candybar/api/stream.py::_on_skipped` -- already emits `skip.applied` for a `skipped` fact whose reason is in SKIP_KINDS; `covered=True` marks the row skipped. `_fact_row` guards with `is_fire_of` (the API's 404 means this always matches) and falls back to `skip:<event.id>` keyed id-only frame when the day fact's tick matches no row — both paths need no change; verify with tests.
- `tests/test_api_definitions.py` -- `Rig`, `Row`, `FakeSink`, `T0`, `rig` fixture: the in-memory rig to import; monkeypatch `add_occurrence_skip` / `add_rest_of_day_skip` on `candybar.api.app`.
- `tests/contract_shapes.py` -- add `skip_routes()` and `skip_body_fields()` parsed from the contract's "Skip operations" section (WRITE_ROUTE_RE already exists), like `noise_routes`.
- `tests/test_skip_occurrence_integration.py` / `test_skip_rest_of_day_integration.py` -- the live-suite patterns (throwaway Postgres + bus, dispatcher honoring stored rows); the new live test rides the same fixtures and adds the API as the skip writer.
- `.github/workflows/ci.yml` -- the live-Postgres step gets the new live test file; the SSE live step if the stream smoke extends.

## Tasks & Acceptance

**Execution:**
- [x] `src/candybar/api/schedule.py` -- `fires_on_day` helper -- the day-existence check and the day fact's tick in one.
- [x] `src/candybar/api/app.py` -- the two routes, request models, docstring -- contract-conformant writes with fact-first ordering.
- [x] `tests/contract_shapes.py` -- `skip_routes()` and `skip_body_fields()` -- contract conformance parsed from the file.
- [x] `tests/test_api_skips.py` -- every matrix row except live-only ones; contract test (the app serves exactly the contract's skip routes, bodies match); fact-first ordering; determinism of the event id; the stream-fed-with-route-facts check (route's fact into a real hub emits `skip.applied` with `skipped: true`).
- [x] `tests/test_skip_api_integration.py` -- on throwaway Postgres + bus through the real routes and the real dispatcher role: a skip posted via the API suppresses that fire (no invocation row/command, one occurrence fact), a day skip suppresses the day's fires, `/schedule` reads them `skipped: true`, and the stream shows `skip.applied`.
- [x] `README.md` -- a "Skip operations (Story 5.6)" section.

**Acceptance Criteria:**
- Given Epic 2 skip mechanics and a registered candybar, when the consumer posts `{ fireTime }` to `/skip` or `{ date }` to `/skip-rest-of-day`, then the corresponding skip rows are written and honored by the dispatcher, and subsequent `/schedule` responses show affected rows `skipped: true` with recomputed upcoming fire times.
- Given either write succeeds, when a stream consumer is connected, then it receives the operation's `skip.applied` event on `/schedule/stream` without waiting for the fire's tick.
- Given a nonexistent fireTime or id, when either route is called, then the answer is an RFC 7807 404 problem+json and nothing is stored or published.

## Implementation Notes

- Unattended run under the workflow harness's no-questions directive; Checkpoint 1 auto-approved with no Open Questions (the fact-first bus emission is settled by the spine's state rule + the existing `_on_skipped`; the success body is the contract-silent trivial shape).
- Implementation landed as one commit (`080ae7b`) after iterative local verification. Surprises: (1) the test rig's `T0` registration minute made an hourly candybar's first day-fire 06:00, not 00:00 — the day-fact tick assertion now registers that candybar at midnight; (2) the API answers ISO times with `+00:00` (`utc_iso`), not `Z` — tests compare against `utc_iso`; (3) a `TestClient` call inside `asyncio.run` blocks the hub's loop — the stream test runs the request via `asyncio.to_thread`.
- Matrix row "Skip a past fire ... /schedule past row unaffected if it ran": the "if it ran" branch (a recorded invocation wins over the skip) is 5.2's `ScheduleRow.skipped` rule, covered by 5.2's tests; here the unit test pins inert storage.
- Verification: 38 new unit tests pass; full unit suite 735 passed / 3 skipped (pre-existing skips); ruff clean; live integration green on throwaway nats:2.10.29 + postgres:17-alpine (1 passed), plus regression runs of the live-Postgres step (17 passed, 2 skipped), the fault+misfire+skips step (6 passed), and the bus suites (6 passed). Containers torn down after.
- Review patch re-verified: unit suite 735 passed, ruff clean, live skip-API test 1 passed, fault+misfire+skip live suites 5 passed, live-Postgres suites 13 passed / 2 skipped — all on a fresh throwaway pair, torn down after.


## Spec Change Log

## Review Triage Log

The three review layers (blind-hunter, edge-case-hunter, verification-gap) ran INLINE because this session has no subagent-spawn capability — the same deviation as stories 1.1–5.5. Blind-hunter floor: diff 73.5kB → N = min(floor(√73.5+1), 10) = 9.

- PATCHED (low) — repeat POST republished the fact before learning the row exists: a repeat outside the broker's duplicate window would emit a second observable `skipped` fact and a redundant `skip.applied`, contradicting the frozen rule "a repeat ... publishes nothing" (and the test's own name while it asserted 2 facts). Fixed with a store pre-check (`skip_stored`, a primary-key read on the deterministic id): a repeat answers 200 with no publish and no write; the publish still precedes the insert for a fresh skip; a concurrent twin racing the check still broker-dedupes on the shared deterministic id. Test now asserts exactly 1 fact.
- REJECTED (false) — `date: date` field shadowing the imported type: pydantic v2 resolves annotations in module globals; covered by the passing 422/date tests.
- REJECTED (false) — ORM row crossing threads via `asyncio.to_thread(is_fire_of, candybar, fire)`: `WindowCandybar` is a detached frozen dataclass of plain fields.
- REJECTED (false) — unlocked `get_candybar` read makes concurrent duplicate inserts possible: skip ids are deterministic and both inserts use `on_conflict_do_nothing`; broker dedupe covers the twin publish.
- REJECTED (false) — 404 check can race a schedule PATCH, storing a skip for a no-longer-produced fire: that row is inert by the established 2.3/2.4 "stores inertly" rule; no wrong dispatch can follow.
- REJECTED (per workflow rule) — the spec's Verification command for the live test omits `CANDYBAR_TEST_FAULT=1` (the env gate would silently skip): findings whose fix edits this build's spec are rejected; the correct command is the CI step's.
- REJECTED (false) — the day fact's `tick_id` could name a fire that is itself occurrence-skipped, confusing the stream: `_on_skipped` marks `covered` from the fact's reason regardless; the row reads `skipped: true` either way.
- REJECTED (false) — integration stream check uses `hub.frames()` instead of a real HTTP SSE client: this story adds no stream code; the real HTTP path is 5.4's tested surface.
- REJECTED (false) — `assert day is None` in `_store_skip` is stripped under `python -O`: the routes pass exactly one of the two; a violation is a programming error caught in tests.
- Claims check (edge-case layer reading this spec): "404 before any publish or write" (pinned by the 404 tests asserting zero facts), "fact FIRST then row" (pinned by `writes_at_publish == [0]`), "stream with no new stream code" (`stream.py` untouched; real route fact → `skip.applied` tested), "repeat publishes nothing" — FALSE before the patch, true after; everything else verified true.

## Design Notes

- **The fact IS the stream event.** The contract's "Both are reflected ... in the stream" is satisfied without touching `stream.py`: the route publishes `bloodbank.cron.candybar.skipped` before storing, and the hub's existing `_on_skipped` turns it into `skip.applied` (`covered=True` because the reason is a skip kind), exactly as it does for the dispatcher's later per-fire suppression fact. The overlay/read race in `_fact_row` is safe: the route publishes BEFORE the row commits, but `_on_skipped` reads only the definition (not the skip rows) and marks the row from the fact itself.
- **Deterministic fact ids.** The route's fact id is the store's skip-row id (UUIDv5 of the skip). A retry after an ambiguous failure (503, timeout) republishes the same `Nats-Msg-Id`, which JetStream dedupes inside the duplicate window; a plain repeat of an already-stored skip is caught by the `skip_stored` pre-check and publishes nothing (review patch), and a concurrent twin racing that check still broker-dedupes on the shared id. The dispatcher's own fact for the same fire carries a different deterministic id, so both can appear; the stream patch is idempotent, and the bus contract allows it (facts are ordered per candybar, not unique per fire).
- **404 rule.** "Nonexistent" means the schedule never produces that fire (off-schedule minute, pre-registration, fireless date) — a client-visible nonsense target. A real fire that already passed is storable and inert, matching 2.3/2.4: the view can skip a row it already renders.
- **`tick_id` on the day fact.** The skipped schema requires `tick_id` (nullable). The day skip is not bound to one fire, but `tick_id = null` would emit the stream's `skip:<event.id>` id-only frame and no row patch until the first suppressed tick; naming the day's first fire gives the immediate row patch. It is metadata only — the registry row (kind=rest_of_day) is what suppresses the day.

## Verification

**Commands:**
- `uv run pytest tests/test_api_skips.py -q` -- expected: all pass
- `uv run pytest tests/ -q -k 'not integration and not e2e'` -- expected: full unit suite pass
- `uv run ruff check src/candybar/api/ tests/test_api_skips.py tests/contract_shapes.py` -- expected: clean
- `CANDYBAR_TEST_PG=1 CANDYBAR_TEST_NATS=1 CANDYBAR_TEST_FAULT=1 uv run pytest tests/test_skip_api_integration.py -q` (throwaway containers) -- expected: live suite pass
