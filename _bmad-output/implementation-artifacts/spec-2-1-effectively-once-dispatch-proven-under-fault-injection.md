---
title: 'Story 2.1: Effectively-once dispatch proven under fault injection'
type: 'feature'
created: '2026-10-26'
status: 'in-progress'
baseline_commit: '2f8504658e4d848b45efa2becdae5918ad3fc1b6'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Epic 1 built stable command identity and registry dedupe, but nothing proves the composed daemon executes exactly once per `(candybar, tick)` and per manual UUID when ticks are redelivered, invoke commands are redelivered, or the dispatcher dies mid-tick. Investigation found two holes: the executor's in-process claim happens after several awaits (two concurrent deliveries of one key both execute), and tick event ids are random, so the broker cannot dedupe a re-published tick on `tick_id`.

**Approach:** Close both holes, pin the single-durable-consumer rule, and add a live fault-injection suite driving the real dispatcher and executor roles over throwaway NATS and Postgres.

## Boundaries & Constraints

**Always:** Dedupe keys are exactly `tick_id` (ticks; the tick event id and so its `Nats-Msg-Id` is a pure function of `tick_id`), `candybar:<id>:tick:<tick_id>` (scheduled), `candybar:<id>:manual:<uuid>` (manual). The executor claims an idempotency key synchronously before its first await and releases it on every exit path. The dispatcher role only ever binds the one production durable (`candybar-dispatcher-ticks`) on `BLOODBANK_EVENTS`; no code path creates an ephemeral or second tick consumer. The fault suite runs the production `run_dispatcher`/`run_executor` coroutines, opts in via env, and refuses a bus whose `BLOODBANK_*` streams already carry traffic (never the live bus).

**Never:** No misfire, skip, or last-seen-tick machinery (Stories 2.2–2.4). No schema migration. No change to the row-first terminal ordering from Story 1.6. No production timers or sleeps outside `heartbeat/`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Tick republished | Same tick built twice and published via the heartbeat sink | Second publish acks `duplicate`; one stored tick | n/a |
| Tick replay past dedupe window | Same tick_id under a fresh msg id | Dispatcher re-evaluates; identical command ids; broker dedupes commands; one execution | Ack tick |
| Dispatcher crash mid-tick | Two matches; crash after first command publish, before ack; restart | Tick redelivered after ack_wait; both commands published; each candybar runs once | First command deduped by msg id |
| Command redelivered after completion | Executor crashes after terminal publish, before ack; restart | Redelivery finds the row; republishes terminal; no second run | Ack |
| Duplicate command, distinct msg id | Same scheduled or manual command stored twice | One execution, one row | Second acked after republish |
| Concurrent same-key delivery | Two deliveries of one key in flight together | One spawn; the other naks and later dedupes | nak(delay) |
| Distinct manual UUIDs | Two manual commands, same candybar | Two executions | n/a |
| Two dispatcher roles / restarts | Roles started repeatedly or concurrently | Exactly one tick consumer on the stream, named `candybar-dispatcher-ticks` | n/a |

</frozen-after-approval>

## Code Map

- `src/candybar/events/{vocabulary,builders,__init__}.py` -- add `TICK_NAMESPACE` + `tick_event_id(tick_id)`; `build_tick_fired` defaults `event_id` to it (explicit `event_id` still wins). Heartbeat needs no change: it already publishes `Nats-Msg-Id = event.id`.
- `src/candybar/executor/worker.py` -- `Executor.handle_message`: `_inflight.add(key)` currently runs only just before `_run`, after the `_by_key`, `_entrypoint` and invoked-publish awaits. Move the claim right after the in-flight check; wrap the rest in try/finally discard (extract `_handle_claimed`). Keep all nak/ack/term semantics.
- `src/candybar/dispatcher/role.py` -- hoist `ack_wait=120` into `ACK_WAIT_SECONDS` so the suite can shorten it; consumer identity is already the module constant `DURABLE`.
- `src/candybar/executor/role.py` -- reuse `ACK_WAIT_SECONDS`/`DURABLE`; both roles import `NatsEventSink` into their module namespace, which is the crash-injection seam.
- `tests/test_heartbeat_integration.py` -- its dedupe/redelivery/virtual tests publish real-subject ticks for upcoming minutes; give them unique msg ids so deterministic ids do not dedupe them against each other or the live heartbeat.
- `tests/test_dispatcher_role.py` -- reuse its FakeNC/FakeJS pattern for the consumer-identity guard.
- `tests/test_executor_integration.py` -- reuse `_ensure_schema`, `_make_script`.
- `.github/workflows/ci.yml` -- add the fault suite to the NATS+PG integration step, before fast-stop (the container boot smoke needs the streams to exist afterwards).

