---
title: 'Story 2.2: Misfire coalesce-to-latest after downtime'
type: 'feature'
created: '2026-10-26'
status: 'done'
route: 'dispatch'
baseline_commit: '7a41423fe15ab01e130c248d64016f1d20bf6515'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The dispatcher has no memory of the last tick it handled. Fires missed while the daemon was down are silently lost. If the dispatcher alone was down while the heartbeat kept publishing, the durable consumer replays the whole tick backlog as scheduled fires, which is the thundering herd AD-6 forbids.

**Approach:** Persist a durable last-seen tick cursor in the registry. When a consumed tick lands more than one minute past the cursor, coalesce each candybar's missed fires before dispatching the tick normally: the latest missed fire runs with `trigger=misfire`, and the earlier fires in the 24h window are recorded as `skipped` with `reason=misfire_coalesced`. Backlog ticks that have newer ticks queued behind them are acked without dispatch, so the newest tick coalesces the whole gap.

## Boundaries & Constraints

**Always:**
- Misfire detection keys off consumed ticks (AD-1). It never reads the wall clock and never sleeps.
- The cursor is a registry row, written only through `registry/store.py`. It only moves forward (`GREATEST`) and advances after every publish for the tick and before the ack.
- A missed fire is a cron fire time `m` with `cursor < m < tick` and `m >= created_at`.
- The latest missed fire always runs, even when it is older than 24h (anacron-style).
- Earlier missed fires are recorded only when they fall inside `[tick - 24h, latest)`.
- A misfire command reuses the idempotency key `candybar:<id>:tick:<tick_id of the missed minute>`, so a fire time executes at most once whatever its trigger. Command, invocation and skipped-event ids are UUIDv5 of `(candybar, missed tick_id)`.
- The tick's own matching fire still dispatches as `scheduled`.
- When the cursor is absent (fresh install), the tick is `<=` the cursor (redelivery or replay), or no `cursor` port is wired, behaviour is exactly the same as in Story 2.1.

**Never:**
- No misfire replay beyond the latest fire.
- Coalesced fires get no `skips` or `invocations` rows; `skips` stays operator override state for 2.3/2.4.
- No new event types.
- No change to executor semantics.
- Two concurrently running dispatchers are not coordinated beyond the shared idempotency keys (single node, AD-2).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| N>1 missed | cursor 10:00, `*/5`, tick 10:23 | 10:20 runs as misfire; skipped events for 10:05, 10:10 and 10:15 (in that order) | n/a |
| N=1 missed | cursor 10:00, `*/5`, tick 10:07 | 10:05 runs as misfire; no skipped events | n/a |
| Downtime >24h | cursor T-30h, hourly | One misfire run (latest). Skipped events only for fires `>= T-24h`. Warning logged about the cap | n/a |
| Latest older than 24h | weekly job, fire 3d ago, cursor 5d ago | That fire runs as misfire; no skipped events | n/a |
| No missed fires | `0 0 1 1 *`, or a gap with no fire | No misfire or skipped events; the tick dispatches normally | n/a |
| Created during downtime | `created_at` 10:12:30, `*/5`, cursor 10:00, tick 10:23 | 10:20 misfire, 10:15 skipped; 10:05/10:10 not considered | n/a |
| Contiguous / old / first tick | tick = cursor+1, tick <= cursor, or no cursor | Normal dispatch; cursor = max(cursor, tick) | n/a |
| Backlog tick | tick > cursor and `num_pending > 0` | Ack, no publish, cursor unchanged; the newest tick later coalesces the gap | n/a |
| Publish or cursor failure | Bus or DB error mid-plan | nak(delay=1); the redelivery rebuilds the same ids | Broker and executor dedupe |

</frozen-after-approval>

## Code Map

- `src/candybar/dispatcher/worker.py`
  - `Dispatcher.__init__(schedules, publish)` becomes `(schedules, publish, cursor=None)`. `publish` now also carries skipped events (`NatsEventSink.publish` takes any `Envelope`).
  - `dispatch(tick)` keeps returning the number of scheduled commands; SDK integration tests assert `== 1/2`.
  - `handle_message` reads `msg.metadata.num_pending` defensively. Unit-test `Message` fakes have no `metadata`, and nats raises `NotJSMessageError` without a reply subject.
  - Reuse `command_for`, `DISPATCH_NAMESPACE`, the nak/term floors.
