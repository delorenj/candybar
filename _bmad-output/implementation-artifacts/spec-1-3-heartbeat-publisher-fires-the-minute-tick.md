---
title: 'Story 1.3: Heartbeat publisher fires the minute tick'
type: 'feature'
created: '2026-10-25'
status: 'done'
baseline_commit: '6fd72fc2bcc81ed22ec1cd1f3c2fa584bb2392dc'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The `bloodbank.cron.tick.fired` vocabulary exists (story 1.2) but nothing publishes it — the system has no clock, so no scheduled work can ever react to time (AD-1: the heartbeat publisher is the ONLY component allowed to own a timer).

**Approach:** Implement the heartbeat role: pure minute-boundary clock math, a publisher loop that sleeps to each UTC minute boundary and publishes `build_tick_fired(<that scheduled minute>)` via JetStream with `Nats-Msg-Id` = event id, NATS wiring with connect-retry, a `candybar daemon [--roles ...]` CLI entry composing roles in one asyncio process (AD-2), unit tests with injected clock/sleep, and env-gated integration tests against a real NATS — an accelerated virtual-time subscriber test in CI plus a literal 5-minute real-time run executed locally.

## Boundaries & Constraints

**Always:**
- Tick identity (AD-4): `tick_id = tick:<unix_epoch_minute>` derived from the SCHEDULED boundary minute, captured before sleeping — never re-read from the wall clock at publish time. `ordering_key` and `data.tick_id` both carry it (builders already enforce this).
- JetStream publish with header `Nats-Msg-Id` = event id for server-side dedupe (AD-14); subject is the envelope's `subject` field; payload is the envelope's JSON (`model_dump_json`).
- One tick per UTC minute boundary from one heartbeat role instance; loop is `asyncio` with an injectable `now`/`sleep` so tests never wait real minutes.
- Clock ownership (AD-1): only `src/candybar/heartbeat/` may contain sleep-until-a-time primitives; a static guard test scans the rest of `src/candybar/` for them.
- NATS URL from env var `NATS_URL` (ecosystem convention, cf. bloodbank services), default `nats://localhost:4222` for dev; reconnect forever after connect; initial connect retried with backoff until stopped; `nc.drain()` on shutdown.
- Roles via CLI (AD-2): `candybar daemon --roles heartbeat[,dispatcher,...]`; default all implemented roles (today: heartbeat only); unknown role name is a usage error (exit 2). SIGINT/SIGTERM stop roles gracefully.
- UTC RFC 3339 everywhere; no new runtime dependencies (nats-py 2.16.0 already pinned); ruff + pytest green; commits straight to origin main.

