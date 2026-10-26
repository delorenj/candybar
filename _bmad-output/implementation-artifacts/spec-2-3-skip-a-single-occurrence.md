---
title: 'Story 2.3: Skip a single occurrence'
type: 'feature'
created: '2026-10-26'
status: 'done'
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
- [x] `src/candybar/registry/{models,store,__init__}.py` -- skip kind vocabulary, `SkipEntry`, idempotent `add_occurrence_skip`, `list_skips`.
- [x] `src/candybar/dispatcher/schedule.py` -- `SkipSet`, `group_skips`, skip-aware helpers, `next_fire`.
- [x] `src/candybar/dispatcher/{worker,role}.py` -- skips port, tick-path suppression, skip-aware coalescing, role wiring.
- [x] `tests/test_skips.py` -- NEW: every matrix row except the DB-bound ones, plus deterministic ids, redelivery identity, and a no-skips-port regression.
- [x] `tests/test_dispatcher_role.py` -- assert the skips port is wired.
- [x] `tests/test_skip_occurrence_integration.py` -- NEW live suite. Store: idempotence, unknown candybar, window read. Roles: production `run_dispatcher` + `run_executor` on a throwaway bus; a stored skip suppresses exactly that fire, the next fire runs, and a gap with a skipped latest fire runs the earlier fire as misfire.
- [x] `.github/workflows/ci.yml` -- run the live suite: store test in the registry step, role test in the fault + misfire step.
- [x] `README.md` -- short skip-semantics note under the misfire section.

**Acceptance Criteria:**
- Given a registered candybar and a stored occurrence skip for its 10:10 fire, when the live dispatcher consumes ticks 10:05, 10:10 and 10:15, then exactly two child runs happen (10:05 and 10:15, `trigger=scheduled`), no `invocations` row or command exists for 10:10, and `BLOODBANK_EVENTS` holds exactly one `skipped` fact with `reason=occurrence` and `tick_id` 10:10.
- Given the same registry state, when `next_fire`/`fires_between` compute upcoming fires with the candybar's `SkipSet`, then 10:10 is absent and 10:15 is present.

## Implementation Notes

I implemented this directly in this session. There is no subagent-spawn tool, the same deviation as Stories 1.1 to 2.2. The spec runs to about 2.4k tokens, over the 1600 guide, and I kept it whole: it is a single cohesive goal and the run is unattended. Commits on main: 1754461 (spec), ee04dc5 (code + unit tests), 8c45a74 (live suite, CI, README), 96c6145 (review patches).