- `src/candybar/dispatcher/schedule.py` -- add pure UTC helpers next to `matches_tick`: the latest fire strictly before a minute (`croniter.get_prev` is strict, verified), and the fires in `[start, end)`. Story 2.3 builds its skip-aware helper here.
- `src/candybar/dispatcher/role.py` -- wire a registry-backed cursor keyed on `DURABLE` into `Dispatcher`. Engine and session handling follow the existing `schedules()` closure.
- `src/candybar/registry/models.py`, `store.py`, `__init__.py`
  - New model `DispatchCursor(name PK, tick_minute timestamptz, updated_at)`; add it to `TABLES`.
  - `ScheduleEntry` gains `created_at: datetime | None = None`. It is positional-compatible, and existing tests build 3-arg entries.
  - `list_schedules` selects `created_at`.
  - New `get_dispatch_cursor` and `advance_dispatch_cursor`; the latter is a postgres upsert with `GREATEST`.
- `deploy/alembic/versions/0003_dispatch_cursor.py` -- additive table with a reversible downgrade. Heads must stay linear; check origin for a competing 0003 before committing.
- `src/candybar/events/builders.py` -- `build_candybar_skipped` gains an optional `event_id`, like the other builders.
- Do NOT change:
  - `executor/*`: it is trigger-agnostic, and `misfire` is already allowed by the row check, the env and the events.
  - `events/vocabulary.py`.
  - the fault suite's assertions: its candybars are created today, so the `created_at` floor excludes them from misfire on its historical ticks.
- Tests to reuse:
  - `tests/test_dispatcher.py` `Message` fake.
  - `tests/test_dispatcher_role.py`: its `FakeDispatcher` must accept `cursor`.
  - `tests/test_executor_integration.py` `_ensure_schema`, `_make_script`.
  - The fault suite's `_refuse_live_bus` / `_reset_streams` pattern.

## Tasks & Acceptance

**Execution:**
- [x] `src/candybar/registry/{models,store,__init__}.py` + `deploy/alembic/versions/0003_dispatch_cursor.py` -- cursor table, accessors, `created_at` on `ScheduleEntry`.
- [x] `src/candybar/events/builders.py` -- deterministic skipped `event_id`.
- [x] `src/candybar/dispatcher/{schedule,worker,role}.py` -- fire-time helpers, coalescing plan, backlog deferral, cursor wiring.
- [x] `tests/test_misfire.py` -- NEW unit tests: every matrix row, plus id determinism, nak on failure, and cursor untouched on failure.
- [x] `tests/test_dispatcher_role.py` -- assert that the cursor is wired with the `DURABLE` name.
- [x] `tests/test_misfire_integration.py` -- NEW live suite. It runs the production `run_dispatcher` + `run_executor` with real scripts, Postgres and JetStream, and covers both downtime shapes: a whole-daemon gap and a dispatcher-only backlog. The latter must be a real backlog, published while the role is stopped. It also runs the cap case.
- [x] `tests/test_registry_integration.py` -- the migration creates `dispatch_cursor`, and the upsert is monotonic.
- [x] `.github/workflows/ci.yml` -- run the live misfire suite in the fault-injection step (throwaway bus).

**Acceptance Criteria:**
- Given the live roles on a throwaway bus with a cursor N>1 fires behind (within 24h), when the dispatcher consumes the next tick, then each candybar has exactly one child run, with `CANDYBAR_TRIGGER=misfire` and the latest missed `tick_id`. One `invocations` row has `trigger=misfire`, and `BLOODBANK_EVENTS` holds exactly one `skipped` event (`reason=misfire_coalesced`) per earlier missed fire.
- Given downtime over 24h, then still exactly one misfire run, and no skipped event older than `tick - 24h`.
- Given candybars with no missed fire, then they get no misfire run or skipped event, and a match on the tick runs once with `trigger=scheduled`.

## Implementation Notes

I implemented this directly in this session. There is no subagent-spawn tool, the same deviation as Stories 1.1 to 2.1. The spec runs to about 2.5k tokens, over the 1600 guide, and I kept it whole: it is a single cohesive goal and the run is unattended. Commits on main: 5be2250 (spec), a1463e2 (code + unit tests), 7f48817 (live suite, cursor registry test, CI step), 04e33e9 (review patches).

**What was built**
- `dispatch_cursor` (migration 0003) has one row per tick durable. It advances with an atomic `INSERT … ON CONFLICT DO UPDATE SET tick_minute = GREATEST(...)`.
- The dispatcher reads the cursor on every tick and handles the tick in one of three ways:
  - A tick that is ahead of the cursor and has `num_pending > 0` is a backlog tick. It is acked and nothing is dispatched.
  - A tick more than one minute ahead coalesces through the pure `plan_misfires`, then dispatches normally.
  - In every case where the tick moves ahead of the cursor, the cursor advances only after all publishes and before the ack.
