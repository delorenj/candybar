---
title: 'Story 2.4: Skip the rest of a day'
type: 'feature'
created: '2026-10-26'
status: 'done'
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
- [x] `src/candybar/registry/{models,store,__init__}.py` -- skip kind vocabulary, `rest_of_day_skip_id`, idempotent `add_rest_of_day_skip`, day-intersecting `list_skips`.
- [x] `src/candybar/dispatcher/schedule.py` -- `SkipSet.rest_of_day`, `group_skips` folding, stale-comment cleanup.
- [x] `src/candybar/dispatcher/worker.py` -- rename to `operator_skipped_for`; docstrings mention both kinds.
- [x] `tests/test_skips.py` -- update the import/usages for the rename; keep every 2.3 test green unchanged otherwise.
- [x] `tests/test_skip_rest_of_day.py` -- NEW: every non-DB matrix row, deterministic ids, redelivery identity, precedence, store-input validation without a session.
- [x] `tests/test_skip_rest_of_day_integration.py` -- NEW live suite. Store: idempotence + note kept, unknown candybar, day-intersecting window read. Roles: production `run_dispatcher` + `run_executor` on a throwaway bus — a stored day skip suppresses every remaining fire across a UTC midnight (and the next day runs), plus a gap covering a fully skipped day (hourly job runs the next day's fire as misfire).
- [x] `.github/workflows/ci.yml` -- store test in the live-Postgres step; role test in the fault + misfire + skips step.
- [x] `README.md` -- extend the skip-semantics section with rest-of-day behavior.

**Acceptance Criteria:**
- Given a registered `*/5` candybar with a stored rest-of-day skip for its tick's UTC date, when the live dispatcher consumes ticks at 23:40, 23:45 (that date) and 00:00, 00:05 (the next), then exactly two child runs happen (00:00 and 00:05, `trigger=scheduled`), no `invocations` row or command exists for the suppressed fires, `BLOODBANK_EVENTS` holds one `skipped` fact per suppressed fire with `reason=rest_of_day`, and the neighbour candybar runs all four ticks.
- Given an hourly candybar whose entire UTC date is day-skipped and a dispatcher gap spanning it, when the first tick after the gap lands on the following day, then the latest NON-skipped missed fire (on the following day) runs as `trigger=misfire` and each skipped-day fire in the 24h window is recorded with `reason=rest_of_day`.
- Given the same registry rows, when `next_fire`/`fires_between`/`previous_fire` compute with the candybar's `SkipSet`, then no fire on the skipped date is produced.

## Implementation Notes

I implemented this directly in this session. There is no subagent-spawn tool, the same deviation as Stories 1.1 to 2.3. The spec runs to about 2.3k tokens, over the 1600 guide, and I kept it whole: one cohesive goal, unattended run. Commits on main: 79c236c (spec), bb7f0e2 (code + unit tests), 2776031 (live suite, CI, README).

**What was built**
- Registry: `SKIP_REST_OF_DAY` in `SKIP_KINDS`; `rest_of_day_skip_id` (UUIDv5 `rest_of_day:<candybar>:<iso-date>` under the existing `SKIP_NAMESPACE`); `add_rest_of_day_skip` with the 2.3 contract (candybar check, insert-on-conflict-do-nothing, FK race -> `UnknownCandybarError`, first note kept). `list_skips` is now an OR: occurrence rows in `[start, end)` plus rest-of-day rows with `start.date() <= day <= (end - 1m).date()` — the exact set of dates a fire in the window can land on. No migration: the 0001 `day` column already fit.
- Schedule: `SkipSet.rest_of_day: frozenset[date]`; `reason_for` checks occurrences first (the more specific override wins), then the fire's UTC date. `group_skips` folds day rows; malformed (NULL day) and unknown kinds stay ignored. The three helpers needed no change — they ride `covers`.
- Dispatcher: no control-flow change. `occurrence_skipped_for` renamed `operator_skipped_for` (the reason parameter already carried the kind into the event id and data); the tick path and `_coalesce` publish per-fire skipped facts as before.
- A naive `day` that is a `datetime` (a `date` subclass), a string, None or an int is a `ValueError` before any session work — one error class for invalid skip inputs, matching 2.3 (ruff's TRY004 `TypeError` preference is waived with a noqa naming that contract).

**Live proof** (production `run_dispatcher` + `run_executor`, throwaway nats:2.10.29 and postgres:17-alpine)
- Across a UTC midnight with `*/5` candybars and a day skip on one of them: the skipped candybar ran only the next day's 00:00/00:05 (scheduled), with no rows for the suppressed fires and counters 2/2; the bus held one `rest_of_day` fact per suppressed fire — 23:40 and 23:45 on their own ticks, 23:50 and 23:55 when the 00:00 tick closed the 15-minute gap; the neighbour ran all four ticks plus its own 23:55 misfire.
- Gap spanning a fully skipped day (hourly candybar, 24h25m down): the following day's 00:00 fire ran as `misfire`; the skipped day's 23 fires inside the 24h window were each recorded `rest_of_day`, oldest first; no rows for them; counters 2/2.
- The 2.1 fault, 2.2 misfire, 2.3 occurrence-skip and registry live suites still pass unchanged (14 passed).

**Verification results**
- 530 unit tests pass (24 new), ruff clean.
- Live: the 2.4 suite 2 passed in 7.4s; regression suites 14 passed.

**Test-authoring notes (bugs in my first test draft, not the code)**
- The midnight-crossing ticks are not consecutive minutes, so the 23:45 -> 00:00 tick is a 15-minute gap: coalescing records the skipped candybar's remaining SKIP_DAY fires (23:50/23:55) as overridden and runs the neighbour's 23:55 misfire. The assertions now expect exactly that — a richer proof of "each suppressed fire is recorded".
- In part B the two `*/5` candybars leave the registry before the 24h gap tick, so their coalesced backlogs (~290 facts each) do not flood the throwaway bus; the hourly candybar is the one under test there.

**Decisions where the spine is silent**
- The stored date covers the whole UTC date, not "from store time to midnight": rows are override state Epic 5 renders, and past fires are already dispatched, so the distinction is unobservable at dispatch time.
- Occurrence beats rest-of-day when both cover one fire (the more specific override); the fact's reason is then `occurrence`.
- A past or fireless date stores inertly (2.3 precedent), so `add_rest_of_day_skip` never consults the schedule.
- The day-window read is exact (`start.date() <= day <= (end - 1m).date()`), not an over-read, so the dispatcher never pulls irrelevant rows.

## Spec Change Log

## Review Triage Log

The three review layers (blind-hunter, edge-case-hunter, verification-gap) ran inline, because no subagent-spawn tool was available; the same deviation as 1.1 to 2.3. Diff: 30ed92e..2776031 over src, tests, .github and README. It is 56.5 kB, so the finding floor is N = min(floor(sqrt(56.5)+1), 10) = 8.

**Patched** (bff8f2f)
- *Blind + verification-gap.* `list_skips` ordered by `(fire_time, candybar_id)`; two day rows for one candybar tie on both keys (NULL fire_time), so the live store test's `[SKIP_DAY, NEXT_DAY]` assertion rested on unspecified Postgres tie order. low, patched: `order_by(fire_time, day, candybar_id)`; occurrence ordering (2.3) is unchanged because non-NULL fire_time dominates. Re-ran all seven live suites (20 passed).
- *Blind.* `tests/test_skips.py`'s `Row` double no longer satisfied the `SkipRow` protocol structurally (no `day`); a future 2.3-style test folding a day row through that harness would die with AttributeError instead of a real failure. low, patched: `day: date | None = None` on the double.
- *Blind.* `test_a_skip_for_another_date_is_inert` carried a self-contradicting comment from a drafting edit. low, patched: the comment now states the actual contract (the 25th's row is outside the tick's window, so it is not even read).

**Rejected**
- *Edge.* `list_skips(start, start)` (empty window) returns the date's day rows but no occurrence rows — an asymmetric contract. false: the dispatcher's windows are always non-empty (tick path `[minute, minute+1m)`; gap path `[last+1m, minute+1m)` with `minute > last + 1m`), and 2.3 established the store validates bounds, not ordering, with the same empty-window behavior for occurrences.
- *Blind.* `previous_fire`/`next_fire` walk up to 1440 croniter steps across a skipped day for a `* * * * *` schedule. low, rejected: gap detection and API upcoming-fire calls only, bounded by the stored override set, and a shortcut index would add complexity for an operator-sized table.
- *Blind.* A `* * * * *` candybar day-skipped emits up to 1440 `skipped` facts that day. rejected: the AC requires each suppressed fire to be recorded; thinning the record would falsify it.
- *Blind.* `add_rest_of_day_skip` never checks the date against the schedule. rejected: inert-by-design (2.3 precedent), and Story 5.6 owns the 404 for a fireless date via `matches_tick`.
- *Blind.* The `_require_date` `noqa: TRY004` suppresses ruff's TypeError preference. rejected: deliberate and documented — invalid skip inputs are one error class (ValueError) across both add functions.
- *Edge.* `add_rest_of_day_skip`'s read-back after insert could return None if the candybar were deleted mid-call. false: nothing in `src/` deletes candybars (the only `session.delete` calls are in tests); same refutation as 2.3.
- *Edge.* A day row for a date straddling the gap-read window edge could be missed. false: the window read uses exact date intersection `[start.date(), (end-1m).date()]`, pinned live by the boundary read (`[NEXT_DAY 00:00, day-after 00:00)` returns NEXT_DAY only) and by part B's 24h gap, which read the day row from a cursor on the skipped date.

**Claims check:** nothing falsified. Checked: suppression until the next UTC midnight (live A), one fact per suppressed fire published on its own tick (live A: 23:40/23:45 tick-path, 23:50/23:55 at gap close), the following day dispatching normally (live A/B), registry-not-memory override state (role reads via `list_skips` per tick), occurrence-beats-day precedence, idempotent storage keeping the first note, and that the unwired-port behavior is still exactly Story 2.2 (2.3's regression test passes unchanged).

**Deleted code check:** clean. The old single-kind `list_skips` query, `reason_for`/`__bool__` bodies and `group_skips` comprehension are re-established with the day clause; the `occurrence_skipped_for` rename re-established under `operator_skipped_for` with no other importers (grep-verified).

**Verification-gap layer:** no gaps filed. Traced consumers of the changed `list_skips` (dispatcher role port — covered live; no other consumers in the repo), the `SkipSet`/`group_skips` extension (unit + live, with a behavioral control: if day rows were dropped, the live skipped candybar would run at 23:40/23:45 and the `runs()` assertion would fail), CI wiring (store test in the live-Postgres step, role test by node id in the fault step), and contract drift (skipped reason is a free string since 1.2 — no schema rev needed).

**Deferred:** nothing.

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

## Independent Review (fresh context, 2026-10-26)

Fresh-context review by a non-implementing agent. It covered bb7f0e2, 2776031 and bff8f2f and the current rest-of-day store path, the `list_skips` day window, `SkipSet.rest_of_day`, and the tick and gap handling. The layers ran inline because no subagent-spawn tool was available. On throwaway nats:2.10.29 and postgres:17-alpine (`cbr2-*`), both the store test and the live midnight-boundary test passed (`test_stored_day_skip_suppresses_until_utc_midnight_live`), as did the gap-over-a-skipped-day unit rows. All three ACs hold. No patches.

### Review Findings

Rejected:
- low: unlike `next_fire`, which Story 5.3 bounded with a midnight jump, `previous_fire` walks back across rest-of-day dates one fire at a time. Measured: a minutely schedule with 8 consecutive skipped days resolves in 80ms. The dispatcher loads overrides only within [cursor+1m, tick], so the walk is bounded by the gap.
