---
title: 'Story 5.4: Live updates over SSE'
type: 'feature'
created: '2026-10-26'
status: 'done'
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
- [x] `src/candybar/api/schedule.py` -- `row_state`, `candybar_rows`, `fire_of_tick`; window_rows uses `row_state` -- one row computation for /schedule, /schedule/next and the stream.
- [x] `src/candybar/api/stream.py` -- `ScheduleStream` hub (handle, subscribe, render, close), `STREAM_TRIGGERS` mapped to the contract phrases, SSE framing.
- [x] `src/candybar/api/app.py` + `role.py` -- the route (headers `text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`), the feed on `BLOODBANK_EVENTS` (an ordered consumer, deliver NEW; a failure is logged and REST keeps serving), and stop wiring.
- [x] `tests/test_schedule_stream.py` -- each matrix row; a contract test (the triggers parsed from the contract and a row check on every row); patched snapshot == refetch for every trigger; resume, reset, window filter, overflow, close.
- [x] `tests/test_schedule_stream_integration.py` + CI -- live NATS + Postgres: real register/PATCH and production envelopes on an isolated stream, a real uvicorn and HTTP SSE client, patched == refetch, Last-Event-ID resume with no duplicate; `run_api` with a connected client stops within budget.
- [x] Guard and role tests updated; README section.

**Acceptance Criteria:**
- Given the daemon's lifecycle events, when a consumer subscribes to `GET /schedule/stream`, then an SSE event is emitted for each of the contract's five triggers, and applying the events to a `/schedule` snapshot yields that window's refetch.
- Given a dropped connection, when the client reconnects with `Last-Event-ID`, then it receives each missed event exactly once, and re-applying any event is a no-op.

## Implementation Notes

- Unattended run under the workflow harness's no-questions directive. Checkpoint 1 was auto-approved ("Approve and continue"), with no Open Questions: every choice the spine and contract leave open is decided in the frozen block. The spec is about 2,100 tokens, over the 1,600 target. It has one goal (the stream), so it was kept whole. This session has no subagent tool, so implementation and review run inline, the same deviation recorded for 1.1 to 5.3.
- g33 preflight passed. The ecosystem route came back as event naming (Bloodbank) and UI (Holocene). No Bloodbank type or schema is added; the stream's event names are API-level only. Holocene is not touched.
- `epic-5-context.md` was valid, so it was loaded instead of the raw planning docs. Continuity came from the done 5.3 spec.
- What was built:
  - `api/schedule.py`: `row_state` is now the one state rule. `schedule_row`, `candybar_rows`, `is_fire_of`, `tick_minute` and `current_key` are added, and `window_rows` builds its rows through `schedule_row`. The Tasks line's `fire_of_tick` shipped as `tick_minute`.
  - `api/stream.py`: the `ScheduleStream` hub, `STREAM_TRIGGERS`, SSE framing and `parse_fact`.
  - `api/shapes.py`: `utc_iso` and `candybar_row_json` moved here from `app.py`, so the hub can build definitions without importing the app. `app.py` re-imports them, so `candybar.api.app.candybar_row_json` still resolves.
  - `api/app.py`: the `GET /schedule/stream` route and `create_app(stream=)`.
  - `api/role.py`: `subscribe_feed`, an ordered consumer on `BLOODBANK_EVENTS` with deliver NEW. `hub.start()` runs alongside uvicorn. Stop closes the streams, and `timeout_graceful_shutdown=3` is a backstop.
  - The AD-5 subscription guard now admits the api feed and asserts it is ordered, with no durable and no queue.
- Decisions the frozen block leaves to the code:
  - Window-filtered row events go out as id-only frames. These move the client's `Last-Event-ID` and dispatch nothing.
  - A shutdown close flushes the queued frames first. An overflow close drops them, and the client resumes from the buffer.
  - `ready` and `reset` carry `key` and `at` like every other payload.
  - A run whose fire the current schedule no longer produces gets `row: null`, as a refetch has no such row.
