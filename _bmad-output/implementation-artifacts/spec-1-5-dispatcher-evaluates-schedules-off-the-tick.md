---
title: 'Story 1.5: Dispatcher evaluates schedules off the tick'
type: 'feature'
created: '2026-10-26'
status: 'done'
baseline_commit: 'dbdf8c74c8a42fa520be00e731f88a9cf2f74aab'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Registered candybars are inert: ticks publish but no durable consumer evaluates schedules or creates work commands.

**Approach:** Add one dispatcher role consuming tick facts from JetStream, evaluate every registered five-field cron expression at the tick's UTC minute, and publish typed invoke commands for matches with stable per-candybar/tick identity.

## Boundaries & Constraints

**Always:** One durable pull consumer filtered to `bloodbank.evt.cron.tick.>`; use the shared `connect_nats` retry/drain posture. Derive evaluation time from validated tick payload, never receipt time or host TZ. Query registry through `registry/store.py` only; use existing `build_candybar_invoke`, subject `bloodbank.cmd.cron.candybar.invoke`, and `Nats-Msg-Id` = command id. Acknowledge a tick only after every matching command publish succeeds. Duplicate ticks have no second effective dispatch, including after restart and outside the JetStream duplicate window: persist a stable command id per `(candybar_id,tick_id)` (UUIDv5 namespace) and keep the consumer durable. Preserve at-least-once retry on publish failure.

**Never:** No clock/timer or APScheduler, no per-candybar subscriber, no SQL outside registry, no executor or Epic 2 misfire/skip machinery in this story. Do not stage concurrent provisioning files under `_bmad/`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Match | Valid registered `*/5 * * * *` at UTC 12:35 | One scheduled invoke command with spine idempotency key and matching tick_id | Tick ack after publish |
| Non-match | Same schedule at UTC 12:36 | No command | Tick ack |
| Redelivery | Same tick envelope consumed again after successful dispatch | Stable command id and key; no second effective command on JetStream | Ack redelivery |
| Publish failure | Two matching candybars, one command publish fails | Tick remains unacked; retry republishes stable ids for both | No silent loss |
| Invalid payload | Bad tick_id/minute or malformed envelope | No commands | Terminate/ack invalid message with log, avoid poison loop |

</frozen-after-approval>

## Code Map

- `src/candybar/dispatcher/{schedule.py,worker.py,role.py,__init__.py}` — UTC croniter matcher, tick handling, durable pull subscription, daemon lifecycle; NEW.
- `src/candybar/registry/store.py` — add registry-only read of `{id,name,schedule}` inventory; reuse engine/session_scope.
- `src/candybar/events/builders.py` — reuse builder, extend with optional caller-supplied event id; existing `command_id` and `invocation_id` alone do not stabilize `Nats-Msg-Id`.
- `src/candybar/bus.py` — reuse connect_nats and NatsEventSink.
- `src/candybar/daemon.py` — register dispatcher role.
- `tests/test_dispatcher.py` — match/miss/TZ, command identity, ack/error and role tests; update role-list assertion in `tests/test_heartbeat.py`.
- `tests/test_dispatcher_integration.py` — isolated JetStream stream/durable with real NATS, optional PG inventory integration; CI runs env-gated tests.
- `.github/workflows/ci.yml` — add dispatcher live-bus test step.

## Tasks & Acceptance

**Execution:**
- [x] `registry/store.py` — list registered schedule identities via registry boundary.
- [x] `dispatcher/schedule.py`, `worker.py`, `role.py` — match tick and durable pull/publish/ack lifecycle.
- [x] `daemon.py`, `dispatcher/__init__.py` — compose dispatcher role.
- [x] `tests/test_dispatcher.py`, `tests/test_dispatcher_integration.py` — matrix, timezone, redelivery, and real JetStream proof.
- [x] `.github/workflows/ci.yml`, `tests/test_heartbeat.py` — integration gate and role assertion.