**What was built**
- Registry: `SKIP_OCCURRENCE`/`SKIP_KINDS`, the `SkipEntry` dataclass, `UnknownCandybarError` (a `KeyError`, like the executor's unknown-candybar path), and `occurrence_skip_id`. `add_occurrence_skip` checks that the candybar exists and inserts with `ON CONFLICT (id) DO NOTHING RETURNING id`. It also maps an FK race to `UnknownCandybarError`. `list_skips` reads a half-open UTC window. No migration was needed, because the 0001 `skips` columns already fit.
- Schedule helpers: `SkipSet`, `NO_SKIPS` and `group_skips` (unknown kinds are ignored). `previous_fire` and `fires_between` take `skips=` as a keyword, so existing callers are unchanged. The new `next_fire` is the upcoming-fire helper.
- Dispatcher: the `skips` port is read once per dispatched tick, after the backlog check. On a gap it reads from `cursor+1m`. A skipped matching fire publishes `occurrence_skipped_for` instead of a command. `MisfirePlan` gains `overridden`, and `latest` may be None when every missed fire is skipped. Skipped facts are published oldest first, then the misfire run.
- The role wires the port through `list_skips`.

**Live proof** (production `run_dispatcher` and `run_executor`, throwaway nats:2.10.29 and postgres:17-alpine, `cb23-*`)
- Setup: a skip stored on one of two `*/5` candybars for 10:10, then ticks 10:05, 10:10 and 10:15, each dispatched in turn.
  - The skipped candybar ran 10:05 and 10:15 as scheduled, with no 10:10 row or key and counters at 2/2.
  - The neighbour ran all three.
  - The bus held exactly one `skipped` fact (`occurrence`, 10:10).
  - `next_fire`/`fires_between` over the same registry rows omit 10:10.
- Gap with the latest missed fire skipped: cursor 10:15, a skip at 10:25, tick 10:28.
  - The skipped candybar ran 10:20 as misfire, and 10:25 was recorded as `occurrence`.
  - The neighbour ran 10:25 as misfire, and 10:20 was recorded as `misfire_coalesced`.
- Sanity counter-test: with `group_skips` forced to return `{}`, the live suite fails because 10:10 runs.
- Test fix during implementation: the first live draft published the three ticks in a burst, and the dispatcher handled them as a 2.2 backlog. That is correct behaviour but made the test racy, so each tick now waits for the cursor to advance.

**Verification results**
- 466 unit tests pass and ruff is clean.
- Live: the skip suite 2 passed. The fault, misfire, dispatcher and heartbeat suites gave 9 passed and 1 skipped (the 5-minute end-to-end test). The registry suite 8 passed.

**Decisions where the spine is silent**
- Skip row ids are UUIDv5 of `(candybar, fire minute)`, so storing is idempotent without a unique-index migration. They are not UUIDv7, which stays the convention for candybar ids.
- The event reason is the kind token (`occurrence`). The operator's free-text note stays on the row only.
- Rows are kept after they are honored, as override state Epic 5 renders.
- A fire time that is not UTC is refused rather than converted.
- The skip API and CLI are out of scope (Story 5.6), and the store does not check the fire time against the schedule.

## Spec Change Log

## Review Triage Log

The three review layers (blind-hunter, edge-case-hunter, verification-gap) ran inline, because no subagent-spawn tool was available; the same deviation as 1.1 to 2.2. Diff: 90c0adb..8c45a74 over src, tests, .github and README, excluding Story 3.2's `executor/gpu.py` and `tests/test_gpu.py` that landed in between. It is 62.0 kB, so the finding floor is N = min(floor(sqrt(62.0)+1), 10) = 8.

**Patched** (96c6145)
- *Blind + Code Map deviation.* `store.py` had a second UTC-minute validator, `_require_fire_minute`, beside `_require_tick_minute`. The Code Map said to reuse the existing one, and two copies of one rule would drift. low, patched: one validator with a `what` label.
- *Blind.* The >24h gap warning still said "latest missed fire still runs", but a skipped latest fire now hands the run to an earlier one. low, patched: the text says "latest non-skipped".
- *Blind.* The `Dispatcher.dispatch` docstring did not mention the skips read or the skipped-fact publish. low, patched.
- *Verification-gap.* The FK-race mapping in `add_occurrence_skip` keys on the constraint name `skips_candybar_id_fkey`, which the 0001 migration does not name explicitly. No test pinned it, so a different name would leak a raw `IntegrityError` on the race path. Patched: the live store test asserts the name from `pg_constraint`. Verified against the throwaway database.

**Rejected**
- *Blind.* `list_skips` scans the whole `skips` table on every tick, and rows are never deleted. low, rejected: the table is operator-sized and read once a minute, and an index needs a migration, which is more than a direct correction.
- *Blind + edge.* `add_occurrence_skip` reads the row back after the insert, and the read could return None if the candybar were deleted at the same moment. false: nothing in `src/` deletes candybars (grep found no `session.delete` or `delete(`), so the row cannot vanish in between.
- *Blind.* `UnknownCandybarError` subclasses `KeyError`, so `str()` comes out quoted. low, rejected: cosmetic, it matches the executor's `KeyError` convention, and 5.6 writes its own problem+json detail.
- *Blind.* A skip stored between a failed gap attempt and its redelivery yields two `skipped` facts for one fire (`misfire_coalesced` then `occurrence`). low, rejected: this needs a mid-plan publish failure plus an operator write inside the 1s nak delay. Executions are unaffected.
- *Blind.* Replaying a tick older than the broker's duplicate window republishes the occurrence fact. low, rejected: the id is the same, which is the same at-least-once contract every other fact has, and consumers key on the id.
- *Blind.* `SkipSet.__bool__` is unused in production. false: there is no harm to name.
- *Blind.* `group_skips` drops unknown kinds silently. low, rejected: only a daemon rollback can hit it, and the fix adds a logging branch.
- *Edge.* A gap with a very old cursor reads skips back to that cursor without a bound. low, rejected: same operator-sized table as above, and the read must reach the floor so that a skipped latest fire is honored.
- *Edge.* A row written by hand-run SQL with an off-minute `fire_time` never matches. low, rejected: the store is the sole writer (AD-10) and enforces minute alignment.

**Claims check:** nothing falsified. Checked:
- `reason=occurrence` and the skipped-fact id format;
- idempotent storage that reports a repeat as not-created;
- the unknown-candybar and bad-time errors;
- the skip-aware `previous_fire`, `fires_between` and `next_fire`;
- that an unwired port is exactly Story 2.2 (same fire sets, only the gap log text differs);
- that rows persist after they are honored;
- the CI placement of both live tests.

**Deleted code check:** clean. The old `previous_fire` try/except and the `earlier` enumeration are re-established in the new loops. The unconditional misfire publish is now conditional on `latest`, which is intentional for an all-skipped gap.

**Deferred:** nothing.

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

## Independent Review (fresh context, 2026-10-26)

Fresh-context review by a non-implementing agent. It covered ee04dc5, 8c45a74 and 96c6145 and the current skip store, `SkipSet`/`group_skips`, the skip-aware helpers and the dispatcher tick and gap paths. The layers ran inline because no subagent-spawn tool was available. On throwaway nats:2.10.29 and postgres:17-alpine (`cbr2-*`), both the store idempotence/window read and the live-roles test passed (`test_stored_skip_suppresses_exactly_that_fire_live`). Both ACs hold. No patches.

### Review Findings

Rejected:
- false: "a schedule that registration accepts can make the dispatch helpers raise, so every tick naks forever and the dispatcher wedges for all candybars". Fuzzing 20k random 5-field expressions found that every one the manifest validator accepts (130) runs `matches_tick`, `previous_fire`, `fires_between` and `next_fire` without raising. A hand list of L, W, # and range forms behaved the same.