## Tasks & Acceptance

**Execution:**
- [ ] `src/candybar/events/vocabulary.py`, `builders.py`, `__init__.py` -- deterministic tick event id -- broker dedupe keyed on tick_id.
- [ ] `src/candybar/executor/worker.py` -- synchronous key claim -- closes the concurrent double-execution race.
- [ ] `src/candybar/dispatcher/role.py` -- `ACK_WAIT_SECONDS` constant -- test seam, same value.
- [ ] `tests/test_events.py`, `tests/test_executor.py`, `tests/test_dispatcher_role.py` -- unit pins: tick id determinism; concurrent same-key yields one run; N role starts bind only `DURABLE` on `EVENT_STREAM` with matching `config.durable_name`, never `durable=None`; static guard that no other `src/candybar` module subscribes to tick subjects.
- [ ] `tests/test_heartbeat_integration.py` -- unique msg ids for test-published ticks.
- [ ] `tests/test_dispatch_fault_injection_integration.py` -- NEW live suite covering every matrix row against real roles, real scripts, and real rows.
- [ ] `.github/workflows/ci.yml` -- run the suite.

**Acceptance Criteria:**
- Given the walking skeleton on throwaway NATS and Postgres, when the suite redelivers ticks, redelivers commands, and restarts the dispatcher mid-tick, then every `(candybar, tick)` and every manual UUID has exactly one child execution, one `invocations` row, and counters equal to the row count.
- Given the rows, then their idempotency keys are literally `candybar:<id>:tick:<tick_id>` / `candybar:<id>:manual:<command_id>`, and the stored tick's `Nats-Msg-Id` derives only from `tick_id`.
- Given repeated and concurrent dispatcher role starts, then `BLOODBANK_EVENTS` has exactly one consumer filtering tick subjects, named `candybar-dispatcher-ticks`.

## Implementation Notes

## Spec Change Log

## Review Triage Log

## Design Notes

Crash injection subclasses the role's `NatsEventSink` and raises a `BaseException` subclass after a chosen real publish. The dispatcher's `except Exception` cannot turn that into a nak, so the message is left unacked, as after a real process death, and the role's `finally` still drains. The suite deletes and recreates the `BLOODBANK_*` streams at start, so the durables are rebuilt with the shortened ack_wait. It recreates them empty at teardown so later CI steps still find them.

Out of scope, recorded as deferred: a hard kill of the executor while a child is running leaves no row, so after `ack_wait` (1h) the command is redelivered and runs again. Closing that needs a claim row written before spawn, which changes 1.6's row-first accounting. The ACs' fault set (tick redelivery, command redelivery, dispatcher restart) does not include it.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all unit tests pass.
- `uv run ruff check .` -- expected: clean.
- `CANDYBAR_TEST_NATS=1 CANDYBAR_TEST_PG=1 CANDYBAR_TEST_FAULT=1 uv run pytest tests/test_dispatch_fault_injection_integration.py -q` -- on throwaway nats:2.10.29 + postgres:17-alpine, expected: pass.
- Full integration set (heartbeat, dispatcher, executor, manual, fast-stop) on the same throwaway pair -- expected: pass.