**Never:**
- No scheduler/timing libraries (APScheduler, sched, croniter-in-heartbeat) — the heartbeat is plain asyncio boundary math.
- No per-candybar subscribers, no dispatcher/executor/registry/API role implementations (stories 1.4+).
- No changes to bloodbank repo (registration landed in 1.2) — this story only emits to it.
- No Plane tickets; no holocene changes; no LAN IPs or secrets; do not stage the untracked `_bmad/config.yaml` / `_bmad/custom/*.toml` (concurrent provisioning agent).
- No Prometheus metrics yet (story 1.7); no misfire catch-up in the heartbeat (dispatcher-side, Epic 2).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Minute tick | loop awake at a UTC minute boundary | exactly one `bloodbank.evt.cron.tick.fired` publish, subject `bloodbank.evt.cron.tick.fired`, header `Nats-Msg-Id` == event id, `tick_id`/`ordering_key` = `tick:<epoch_minute>` of the scheduled boundary | publish raises → retry same event (3 attempts, 1s backoff), then drop the tick and continue (missed ticks are the dispatcher's misfire domain) |
| Clock skew after sleep | now() jumps past the boundary during/after sleep | tick still built for the SCHEDULED minute (boundary captured before sleeping), not the skewed publish time | N/A |
| Mid-minute start | daemon starts at 12:34:56.789 | first tick is `tick:` of 12:35:00 boundary; nothing published for 12:34 | N/A |
| Stop during sleep | SIGINT/SIGTERM while sleeping | loop exits without publishing a partial/extra tick; connection drains | N/A |
| NATS down at startup | connect fails | retry connect every 2s until success or stop; no crash loop exit | stop event set → clean exit |
| NATS drops mid-run | connection lost | nats-py reconnects (max_reconnect_attempts=-1); publish retry covers transient failures | publish ultimately fails after retries → drop tick, keep looping |
| Duplicate publish (bus retry) | same envelope published twice | second ack has `duplicate=True`; subscriber sees one stored message | N/A |
| Tick redelivery | consumer doesn't ack; bus redelivers | redelivered message carries the SAME `tick_id` and event id (payload immutable) | N/A |
| Clock ownership | any `asyncio.sleep(`/`time.sleep(`/`.call_later(`/`.call_at(`/`sched.scheduler`/APScheduler outside `src/candybar/heartbeat/` | static guard test fails | N/A |
| Bad roles flag | `--roles heartbeat,bogus` | usage error naming the unknown role, exit != 0, nothing started | N/A |

</frozen-after-approval>

## Code Map

- `src/candybar/heartbeat/__init__.py` — EXISTS (docstring skeleton): replace with public exports.
- `src/candybar/heartbeat/clock.py` — NEW: pure boundary math (`next_minute_boundary(now)`, `seconds_until(target, now)`), no I/O.
- `src/candybar/heartbeat/publisher.py` — NEW: `TickSink` protocol, `NatsTickSink` (JetStream publish + `Nats-Msg-Id`), `Heartbeat` loop (injected now/sleep, scheduled-minute capture, publish retry policy, stop event).
- `src/candybar/heartbeat/role.py` — NEW: `run_heartbeat(stop)` — NATS connect-retry wiring, sink construction, drain.
- `src/candybar/daemon.py` — NEW: role registry `{name: role coroutine factory}` + `run_daemon(roles, stop)` via `asyncio.gather`.
- `src/candybar/cli/main.py` — EXISTS: add `daemon` subcommand (`--roles`, signal handlers, `asyncio.run`).
- `src/candybar/events/builders.py` — REUSE `build_tick_fired` (enforces UTC minute boundary, deterministic tick_id). DO NOT change.
- `src/candybar/events/parse.py` — REUSE `parse_envelope` for the test subscriber. DO NOT change.
- `tests/test_heartbeat.py` — NEW: unit tests (boundary math, scheduled-minute pinning under skew, mid-minute start, stop-during-sleep, retry policy, Nats-Msg-Id header, clock-ownership scan, CLI roles validation).
- `tests/test_heartbeat_integration.py` — NEW: env-gated (`CANDYBAR_TEST_NATS=1`) live-NATS tests: virtual-time accelerated subscriber run, duplicate-publish ack, redelivery tick_id stability; literal 5-minute real-time run additionally gated by `CANDYBAR_TICK_E2E=1`.
- `.github/workflows/ci.yml` — EXISTS: add pinned `nats-server` binary step (v2.10.29, matches live bloodbank-nats) with `-js`, run integration tests with `CANDYBAR_TEST_NATS=1`.
- `README.md` — EXISTS: one short section on `candybar daemon`.

## Tasks & Acceptance

**Execution:**
- [x] `src/candybar/heartbeat/clock.py` -- pure boundary math -- testable without I/O.
- [x] `src/candybar/heartbeat/publisher.py` -- sink protocol + NatsTickSink + Heartbeat loop -- the only clock in the system (AD-1, AD-4).
- [x] `src/candybar/heartbeat/role.py` + `src/candybar/daemon.py` -- NATS wiring + role composition -- AD-2/AD-14 process shape.
- [x] `src/candybar/cli/main.py` -- `daemon` subcommand -- "daemon running with the heartbeat role enabled" entry point.
- [x] `tests/test_heartbeat.py` -- unit tests codifying every I/O matrix row that needs no bus.
- [x] `tests/test_heartbeat_integration.py` -- live-bus tests: subscriber observation (virtual-time), dedupe, redelivery identity, 5-minute e2e flag.
- [x] `.github/workflows/ci.yml` -- nats-server step + integration run -- CI guards the JetStream path.
- [x] `README.md` -- daemon usage note.
- [x] Commit + push (`git pull --rebase` first; stage only owned files).

**Acceptance Criteria:**
- Given the daemon running with the heartbeat role and NATS JetStream reachable, when a UTC minute boundary passes, then exactly one `bloodbank.cron.tick.fired` fact is published with `tick_id = tick:<unix_epoch_minute>` as both `ordering_key` and `data.tick_id`, with `Nats-Msg-Id` = event id (proven by unit tests + live integration).
- Given a tick redelivered by the bus, when inspected, then it carries the same `tick_id` (deterministic, wall-clock-derived) — proven by the redelivery integration test.
- Given `src/candybar/`, when the clock-ownership guard test runs, then no module outside `heartbeat/` contains sleep-until-a-time primitives.
- Given a live NATS, when a test subscriber runs over a 5-minute window, then it observes one tick per minute with small skew — executed locally in this session (env-gated) and evidence recorded in Implementation Notes; CI runs the accelerated virtual-time equivalent.

## Implementation Notes

- Implemented directly (no subagent capability in the build session — same deviation as 1.1/1.2, recorded in Review Triage Log).
- Live verification against bloodbank-nats (localhost:4222, server 2.10.29): accelerated virtual-time subscriber test, `Nats-Msg-Id` duplicate-ack test, and redelivery-identity test all pass (3 passed in ~8s). The literal 5-minute e2e ran with `CANDYBAR_TICK_E2E=1`: **1 passed in 298.36s** — one tick per minute, 5 distinct consecutive tick_ids, receipt skew < 5s each.
- Production smoke through the real CLI entry: `timeout -s TERM 75 uv run candybar daemon --roles heartbeat` — connected, published `tick:29883066` at the boundary (42ms receipt skew), drained on SIGTERM, exit 0. First live emission of the cron domain on the real bus, as sanctioned by 1.2's `active` registration.
- Decision (spine silent): `run_daemon` uses `asyncio.TaskGroup` — a crashed role cancels siblings and exits nonzero rather than running half-alive.
- Decision (spine silent): production shutdown is immediate — the heartbeat sleeps via `asyncio.wait_for(stop.wait(), timeout=seconds-to-boundary)`, not a blind `asyncio.sleep(60)`.
- Decision (spine silent): a boundary already past at wake (suspend/skew) fires immediately rather than being skipped — late-but-correct; gaps are the dispatcher's misfire domain (AD-6).
- Ruff's BLE001 forced the connect-retry catch from blind `Exception` to `(nats.errors.Error, OSError, TimeoutError)` — verified against the nats.errors hierarchy and a live refused connection.
- PubAck.duplicate is `None` (not False) when the server does not flag a duplicate — the dedupe test asserts `not ack1.duplicate` accordingly.
- Matrix audit: all 10 I/O rows covered by named passing tests, including the two bus-dependent rows via live integration runs.
- Verified: `uv run pytest -q` 99 passed / 4 integration-skipped; `CANDYBAR_TEST_NATS=1 uv run pytest tests/test_heartbeat_integration.py` 3 passed / 1 skipped; `uv run ruff check .` clean; `candybar daemon --help` exit 0; `candybar daemon --roles bogus` exit 2.

## Spec Change Log

## Review Triage Log

Review layers ran inline in the build session (no subagent spawn capability — same recorded deviation as stories 1.1/1.2). Blind-hunter floor: N = min(floor(sqrt(50.5 kB)+1), 10) = 8; 13 findings triaged. All findings verified against the tree; patches applied and full verification re-run green (99/99 pytest + 3 live integration, ruff clean).

- `publisher.py` `js.publish` has no explicit timeout — **false** — verified: nats-py's `JetStreamContext` defaults `timeout=5`, and `publish` falls back to `self._timeout`; a hung ack raises `TimeoutError` into the retry path. No unbounded stall exists.
- `daemon.py run_daemon` raises bare `KeyError` on an unknown role — **false** — the only caller (CLI) validates via `resolve_roles` first; a direct misuse fails loudly, never silently.
- `run_daemon` TaskGroup composition untested — **verification gap (pre-verified)** — a TaskGroup wiring mistake would ship green: role/resolve tests never execute `run_daemon` → patched (`test_run_daemon_starts_roles_and_stops` drives a stub role through it).
- CLI `daemon` happy path untested — **verification gap (pre-verified)** — only the rejection path ran under CliRunner; signal-handler/`asyncio.run` wiring could break undetected → patched (`test_cli_daemon_happy_path_runs_and_exits_clean` with stubbed `run_daemon`).
- `heartbeat/__init__` exports `PUBLISH_MAX_ATTEMPTS` but not `PUBLISH_BACKOFF_SECONDS` — **low, rejected** — no caller needs the backoff constant; no named harm.
- CI downloads nats-server without a checksum — **low, rejected** — version-pinned release URL; repo threat model (solo dev, free-tier CI); a checksum line adds maintenance burden for no everyday harm.
- `role.py` connect-retry logged a full traceback every 2s while NATS is down — **low, patched** — hours of downtime would flood logs with identical tracebacks; now a one-line `log.warning` per attempt (which forced the precise exception tuple under BLE001).
- `resolve_roles(" , ,")` silently selected zero roles → daemon exits 0 doing nothing — **low, patched** — an operator typo or empty env expansion would look like a healthy run; now a `ValueError` (exit 2) plus test.
- `daemon.py` docstring mapped the api role to "1.4+" — **low, patched** — api is Epic 5 (1.4 is registry); wrong signpost for the next developer; one-line correction.
- Retry backoff `_pause` is not stop-interruptible (≤2s extra shutdown latency) — **low, rejected** — bounded and negligible; an interruptible backoff adds complexity for no everyday harm.
- Duplicate-publish integration test stores a near-future tick on the live bus — **false as defect** — test emission is sanctioned (1.2 registered the domain `active` for exactly this); consumers dedupe on `tick_id` per AD-5.
- `_ensure_events_stream` creates the CI stream without the canonical 7d `max_age` — **false** — CI-local throwaway server; on the live bus the helper no-ops after `stream_info`.
- CLI `daemon` never configured logging — **medium, patched** — found while tracing verification: the role logs publishes at INFO but no handler/level was set, so a deployed daemon would emit nothing observable; `logging.basicConfig(INFO)` added and proven by the live smoke (publish + drain lines visible).

## Design Notes

Publish-failure policy (spine silent): retry the SAME event object (same id → `Nats-Msg-Id` dedupes server-side) up to 3 attempts with 1s backoff, then drop the tick and continue; missed-tick catch-up is the dispatcher's misfire domain (AD-6), not the heartbeat's. Integration cadence: the subscriber AC is proven fast in CI via injected virtual clock (wall seconds drive successive virtual minutes through the real JetStream publish/consume path) and literally (5 real minutes) via the locally-run `CANDYBAR_TICK_E2E=1` test against bloodbank-nats — first live emission of the cron domain, sanctioned by 1.2's `active` registration. `NATS_URL` (not a candybar-prefixed var) matches the bloodbank services convention; the deploy story (1.9) wires the container value.

## Verification

**Commands:**
- `uv run pytest -q` -- unit + scaffold + events tests pass; integration tests skip without `CANDYBAR_TEST_NATS=1`.
- `CANDYBAR_TEST_NATS=1 uv run pytest tests/test_heartbeat_integration.py -q` -- live-bus tests pass against localhost NATS (skip the 5-minute e2e without `CANDYBAR_TICK_E2E=1`).
- `CANDYBAR_TEST_NATS=1 CANDYBAR_TICK_E2E=1 uv run pytest tests/test_heartbeat_integration.py -q` -- includes the literal 5-minute subscriber window.
- `uv run ruff check .` -- clean.
- `uv run candybar --help` / `uv run candybar daemon --help` -- exit 0.
- `uv run candybar daemon --roles bogus` -- exit != 0.

## Independent Review (2026-10-26)

Fresh-context reviewer (not the implementer), bmad-code-review over the story's commits and the CURRENT tree (later epics reshaped shared modules). No subagent capability in this session, so the blind / edge-case / verification-gap / acceptance layers ran inline. Evidence classes: implemented + tested = unit suite 749 passed (`uv run pytest -q`), ruff clean; deployed = container rebuilt and redeployed per deploy/README.md after the patches, `/healthz` 200, next tick `tick:29883724` fired `deploy-smoke` (success, 19ms).

### Review Findings

- [x] [Review][Patch] A stop during a NATS outage hung every role until SIGKILL [src/candybar/bus.py:connect_nats] — medium. With `max_reconnect_attempts=-1`, nats-py retries the INITIAL connect forever inside `nats.connect` (`_select_next_server` never gives up), so it never raises while the bus is down: the stop-interruptible retry loop was dead code, the matrix row "NATS down at startup → stop event set → clean exit" did not hold, and nats-py's default error_cb logged a full traceback every 2s (the flood the 1.3 triage thought it had fixed). Verified live: `nats.connect` to a refused port only returned when an outer `wait_for` cut it at 7s. Fixed in 68b878a: each attempt is raced against `stop` and cancelled, and a one-line `error_cb` replaces the traceback. Pinned against the REAL client on a refused port (`test_connect_nats_real_client_stops_during_bus_outage`; times out without the fix). The existing tests used a raising double that nats-py never behaves like (verification gap, closed by the same test).
- Rejected: early wake (wall vs monotonic skew) can make the loop re-capture the same boundary and publish its tick twice — low: the tick event id is deterministic (`tick_event_id`), so `Nats-Msg-Id` dedupes the second publish and consumers key on `tick_id`; the fix would add state for no observable harm.
