---
title: 'Story 5.4: Live updates over SSE'
type: 'feature'
created: '2026-10-26'
status: 'in-progress'
baseline_commit: '4d03e306305616ce23fb581858317e670b1cb376'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-5-context.md'
  - '{project-root}/_bmad-output/specs/spec-cron-framework/holocene-view-contract.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The Holocene schedule view can only refetch `/schedule` or poll `/schedule/next`. The contract's "Live updates" section and AD-11 require one SSE channel, `GET /schedule/stream`, whose events let the view patch its rows in place.

**Approach:** The api role runs one in-process stream hub. It is fed by an ephemeral, ordered JetStream consumer on `bloodbank.evt.cron.>` (spine paradigm: live-view updates are consumers of bus facts). It turns each fact into a stream event carrying contract rows built by `api/schedule.py`'s row computation. A bounded ring buffer with monotonic ids lets a `Last-Event-ID` reconnect replay exactly what the client missed.

## Boundaries & Constraints

**Always:**
- Facts to stream events: `invoked` → `invocation.started`; `completed`/`failed` → `invocation.completed` (`result` success|fail); `skipped` → `skip.applied`; `created`/`updated` → `definition.changed`. A tick, or a definition change, that changes the global current row → `current.advanced` `{cut, current: row|null}`. Ticks dedupe on `tick_id` (AD-5): a tick at or before the last handled minute is ignored.
- Every payload is absolute state, never a delta, and carries `type`, `seq`, `key` and `at`. Patch keys: `<candybarId>@<fireTime>` for a row, `definition:<id>`, `current`, and `invocation:<id>` for a manual run, which has no schedule row (`row: null`). Applying the same event twice changes nothing.
- A row equals what `/schedule` would return for that fire right now: state from the wall-clock `cut` and the hub's current row; `skipped` only for operator skip reasons, so `misfire_coalesced` → false; `lastResult` taken from the terminal fact. Definition-sourced fields come from the registry, overlaid with the fact, because the fact is published before the row commits.
- Optional `?from&to` window: both bounds or neither, validated as on `/schedule` (422 otherwise). Row events outside the window are not sent. `definition.changed` then also carries `rows`: that candybar's complete row set in the window, which replaces the client's rows for that candybar. `current.advanced` is always sent.
- SSE id `<boot>-<n>`. A fresh connection gets `ready` (`{cut, current}`, id = the latest n). A resumable `Last-Event-ID` replays every buffered event after n, with no gap and no duplicate. An unknown boot, an evicted or future n, or a malformed id → `reset`: the client refetches. A handling failure broadcasts one buffered `reset` per failure streak. A subscriber whose queue overflows is closed, then reconnects and resumes.
- No timer (AD-1): each tick sends a keepalive comment. Stop closes every stream, so the role still stops inside the 10s docker grace.

**Never:** No new Bloodbank event type or schema; the stream names are API-only. No durable consumer, no ack, no publish from the stream. No migration, no auth, no holocene changes. No skip or noise write endpoints (5.5, 5.6).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Tick advances current | tick 10:15; alpha `*/5` was current at 10:15 | `current.advanced` with cut 10:16, current alpha@10:20 | n/a |
| Duplicate or old tick | the same tick_id again, or an older one | nothing emitted | n/a |
| Scheduled run | `invoked`, then `completed` for alpha tick 10:15 | two events keyed `alpha@10:15`; the second row has `lastResult:"success"` | n/a |
| Manual run | `invoked` with tick_id null | `invocation.started` with `row:null`, key `invocation:<id>` | n/a |
| Operator skip / coalesced | `skipped` occurrence / `misfire_coalesced` | past row with `skipped` true / false | n/a |
| Schedule edit with window | PATCH schedule `*/10` | `definition.changed` + `rows` equal a refetch's rows for that candybar; `current.advanced` if current moved | n/a |
| Resume | `Last-Event-ID` = id of event k | exactly events k+1..last, in order | unresumable → `reset` |
| DB down while handling | registry read fails | one `reset` until a fact is handled again | logged |

