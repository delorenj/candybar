---
title: 'Story 2.4: Skip the rest of a day'
type: 'feature'
created: '2026-10-26'
status: 'in-progress'
route: 'dispatch'
baseline_commit: '30ed92e60d5472eb561ef828422d8dc06e3f2e6f'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** An operator can suppress one fire (Story 2.3) but cannot quiet a noisy or known-broken candybar for the rest of a UTC day without deleting its schedule. The `skips` table already carries a nullable `day` column (0001) and nothing writes it.

**Approach:** Add the skip kind `rest_of_day` with a registry write path `{candybar_id, date}` and extend the window read. Extend `SkipSet` with the covered UTC dates so every skip-aware helper and the dispatcher honor day skips with no control-flow change: each suppressed fire publishes `bloodbank.cron.candybar.skipped` (`reason=rest_of_day`) at the tick on which it would have fired, and the next UTC day dispatches normally.

## Boundaries & Constraints

**Always:**
- Day skips are registry rows (`kind='rest_of_day'`, `day` = UTC date, `fire_time` NULL), written and read only through `registry/store.py` (AD-10). Rows persist after they are honored; they are override state that Epic 5 renders.
- `add_rest_of_day_skip` is idempotent: the row id is UUIDv5 of `(candybar_id, day)` with insert-on-conflict-do-nothing, so a repeat stores nothing new and says so (the first row, and its note, are kept). Unknown candybar raises `UnknownCandybarError`; a `day` that is not a plain `datetime.date` (a `datetime`, a string, None) raises `ValueError` before touching the database.
- The stored date covers that whole UTC date `[day 00:00Z, day+1 00:00Z)`. A fire is suppressed when `fire.date() == day`; the following UTC day is unaffected.
- The `skipped` fact for a suppressed fire `m` has the deterministic id UUIDv5 `skipped:rest_of_day:<candybar>:<tick_id(m)>` — the existing reason-keyed identity — so the tick path, a gap tick and any redelivery rebuild the same broker-dedupable id, distinct from the occurrence fact for the same fire.
- Each suppressed fire is recorded at the time it would have fired: the tick path publishes one `skipped` fact per suppressed matching tick; inside a gap, overridden fires in the 24h window are enumerated exactly like occurrence-skipped ones.
- `list_skips(start, end)` additionally returns rest-of-day rows whose date intersects the window: `start.date() <= day <= (end - 1 minute).date()`. Occurrence rows keep the 2.3 semantics. Ordering stays `fire_time, candybar_id` (day rows have NULL `fire_time`, so they sort last, per candybar id).
- `SkipSet` gains `rest_of_day: frozenset[date]`; `reason_for(fire)` returns `occurrence` for an occurrence-skipped fire, else `rest_of_day` when the fire's UTC date is covered, else None. `group_skips` folds `kind='rest_of_day'` rows with a non-NULL `day`; malformed rows (NULL day) and unknown kinds stay ignored.
- Misfire coalescing (2.2/2.3) inherits day skips through the shared helpers: a day-skipped latest missed fire hands the run to the latest NON-skipped missed fire (which may be on an earlier date; walking a fully skipped day terminates because the covered set is finite).
- With no skips port wired, behaviour is exactly Story 2.2 (2.3's regression test keeps passing).

**Never:**
- No skip API or CLI. Story 5.6 owns `POST /candybars/{id}/skip-day`, including any 404 for a date the schedule never fires on, using `matches_tick`/`fires_between`.
- No migration, no new event types, no schema revision: `skipped.data.reason` is a free string (Story 1.2 decision) and the 0001 `skips` columns already fit.
- Day skips never touch manual invokes, the executor, or a fire whose tick was already dispatched before the skip was stored.
- No partial days: the override is the whole UTC date, never "from now until midnight" (a skip stored at 23:59 covers 00:00–24:00 of that date; already-dispatched fires are unaffected because their ticks are gone).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Day-skipped tick fire | `*/5`, day skip for Oct 26, tick 10:10 | No command; one skipped fact (`rest_of_day`, tick 10:10); cursor advances | n/a |
| Whole day suppressed | same, ticks across Oct 26 | One skipped fact per matching tick that day, each published on its own tick | n/a |
| Next UTC day | same, tick Oct 27 00:00 | Scheduled command as usual | n/a |
| Day before the skip | same, tick Oct 25 23:55 | Scheduled command as usual | n/a |
| Other candybar | day skip on A, B `*/5` | B dispatches all ticks of the skipped day | n/a |
| Upcoming helper | `next_fire(*/5, Oct 26 23:55, skip day Oct 26)` | Oct 27 00:00; `fires_between` over Oct 26–27 omits all Oct 26 fires; `previous_fire` before Oct 27 00:05 walks back to Oct 25 23:55 | n/a |
| Gap covering the skipped day | hourly job, cursor Oct 26 00:05, day skip Oct 26, tick Oct 27 00:30 | Latest non-skipped missed fire = Oct 27 00:00 runs as misfire; the 23 overridden Oct 26 fires get `rest_of_day` facts, oldest first | n/a |
| Gap entirely inside the skipped day | cursor Oct 26 00:05, day skip Oct 26, tick Oct 26 02:30 | Nothing runs; overridden fires 01:00/02:00 recorded `rest_of_day` | n/a |
| Both kinds cover one fire | occurrence skip + day skip on the same fire | One skipped fact, `reason=occurrence` (the more specific override wins) | n/a |
| Duplicate store | same `{id, day}` twice, different note | One row, second call reports not-created, first note kept | n/a |
| Unknown candybar / bad day | random UUID; `datetime` or `str` as `day` | Nothing stored | `UnknownCandybarError` / `ValueError` |
| Past or fireless date | day skip for yesterday, or a date the schedule never fires on | Stored, inert (same contract as 2.3's unmatched fire time) | n/a |
| Skips read fails | DB error loading skips | Nothing published; tick nak'd (2.3 behavior, unchanged) | redelivery rebuilds the same ids |

**Decisions (unattended run, spine silent):** the event reason is the skip kind token `rest_of_day`, matching 2.3's `occurrence` precedent; when both an occurrence and a day skip cover one fire, the occurrence reason wins (the more specific override); the stored date covers the whole UTC date rather than "remaining fires from store time", because rows are override state Epic 5 renders and past fires are already dispatched, so the distinction is unobservable at dispatch time; a past or fireless date stores inertly rather than erroring (2.3 precedent).

</frozen-after-approval>

## Code Map

- `src/candybar/registry/models.py` -- `Skip` already carries `day` (0001). Add `SKIP_REST_OF_DAY = "rest_of_day"` and extend `SKIP_KINDS`; refresh the stale "Story 2.4 adds" comment.
- `src/candybar/registry/store.py` -- add `rest_of_day_skip_id(candybar_id, day)` (UUIDv5 `f"{SKIP_REST_OF_DAY}:{candybar_id}:{day.isoformat()}"` under `SKIP_NAMESPACE`) and `add_rest_of_day_skip(session, candybar_id, day, *, reason=None) -> tuple[SkipEntry, bool]`: same shape as `add_occurrence_skip` (candybar-exists check, `pg_insert(...).on_conflict_do_nothing(index_elements=[Skip.id]).returning`, FK `IntegrityError` race -> `UnknownCandybarError`), storing `kind=rest_of_day`, `day=day`, `fire_time=None`. Extend `list_skips` with an OR clause for rest-of-day rows whose `day` intersects the window (`start.date() <= day <= (end - ONE_MINUTE).date()`). Export the new names from `registry/__init__.py`.
- `src/candybar/dispatcher/schedule.py` -- `SkipSet` gains `rest_of_day: frozenset[date] = frozenset()`; `reason_for` checks occurrences first, then the fire's UTC date; `__bool__` covers both. `SkipRow` protocol gains `day: date | None`. `group_skips` folds rest-of-day rows. `previous_fire`/`fires_between`/`next_fire` need NO change — they already ride `skips.covers`.
- `src/candybar/dispatcher/worker.py` -- rename `occurrence_skipped_for` to `operator_skipped_for` (it now serves both kinds; the reason parameter already carries the kind into the event id and data). No control-flow change: the tick path and `_coalesce` already publish per-fire skipped facts keyed on the reason.
- `src/candybar/dispatcher/role.py` -- NO change (the skips port and `list_skips` signature are unchanged).
- Do NOT change: `executor/*`, `events/*` (`build_candybar_skipped` already takes reason + event_id), the migrations, `api/*`.
- Tests to reuse: the `tests/test_skips.py` `Harness`/`Message`/`FakeCursor`/`Row` pattern; `tests/test_skip_occurrence_integration.py` scaffold (`_refuse_live_bus`, `_reset_streams`, `_skipped_events`, `ENV_LOG_SCRIPT`, cursor snapshot/restore, `_make_script`, `_ensure_schema`).

## Tasks & Acceptance

**Execution:**
- [ ] `src/candybar/registry/{models,store,__init__}.py` -- skip kind vocabulary, `rest_of_day_skip_id`, idempotent `add_rest_of_day_skip`, day-intersecting `list_skips`.
- [ ] `src/candybar/dispatcher/schedule.py` -- `SkipSet.rest_of_day`, `group_skips` folding, stale-comment cleanup.
- [ ] `src/candybar/dispatcher/worker.py` -- rename to `operator_skipped_for`; docstrings mention both kinds.
- [ ] `tests/test_skips.py` -- update the import/usages for the rename; keep every 2.3 test green unchanged otherwise.
- [ ] `tests/test_skip_rest_of_day.py` -- NEW: every non-DB matrix row, deterministic ids, redelivery identity, precedence, store-input validation without a session.
- [ ] `tests/test_skip_rest_of_day_integration.py` -- NEW live suite. Store: idempotence + note kept, unknown candybar, day-intersecting window read. Roles: production `run_dispatcher` + `run_executor` on a throwaway bus — a stored day skip suppresses every remaining fire across a UTC midnight (and the next day runs), plus a gap covering a fully skipped day (hourly job runs the next day's fire as misfire).
- [ ] `.github/workflows/ci.yml` -- store test in the live-Postgres step; role test in the fault + misfire + skips step.
- [ ] `README.md` -- extend the skip-semantics section with rest-of-day behavior.

**Acceptance Criteria:**
- Given a registered `*/5` candybar with a stored rest-of-day skip for its tick's UTC date, when the live dispatcher consumes ticks at 23:40, 23:45 (that date) and 00:00, 00:05 (the next), then exactly two child runs happen (00:00 and 00:05, `trigger=scheduled`), no `invocations` row or command exists for the suppressed fires, `BLOODBANK_EVENTS` holds one `skipped` fact per suppressed fire with `reason=rest_of_day`, and the neighbour candybar runs all four ticks.
- Given an hourly candybar whose entire UTC date is day-skipped and a dispatcher gap spanning it, when the first tick after the gap lands on the following day, then the latest NON-skipped missed fire (on the following day) runs as `trigger=misfire` and each skipped-day fire in the 24h window is recorded with `reason=rest_of_day`.
- Given the same registry rows, when `next_fire`/`fires_between`/`previous_fire` compute with the candybar's `SkipSet`, then no fire on the skipped date is produced.

## Implementation Notes

## Spec Change Log

## Review Triage Log

## Design Notes

`SkipSet.reason_for(fire)`:

```python
if fire in self.occurrences:
    return SKIP_OCCURRENCE
if fire.date() in self.rest_of_day:
    return SKIP_REST_OF_DAY
return None
```

Everything downstream (`covers`, the helpers, the tick path, `plan_misfires`, `_coalesce`) already speaks `reason_for`, so day skips need no dispatcher control-flow change. `previous_fire` walking a fully skipped day is bounded: each stored date suppresses a finite number of fires (1440/day worst case for `* * * * *`), and only stored dates are walked.

Window read, exact intersection in date terms: fires in `[start, end)` have dates in `[start.date(), (end - 1m).date()]`; a day row matters iff its `day` is in that range.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all unit tests pass.
- `uv run ruff check .` -- expected: clean.
- `CANDYBAR_TEST_PG=1 uv run pytest tests/test_skip_rest_of_day_integration.py -q` -- on throwaway postgres:17-alpine. Expected: store tests pass.
- `CANDYBAR_TEST_NATS=1 CANDYBAR_TEST_PG=1 CANDYBAR_TEST_FAULT=1 uv run pytest tests/test_skip_rest_of_day_integration.py tests/test_skip_occurrence_integration.py tests/test_misfire_integration.py tests/test_dispatch_fault_injection_integration.py -q` -- on throwaway nats:2.10.29 + postgres:17-alpine. Expected: pass (2.1–2.3 suites unaffected).