- Tests:
  - `tests/test_schedule_stream.py` has 37 tests: every matrix row, the contract trigger parse, patched == refetch through all five triggers, resume, the reset cases, window, overflow, close, failure streaks and the route.
  - `tests/test_api_role.py` gained the feed wiring, the non-blocking start, the feed-attach failure and the fact parsing tests.
  - `tests/test_schedule_stream_integration.py` is the live HTTP + NATS + Postgres proof. It also covers `run_api` stopping with an open stream before uvicorn's backstop fires. It is in the CI executor step, last.
- Mutation checks, each of which fails its tests:
  - no tick dedupe; `misfire_coalesced` read as skipped; no definition overlay; resume off by one (unit and live); no window rows;
  - no `hub.close()` on stop (live: 3.11s, the backstop fired); a blocking `hub.start()`; `str.isdigit()`-only ids.
- Verification:
  - 664 unit tests pass and ruff is clean.
  - On throwaway nats:2.10.29 and postgres:17-alpine, every CI live step passed: NATS 4 passed and 1 skipped; Postgres 15 passed and 2 skipped; fault, misfire and skips 5 passed; executor, manual, fast-stop, SDK and stream 7 passed.
  - One run of the stream, manual-invoke and fast-stop set reported 1 failure, right after a full unit run, and I did not capture which test it was. 26 reruns passed, including runs under concurrent load.
- Real daemon smoke: `candybar daemon` with all four roles ran on the throwaway services, with `curl -N` on `/schedule/stream`. It showed:
  - `ready`, then `current.advanced` and `definition.changed` from a real register;
  - on each real minute tick, a keepalive, `current.advanced`, `invocation.started` and `invocation.completed` (success);
  - `Last-Event-ID` replaying exactly 6 to 8, and a foreign boot getting `reset`;
  - `candybar run` streaming `invocation:<id>` events with `row: null`;
  - SIGTERM with the stream open stopping in 1.1s, with the curl ending.
  Everything was torn down afterwards. The live candybar container was not touched or redeployed.

## Spec Change Log

## Review Triage Log

The three layers ran inline because this session has no subagent tool: blind-hunter, edge-case-hunter (with the deletion and claims checks) and verification-gap. This is the same deviation as 1.1 to 5.3. The blind-hunter floor was N = min(floor(sqrt(119.7) + 1), 10) = 10.