- `ScheduleEntry.created_at` is the misfire floor.
- Misfire and skipped ids are UUIDv5 of `(candybar, missed tick_id)` under the dispatcher namespace.
- `build_candybar_skipped` gained an optional `event_id`.

**Live proof** (production `run_dispatcher` and `run_executor`, throwaway nats:2.10.29 and postgres:17-alpine, `cb22-*`)
- Whole-daemon gap 10:00 → 10:23 on `*/5`: one child ran with `misfire|tick(10:20)`, one row, and the bus held skipped events for 10:05, 10:10 and 10:15. `23 10 * * *` ran once as scheduled; `0 0 1 1 *` did nothing.
- Real backlog of 18 ticks queued on the durable while the dispatcher role was stopped (`num_pending == 18` asserted): one misfire run for 10:40, skipped events for 10:25, 10:30 and 10:35, no scheduled replays, cursor at 10:41.
- 30h cap on an hourly job: one misfire run for 12:00 and 23 skipped events, all at or after T-24h.
- Sanity counter-test: with backlog deferral disabled (`_num_pending` forced to 0), the suite fails with the backlog replayed as `scheduled` runs. The change was reverted.
- The Story 2.1 fault suite still passes in both suite orders, including with a cursor seeded before its T1. The `created_at` floor (its candybars are registered today) keeps its historical ticks out of misfire.

**Verification results**
- 415 unit tests pass and ruff is clean on the changed files. Story 3.2's in-progress `tests/test_metrics.py` import order belongs to that agent and is uncommitted.
- Integration: heartbeat + dispatcher 4 passed / 1 skipped, fault + misfire 3 passed, registry 7 passed, executor/manual/fast-stop/SDK 7 passed.
- CI run 40280300437 is green on 7f48817. Its fault + misfire step reported 3 passed, and the step that runs Alembic to head applied 0003.

**Decisions where the spine is silent**
- The last-seen tick lives in the registry, not in the JetStream ack floor.
- A gap is detected on every tick, not only at process start.
- The latest missed fire runs even when it is older than 24h (anacron-style). The 24h window caps only the enumeration of skipped events.
- The tick's own matching fire still dispatches as `scheduled` alongside the misfire.
- Misfire commands share the scheduled idempotency key.
- Coalesced fires get no registry rows. Candystore is the audit, and `skips` stays override state for 2.3/2.4.
- A fresh install (no cursor) never misfires.

## Spec Change Log

## Review Triage Log

The three review layers (blind-hunter, edge-case-hunter, verification-gap) ran inline, because no subagent-spawn tool was available; the same deviation as 1.1 to 2.1. Diff: 7a41423..7f48817 over src, deploy, tests and .github, 60.4 kB, so the finding floor is N = min(floor(sqrt(60.4)+1), 10) = 8.

**Patched**
- *Blind + edge (one root cause).* A valid-but-never-firing schedule, such as `0 0 30 2 *`, passes registration. During a gap, `croniter.get_prev` raises `CroniterBadDateError`, which is a `ValueError`, so `dispatch` naks. The cursor never advances, so every later tick sees the same gap and fails too, and the dispatcher stops dispatching anything. Verified with a croniter probe. high, patched: `previous_fire` returns None and `fires_between` stops cleanly. The new test fails without the fix (nak instead of ack).
- *Blind.* The README had no misfire semantics for operators. low, patched with a short section under "Run the daemon"; only my hunk was staged, and Story 3.2's README edits were left unstaged.