**Acceptance Criteria:**
- Given registered candybars and ticks, when a tick UTC minute matches a five-field cron, then a single durable pull consumer publishes a scheduled invoke command with `candybar:<id>:tick:<tick_id>`.
- Given a non-UTC host TZ, when the same tick is evaluated, then matches are identical to UTC.
- Given a duplicate tick or a failed partial publish, when retried, then the same command identity is published and one effective dispatch results.
- Given non-matching minutes, when processed, then no invoke is published.

## Implementation Notes

Implemented single durable tick role, UTC cron matcher, registry-only inventory read, stable UUIDv5 envelope/command/invocation identity, ack-after-publish with NAK on operational failure and termination of poison messages. The existing event builder needed optional event id/time parameters: its command_id alone did not stabilize the CloudEvent id used for `Nats-Msg-Id`. CI now runs isolated real JetStream transport proof; unit suite 171 passed, 10 expected integration skips, integration 1 passed, ruff clean.

## Spec Change Log

## Review Triage Log

Review run inline because no subagent-spawn tool was available to this session; blind, edge-case, and verification-gap checks applied to the owned diff. Finding floor N=min(floor(sqrt(30.3)+1),10)=6. Findings and routes:
- Stable command id but random envelope id defeated JetStream dedupe — medium, patched with optional event id in builder and UUIDv5 envelope id.
- Stable ids but random envelope timestamp made redelivery's wire payload change — low, patched with tick minute timestamp.
- Invalid JSON object with valid type but invalid fields raises pydantic ValidationError; poison tick could crash role — medium, patched and tested.
- JSON array has no `.get`, so parser raises AttributeError; poison tick could crash role — medium, patched and tested.
- Role composition lacked a direct wiring test; connection/consumer/cleanup regression would pass other tests — medium verification gap, patched with role tests.
- Existing API role assertion expected only api+heartbeat; suite failed after adding dispatcher — low, patched.
- Spec originally called for `Nats-Msg-Id` = command id, while first implementation used a different stable event id — low, patched to make both identical.
- Integration uses isolated test subjects, so it proves JetStream dedupe and typed payload but not production subject routing — medium verification gap; production NatsEventSink subject/header already pinned in `tests/test_bus.py`; future full daemon+DB scenario deferred to Story 2.1's fault-injection suite.

## Design Notes

Stable UUIDv5 command and invocation ids scoped to `(candybar_id,tick_id)` make retries identity-preserving; existing builder derives the exact spine idempotency key. JetStream's duplicate window is finite; the executor's unique idempotency key (Story 1.6) is the durable last line for replay beyond that window. Story 2.1 owns full restart/fault-injection proof.

## Verification

**Commands:**
- `uv run pytest -q` — unit suite green.
- `uv run ruff check .` — lint green.
- `CANDYBAR_TEST_NATS=1 uv run pytest tests/test_dispatcher_integration.py -q` — JetStream command delivery/dedupe green.

## Independent Review (2026-10-26)

Fresh-context reviewer (not the implementer), bmad-code-review over the story's commits and the CURRENT tree (later epics reshaped shared modules). No subagent capability in this session, so the blind / edge-case / verification-gap / acceptance layers ran inline. Evidence classes: implemented + tested = unit suite 749 passed (`uv run pytest -q`), ruff clean; deployed = container rebuilt and redeployed per deploy/README.md after the patches, `/healthz` 200, next tick `tick:29883724` fired `deploy-smoke` (success, 19ms).

### Review Findings

- No `decision-needed`, `patch` or `defer` findings. Single durable pull consumer, UTC evaluation from the validated tick payload (`matches_tick` refuses non-UTC minutes), stable UUIDv5 command/event ids per (candybar, tick), ack only after every publish, poison ticks termed — all hold on the current tree (now extended by Epic 2's cursor/skips).
- Rejected: a `msg.ack()`/`term()` that raises would crash the daemon — false: nats-py buffers publishes while reconnecting, and the connection is only closed/draining after the loop exits.