| # | Layer | Finding | Verdict | Evidence / route |
|---|-------|---------|---------|------------------|
| 1 | blind-hunter | `run_api` awaited `hub.start()` (a registry read) before uvicorn served, so a hung DB held REST and `/healthz`. | medium | Patched: start runs as a task under the hub lock and is cancelled on stop. A role test whose start never answers proves uvicorn serves and stop completes; restoring the `await` fails it. |
| 2 | edge-case-hunter | A `Last-Event-ID` of `<boot>-²` passes `str.isdigit()`, then `int()` raises inside the open response. Starlette decodes headers as latin-1, so the input is reachable. | low | Patched: ASCII digits only, with a parametrized case. The `isdigit`-only mutation fails it. |
| 3 | edge-case-hunter (claims) | "Every payload ... carries type, seq, key and at", but `ready`/`reset` had no `key`/`at`. | low | Patched: `_notice` stamps both, asserted on ready, unresumable reset and update_failed reset. |
| 4 | blind-hunter | `seq` restarts at 1 per boot. A client keeping applied seqs across a reset would ignore a restarted hub's events. | low | Patched: README and module docstring say to forget applied seqs on `reset`. The new boot always forces a reset, so the rule is sufficient. |
| 5 | verification-gap | Removing `hub.start()` or `timeout_graceful_shutdown` from `run_api` failed no test. The unit role test's fake engine makes start fail silently. | low | Patched: role tests assert start is called and the backstop value. |
| 6 | verification-gap | `create_app`'s default hub (no `stream=`) had no stream test. | low | Patched: `test_an_app_built_without_a_hub_still_serves_the_stream`. |
| 7 | blind-hunter | Manual-run events carry no `name`, so the view must look the candybar up. | low | Rejected: the fix adds public surface, and the view already keys on `candybarId` against `GET /candybars`. |
| 8 | blind-hunter | `ready.current` is cached, so it can be a past fire while the heartbeat is down. | low | Rejected: this needs the heartbeat down. `/schedule/next` recomputes per request and is the fallback, and a guard here would be a new branch for a rare state. |
| 9 | blind-hunter | `definition.changed` with a window costs two registry reads per subscriber. | low | Rejected: operator-rate events for one to three viewers. |
| 10 | blind-hunter | A non-cron message on the cron subjects logs one warning per message. | low | Rejected: only candybar publishes in the `cron` domain, and the filter excludes foreign subjects. |
| 11 | blind-hunter | The 3s graceful backstop also cancels slow REST requests at stop. | low | Rejected: before this they ran into docker's SIGKILL at 10s, so this is an improvement. |
| 12 | blind-hunter | The ordered consumer may lose facts across a NATS reconnect. | false | nats-py's `activity_check` resets an ordered consumer from `_sseq + 1` after two missed heartbeats (`nats/js/client.py:779-806`). |
| 13 | blind-hunter | `StreamEvent.body` is one dict shared by every subscriber. | false | Nothing mutates it: `_render` builds a new dict (`{**body, "rows": ...}`). |
| 14 | blind-hunter | A feed that fails to attach leaves the stream silent. | low | Rejected: this is the frozen decision (log at error, REST keeps serving), and it is tested. |
| 15 | blind-hunter | The contract, the only holocene handoff, does not name the SSE event vocabulary. | low | Deferred: the Epic 6 handoff must carry README "Live updates" (see deferred-work.md). Editing the delegated contract is outside this story's conformance scope. |
| 16 | edge-case-hunter (claims) | The Tasks line names `fire_of_tick`; the code ships `tick_minute`. | low | Rejected: naming only, recorded in Implementation Notes. |
| 17 | edge-case-hunter (claims) | "Row events outside the window are not sent", but they go out as id-only frames. | low | Rejected: an EventSource dispatches nothing for an id-only frame; only its cursor moves, which keeps resume ids fresh. Documented and tested. |
| 18 | edge-case-hunter (deletion) | `window_rows`' inline state logic was replaced, and the check order changed from current-first to past-first. | false | `global_next` never returns a fire before `cut`, so the order is equivalent. The 33 tests of 5.2 and 15 of 5.3 pass unchanged. |
| 19 | edge-case-hunter | An `updated` fact for an unknown candybar returns without an event or a test. | low | Rejected: unreachable, since no delete path exists in `src/`, and it is logged. |
| 20 | edge-case-hunter (claims) | Other claims checked: ordered, ephemeral feed; ticks deduped on tick_id; rows equal a refetch (`misfire_coalesced` not skipped, `lastResult` from the fact, overlay before commit); window validation; resume, reset, overflow and close; one reset per streak; no timer; stop inside the grace; no durable, ack, publish, migration or Bloodbank type. | — | None falsified except #3, #16 and #17. |

## Design Notes

Client patch rule (documented in the README): on `current.advanced`, every row with `fireTime < cut` is `past`, the row whose key matches `current` is `current`, and the rest are `upcoming`. On a row event, set the row at its key. On `definition.changed`, set `name`, `schedule` and `noise` on that candybar's rows, or replace them with `rows` when a window was given. Rendering happens at delivery, so a replay reflects the current state; payloads are absolute, so replays converge.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all pass
- `uv run ruff check .` -- expected: clean
- `CANDYBAR_TEST_NATS=1 CANDYBAR_TEST_PG=1 DATABASE_URL=<throwaway> uv run pytest tests/test_schedule_stream_integration.py -q` -- expected: pass on throwaway nats:2.10.29 + postgres:17-alpine