</frozen-after-approval>

## Code Map

- `src/candybar/api/schedule.py` -- ScheduleRow/to_json, WindowCandybar, global_next/next_row, window_rows, attach_results, fire_key, first_tick_after, window_bounds, skip_read_start. Add a shared state rule and a one-candybar row helper; window_rows keeps its results (the 33 tests of 5.2 must pass unchanged).
- `src/candybar/api/app.py` -- create_app(session_factory, sink, metrics_registry, now). Add `stream=` and the route `GET /schedule/stream`. Reuse `problem` and `candybar_row_json`.
- `src/candybar/api/role.py` -- run_api: build the hub, subscribe the feed, close streams on stop.
- `src/candybar/events/{envelope,parse}.py` -- the typed facts. tick_id on invocation and skipped facts is the fire's own tick, including misfire.
- `src/candybar/registry/store.py` -- get_candybar, list_candybars, list_skips (open-ended), fire_results. DB access goes only through here (AD-10 guard).
- `tests/test_dispatcher_role.py::test_no_other_module_subscribes_to_ticks` -- allow the api's ordered consumer, which is non-durable and dedupes on tick_id per AD-5.
- `tests/test_api_role.py` -- the fakes need `stream=` and `jetstream().subscribe`.
- `tests/test_api_schedule.py` -- Rig and skips_in, for the patch-equals-refetch tests.
- `tests/contract_shapes.py` -- add a parse of the "Live updates" triggers.
- `tests/test_manual_invoke_integration.py` -- the isolated-stream RerouteSink pattern for the live test.

## Tasks & Acceptance

**Execution:**
- [ ] `src/candybar/api/schedule.py` -- `row_state`, `candybar_rows`, `fire_of_tick`; window_rows uses `row_state` -- one row computation for /schedule, /schedule/next and the stream.
- [ ] `src/candybar/api/stream.py` -- `ScheduleStream` hub (handle, subscribe, render, close), `STREAM_TRIGGERS` mapped to the contract phrases, SSE framing.
- [ ] `src/candybar/api/app.py` + `role.py` -- the route (headers `text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`), the feed on `BLOODBANK_EVENTS` (an ordered consumer, deliver NEW; a failure is logged and REST keeps serving), and stop wiring.
- [ ] `tests/test_schedule_stream.py` -- each matrix row; a contract test (the triggers parsed from the contract and a row check on every row); patched snapshot == refetch for every trigger; resume, reset, window filter, overflow, close.
- [ ] `tests/test_schedule_stream_integration.py` + CI -- live NATS + Postgres: real register/PATCH and production envelopes on an isolated stream, a real uvicorn and HTTP SSE client, patched == refetch, Last-Event-ID resume with no duplicate; `run_api` with a connected client stops within budget.
- [ ] Guard and role tests updated; README section.

**Acceptance Criteria:**
- Given the daemon's lifecycle events, when a consumer subscribes to `GET /schedule/stream`, then an SSE event is emitted for each of the contract's five triggers, and applying the events to a `/schedule` snapshot yields that window's refetch.
- Given a dropped connection, when the client reconnects with `Last-Event-ID`, then it receives each missed event exactly once, and re-applying any event is a no-op.

## Design Notes

Client patch rule (documented in the README): on `current.advanced`, every row with `fireTime < cut` is `past`, the row whose key matches `current` is `current`, and the rest are `upcoming`. On a row event, set the row at its key. On `definition.changed`, set `name`, `schedule` and `noise` on that candybar's rows, or replace them with `rows` when a window was given. Rendering happens at delivery, so a replay reflects the current state; payloads are absolute, so replays converge.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all pass
- `uv run ruff check .` -- expected: clean
- `CANDYBAR_TEST_NATS=1 CANDYBAR_TEST_PG=1 DATABASE_URL=<throwaway> uv run pytest tests/test_schedule_stream_integration.py -q` -- expected: pass on throwaway nats:2.10.29 + postgres:17-alpine