**Rejected**
- *Blind.* If a tick is nak'd transiently and the next tick is already queued, the redelivery is deferred, so its fires run as `misfire` instead of `scheduled`. low, rejected: nothing is lost or doubled, and that minute really was not dispatched on time.
- *Blind.* A bogus future-minute tick on the bus moves the cursor forward and coalesces fires early. low, rejected: only a test can inject future ticks, and before this change a future tick already dispatched its own minute early. The heartbeat publishes only the current boundary (AD-4).
- *Blind.* Two concurrent dispatchers can emit a `skipped` fact for a fire the other one ran. low, rejected: this is excluded by the spec's Never (single node, AD-2), and executions stay once per key.
- *Blind.* A big plan (many minutely candybars after 24h) is published serially and could approach the 120s ack_wait. low, rejected: about 1ms per publish, and a pull consumer has no outstanding pull while busy, so the ack lands before any redelivery.
- *Blind.* A deferred tick also logs "dispatched 0 candybars". low, cosmetic, rejected; the deferral info line precedes it.
- *Blind.* Each tick now costs one extra registry read. low, rejected: one indexed PK read per minute.
- *Blind.* `RegistryTickCursor(engine: Any)` drops typing. low, rejected: the sole-writer guard forbids importing the sqlalchemy type outside `registry/`.
- *Edge.* `ceil_minute` on a naive `created_at` would assume local time. false: `created_at` is timestamptz, psycopg returns aware values, and production builds `ScheduleEntry` only from the database.
- *Verification-gap.* CI never runs the 2.1 fault suite with a cursor behind its T1, because CI starts from a fresh schema. rejected: the property rests on the `created_at` floor, which `test_fires_before_registration_are_not_missed` pins, and I verified it live by hand.

**Claims check:** nothing falsified. Checked: missed-fire definition, latest-always-runs, the window bounds, cursor ordering, the "exactly Story 2.1" fallbacks, and the deterministic ids.

**Deferred:** nothing.

## Design Notes

**Why the cursor lives in the registry and not the JetStream ack floor.** The ack-floor message can be purged by stream limits or lost when the stream or consumer is recreated, and the miss would then be silent. A registry row survives both.

**Why gap detection runs on every tick, not only at process start.** The first tick after startup is exactly where a gap shows up, so this still satisfies AD-6. The same rule also catches a heartbeat or bus outage, and a dropped tick, which is the "silent loss disguised as catch-up" that AD-6 names.

**Why backlog ticks are deferred.** Without the deferral, a dispatcher restarted behind a live heartbeat would replay every queued minute as a scheduled fire. With it, the newest tick coalesces the whole gap. If a deferred minute is followed by an out-of-order replay of an older minute, it is not lost: the next newer tick picks it up as part of its own gap.

Plan for tick T with `cursor < T - 1min`, per entry:

```
latest = prev_fire(schedule, T)                          # strictly < T
if latest > cursor and latest >= created_at:
    skipped += fires_in([max(cursor+1m, T-24h, ceil(created_at)), latest))
    misfire = latest
publish skipped (oldest first) -> misfire commands -> scheduled(T) -> cursor.advance(T) -> ack
```

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all unit tests pass.
- `uv run ruff check .` -- expected: clean.
- `CANDYBAR_TEST_NATS=1 CANDYBAR_TEST_PG=1 CANDYBAR_TEST_FAULT=1 uv run pytest tests/test_misfire_integration.py tests/test_dispatch_fault_injection_integration.py -q` -- on throwaway nats:2.10.29 + postgres:17-alpine. Expected: pass.
- `CANDYBAR_TEST_PG=1 uv run pytest tests/test_registry_integration.py -q` -- on the throwaway Postgres. Expected: pass.

## Independent Review (fresh context, 2026-10-26)

Fresh-context review by a non-implementing agent. It covered a1463e2, 7f48817 and 04e33e9 and the current `dispatcher/worker.py`, `schedule.py`, `role.py` and `registry/store.py` cursor code. The layers ran inline because no subagent-spawn tool was available. The live misfire suite passed on throwaway nats:2.10.29 and postgres:17-alpine (`cbr2-*`), covering the daemon gap, a real tick backlog and the 24h cap. The cursor registry tests passed too. All three ACs hold. No patches.

### Review Findings

Rejected:
- low: backlog deferral keys on `num_pending > 0`, meaning any queued tick, not only a newer one. If someone republishes an older tick behind the current one, the current tick is deferred and its fires run one minute later as `misfire` under the same idempotency key, so there is no double run. The heartbeat never publishes a past minute (`heartbeat/publisher.py`), so only a manual replay can trigger this. A precise fix needs a stream peek.
- low: replaying a tick that sits behind the cursor re-dispatches as `scheduled`. A fire that a gap recorded as `misfire_coalesced` has no row, so it would execute on such a replay. This is the frozen "tick <= cursor behaves exactly as Story 2.1" constraint. Changing it means editing the spec, so it is rejected. Only a manual replay of old ticks can reach it.
- low: a gap tick publishes up to 1,439 `skipped` facts per minutely candybar, one at a time. If that runs past the 120s ack_wait, the redelivery is fetched only after the first copy finishes (the role fetches one at a time). By then the cursor is at the tick, so the redelivery takes the normal path and every command dedupes. Correctness is unaffected.
