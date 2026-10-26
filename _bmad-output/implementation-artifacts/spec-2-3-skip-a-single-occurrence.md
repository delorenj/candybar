---
title: 'Story 2.3: Skip a single occurrence'
type: 'feature'
created: '2026-10-26'
status: 'in-progress'
route: 'dispatch'
baseline_commit: '90c0adbb02a628a82f3177e4ea23d5ff9196cc26'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** An operator cannot suppress one known-bad fire of a candybar. The `skips` table exists (0001) but nothing writes it and the dispatcher never reads it, so every cron fire dispatches.

**Approach:** Add a registry write path for occurrence skips `{candybar_id, fire_time}` and a window read. Teach the shared croniter helpers to omit skipped fires. The dispatcher loads the tick's skips before emitting commands: a skipped fire publishes `bloodbank.cron.candybar.skipped` (`reason=occurrence`) instead of an invoke command, and later fires dispatch normally.

## Boundaries & Constraints

**Always:**
- Skips are registry rows (`kind='occurrence'`, `fire_time` = UTC minute), written and read only through `registry/store.py` (AD-10). Rows persist after they are honored; they are override state that Epic 5 renders.
- `add_occurrence_skip` is idempotent: the row id is UUIDv5 of `(candybar_id, fire_time)` with insert-on-conflict-do-nothing, so a repeat stores nothing new and says so. Unknown candybar raises `UnknownCandybarError`; a naive or off-minute `fire_time` raises `ValueError`.
- The skipped fact for fire `m` has the deterministic id UUIDv5 `skipped:occurrence:<candybar>:<tick_id(m)>`, `tick_id = tick_id(m)`, and is caused by the consumed tick. Tick redelivery republishes the same id.
- Shared helpers in `dispatcher/schedule.py` take an optional per-candybar `SkipSet`: `fires_between` and a new `next_fire` (upcoming fires, the AC's helper) omit skipped fires, and `previous_fire` returns the latest non-skipped fire.
- Misfire coalescing (2.2) uses the skip-aware fires. An operator-skipped fire in a gap is never the misfire run; the latest non-skipped missed fire runs instead. Skipped fires inside the 24h window get one `skipped` fact with `reason=occurrence`, the rest keep `misfire_coalesced`, published oldest first.
- With no skips port wired, behaviour is exactly Story 2.2.

**Never:**
- No skip API or CLI. Story 5.6 owns `POST /candybars/{id}/skip`, including the 404 for a fire time the schedule never produces, using `matches_tick`.
- No migration and no new event types.
- Skips never touch manual invokes, the executor, or a fire whose tick was already dispatched before the skip was stored.
- No rest-of-day skips (Story 2.4 extends `SkipSet`).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Skipped tick fire | `*/5`, skip 10:10, tick 10:10 | No command; one skipped fact (`occurrence`, tick 10:10); cursor advances | n/a |
| Next fire | same, ticks 10:15 | Scheduled command as usual | n/a |
| Other candybar | skip on A at 10:10, B `*/5` | B dispatches 10:10 | n/a |
| Upcoming helper | `next_fire(*/5, 10:06, skip 10:10)` | 10:15; `fires_between(10:00,10:30)` omits 10:10 | n/a |
| Skip in a gap, latest | cursor 10:15, skip 10:25, tick 10:28 | 10:20 misfire; skipped 10:25 `occurrence` | n/a |
| Skip in a gap, earlier | cursor 10:00, skip 10:10, tick 10:23 | 10:20 misfire; skipped 10:05 + 10:15 coalesced, 10:10 `occurrence`, in time order | n/a |
| Duplicate store | same `{id, fire_time}` twice | One row, second call reports not-created | n/a |
| Unknown candybar / bad time | random UUID; naive or 10:10:30 | Nothing stored | `UnknownCandybarError` / `ValueError` |
| Skips read fails | DB error loading skips | Nothing published; tick nak'd | redelivery rebuilds the same ids |

**Decisions (unattended run, spine silent):** the event reason is the skip kind token `occurrence` (2.4 adds `rest_of_day`), not free operator text, which stays on the row; a skipped latest missed fire hands the misfire run to the latest non-skipped one, so the skip suppresses exactly one occurrence and the catch-up still happens.

</frozen-after-approval>

## Code Map

- `src/candybar/registry/models.py` -- `Skip` model exists (`kind`, `fire_time`, `day`, `reason`, `created_at`); add `SKIP_OCCURRENCE = "occurrence"` and `SKIP_KINDS` next to `TRIGGERS`.
- `src/candybar/registry/store.py` -- add `SkipEntry` (frozen dataclass, no ORM leak), `UnknownCandybarError`, `occurrence_skip_id`, `add_occurrence_skip(session, candybar_id, fire_time, *, reason=None) -> tuple[SkipEntry, bool]` (check `Candybar` exists, then `pg_insert(Skip).on_conflict_do_nothing(index_elements=[Skip.id]).returning(Skip.id)`; also map an FK `IntegrityError` race to `UnknownCandybarError`), and `list_skips(session, start, end)` for occurrence rows with `start <= fire_time < end`. Reuse `_require_tick_minute`. Export from `registry/__init__.py`.
- `src/candybar/dispatcher/schedule.py` -- `SkipSet(occurrences: frozenset[datetime])` with `reason_for(fire)`, `NO_SKIPS`, `group_skips(rows) -> dict[UUID, SkipSet]`; skip-aware `previous_fire`/`fires_between` via keyword `skips=NO_SKIPS` (positional callers unchanged); new `next_fire(schedule, at, *, skips)` = first non-skipped fire `>= at`, None if it never fires.
- `src/candybar/dispatcher/worker.py`
  - `Dispatcher(..., skips=None)` takes the port `Callable[[datetime, datetime], Awaitable[list[SkipEntry]]]`, loaded once per dispatched tick over `[last+1m if gap else minute, minute+1m)` after the backlog check.
  - `plan_misfires(..., skips=None)`: `MisfirePlan` keeps `skipped` (coalesced; the existing test pins it) and gains `overridden` (operator-skipped fires in the window).
  - New `occurrence_skipped_for`. The tick path publishes it for a skipped matching fire, and it is not counted in the `dispatch` return value.
- `src/candybar/dispatcher/role.py` -- wire a `skips()` closure on `list_skips` (same session pattern as `schedules()`).
- Do NOT change: `executor/*`, `events/*` (the `skipped` builder already takes `event_id`), the 2.2 fallbacks, the migrations.
- Tests to reuse: the `tests/test_misfire.py` `Harness`/`Message`/`FakeCursor` pattern; `tests/test_dispatcher_role.py` `FakeDispatcher` (must accept `skips`); `tests/test_misfire_integration.py` scaffold (`_refuse_live_bus`, `_reset_streams`, `_skipped_events`, `ENV_LOG_SCRIPT`, cursor snapshot/restore).

## Tasks & Acceptance

**Execution:**
- [ ] `src/candybar/registry/{models,store,__init__}.py` -- skip kind vocabulary, `SkipEntry`, idempotent `add_occurrence_skip`, `list_skips`.
- [ ] `src/candybar/dispatcher/schedule.py` -- `SkipSet`, `group_skips`, skip-aware helpers, `next_fire`.
- [ ] `src/candybar/dispatcher/{worker,role}.py` -- skips port, tick-path suppression, skip-aware coalescing, role wiring.
- [ ] `tests/test_skips.py` -- NEW: every matrix row except the DB-bound ones, plus deterministic ids, redelivery identity, and a no-skips-port regression.
- [ ] `tests/test_dispatcher_role.py` -- assert the skips port is wired.
- [ ] `tests/test_skip_occurrence_integration.py` -- NEW live suite. Store: idempotence, unknown candybar, window read. Roles: production `run_dispatcher` + `run_executor` on a throwaway bus; a stored skip suppresses exactly that fire, the next fire runs, and a gap with a skipped latest fire runs the earlier fire as misfire.
- [ ] `.github/workflows/ci.yml` -- run the live suite: store test in the registry step, role test in the fault + misfire step.
- [ ] `README.md` -- short skip-semantics note under the misfire section.

**Acceptance Criteria:**
- Given a registered candybar and a stored occurrence skip for its 10:10 fire, when the live dispatcher consumes ticks 10:05, 10:10 and 10:15, then exactly two child runs happen (10:05 and 10:15, `trigger=scheduled`), no `invocations` row or command exists for 10:10, and `BLOODBANK_EVENTS` holds exactly one `skipped` fact with `reason=occurrence` and `tick_id` 10:10.
- Given the same registry state, when `next_fire`/`fires_between` compute upcoming fires with the candybar's `SkipSet`, then 10:10 is absent and 10:15 is present.

## Implementation Notes

## Spec Change Log

## Review Triage Log

## Design Notes

Per entry, at tick T with cursor C (gap when `T - C > 1m`). `S` is the entry's `SkipSet`:

```
gap:  latest = previous_fire(sched, T, skips=S)      # walks past skipped fires
      if latest >= floor: misfire(latest)
      window fires in [max(floor, T-24h), T) except latest:
         S.reason_for(f) -> skipped(f, "occurrence") else skipped(f, "misfire_coalesced")
tick: matches(T) and T in S -> skipped(T, "occurrence")   else command(T)
publish: all skipped (oldest first) -> misfire -> tick -> cursor.advance(T) -> ack
```

Skipped fires newer than `latest` and inside the window are always enumerated. A skipped fire older than T-24h gets no fact, the same cap as 2.2.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all unit tests pass.
- `uv run ruff check .` -- expected: clean.
- `CANDYBAR_TEST_NATS=1 CANDYBAR_TEST_PG=1 CANDYBAR_TEST_FAULT=1 uv run pytest tests/test_skip_occurrence_integration.py tests/test_misfire_integration.py tests/test_dispatch_fault_injection_integration.py -q` -- on throwaway nats:2.10.29 + postgres:17-alpine. Expected: pass.
