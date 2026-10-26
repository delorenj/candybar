---
status: final
stepsCompleted:
  - step-01-validate-prerequisites
  - step-02-design-epics
  - step-03-create-stories
  - step-04-final-validation
inputDocuments:
  - _bmad-output/specs/spec-cron-framework/SPEC.md
  - _bmad-output/specs/spec-cron-framework/holocene-view-contract.md
  - _bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md
---

# candybar - Epic Breakdown

## Overview

This document provides the complete epic and story breakdown for candybar, decomposing the requirements from the SPEC (SPEC-cron-framework, acting as the requirements source — no separate PRD exists in this express-mode run), the delegated Holocene view contract, and the Architecture Spine (ARCHITECTURE-SPINE, 15 ADs) into implementable stories.

## Requirements Inventory

### Functional Requirements

- FR1: The system publishes one heartbeat tick per minute onto Bloodbank as `bloodbank.cron.tick.fired` with deterministic `tick_id = tick:<unix_epoch_minute>` (CAP-1; AD-1, AD-3, AD-4).
- FR2: Registered Candybars fire at each time their standard-cron schedule dictates, evaluated by the dispatcher against the UTC tick minute via croniter, and each invocation's success/fail result is recorded (CAP-2; AD-5, AD-6, AD-13).
- FR3: Every invocation is metered to Prometheus — runtime (wall ms histogram), memory (cgroup peak gauge), token spend (counter), CPU/GPU load (summaries) — on series labeled `{candybar_id, result}` with per-invocation `invocation_id` exemplars (CAP-3; AD-9).
- FR4: A Postgres registry holds the Candybar inventory (name, description, tags, schedule) plus `total_invoked` / `total_successful` counters updated in the same transaction as the terminal invocation row (CAP-4; AD-10).
- FR5: SDKs for Python, Node, and Rust let a Candybar be authored natively; all three implement the same process protocol — invocation context in via env vars, result JSON out at `CANDYBAR_RESULT_FILE` (CAP-5; AD-8).
- FR6: Any executable registers as a Candybar without an SDK; exit code 0 = success, nonzero = fail, unmeasured metrics report zero (CAP-6; AD-7, AD-8).
- FR7: `candybar run <registryId>` triggers an immediate manual invocation that rides the identical dispatcher/executor/registry/metrics path as a scheduled fire (`trigger=manual`) (CAP-7; AD-12).
- FR8: A read/control API exposes: definitions CRUD (`GET/POST /candybars`, `GET /candybars/{id}`), schedule window (`GET /schedule?from&to`), next-fire poll (`GET /schedule/next`), live stream (`GET /schedule/stream` SSE), noise collection (`PUT/DELETE /candybars/{id}/noise`, `PUT /noise/batch`), and skip operations (`POST /candybars/{id}/skip`, `POST /candybars/{id}/skip-rest-of-day`) — shapes per `holocene-view-contract.md` (CAP-8; AD-11).
- FR9: Skip operations are schedule overrides stored in the registry, honored by the dispatcher before emitting invoke commands, and reflected in computed upcoming fire times and schedule rows (`skipped: true`) (spec assumption; contract; AD-13).
- FR10: Registration metadata (name, description, tags, schedule, entrypoint) is declared in a `candybar.toml` manifest consumed by the CLI/API register path (AD-8).
- FR11: Every lifecycle state change emits its `bloodbank.cron.*` fact (`candybar.{created,updated,invoked,completed,failed,skipped}`) as one logical unit with the registry mutation (Consistency Conventions; AD-3).

### NonFunctional Requirements

- NFR1: Bloodbank is the only scheduling transport; the heartbeat publisher is the single timer in the system — no crontab, no private timer loops, no scheduler libraries that own clocks (AD-1).
- NFR2: Schedules use the standard 5-field cron format only; all cron evaluation and fire-time computation is UTC (AD-13).
- NFR3: Existing infrastructure only — NATS JetStream (Bloodbank), shared Postgres, Prometheus + Grafana, Candystore; no new data stores (spec constraint; AD-15).
- NFR4: Secrets only as `op://DeLoSecrets/...` references or env vars; service references are `*.delo.sh` hostnames; never LAN IPs or plaintext secrets (AD-15).
- NFR5: Effectively-once dispatch via idempotency keys (`candybar:<id>:tick:<tick_id>` scheduled, `candybar:<id>:manual:<uuid>` manual) over at-least-once ticks; only the dispatcher's single durable consumer evaluates schedules (AD-5).
- NFR6: Misfire policy is anacron-style coalesce-to-latest with a 24h catch-up cap; earlier missed fires are recorded as `skipped` with `reason=misfire_coalesced` (AD-6).
- NFR7: One Python asyncio deployable (one container, one systemd unit) composing heartbeat/dispatcher/executor/api roles; single node, no HA (AD-2, AD-15).
- NFR8: Prometheus label cardinality is bounded to `{candybar_id, result}`; per-invocation identity rides exemplars and the full record lives in events + the `invocations` table (AD-9).
- NFR9: The daemon is the sole writer of the registry database; counters never drift from invocation history (AD-10).
- NFR10: The `cron` event domain (entities `tick`/`candybar`, action `fired`) and JSON schemas MUST be registered in the bloodbank repo (`docs/event-naming.md`, `schemas/`) PR-first before first emission (AD-3).
- NFR11: Every invocation runs as a cgroup v2 scoped subprocess spawned by the executor; SDKs never open NATS connections (AD-7, AD-8).
- NFR12: CI runs on GitHub-hosted runners (public repo); deploy is one docker container behind Traefik at `candybar.delo.sh` with Prometheus scraping `/metrics` (AD-15).

### Additional Requirements

- Cross-repo governance action (precondition for first event emission): PR the `cron` domain, entities `tick`+`candybar`, action `fired`, and all event/command JSON schemas into the `bloodbank` repo (`docs/event-naming.md`, `schemas/`). Draft schemas live in this repo's `schemas/` seed directory (AD-3; Structural Seed).
- Registry bootstrap: `candybar` database on the shared homelab Postgres with alembic migrations; ERD = `candybars` / `invocations` / `skips` (AD-10; Structural Seed).
- Executor metering attribution: runtime = wall ms; memory = cgroup `memory.peak`; CPU = cgroup `cpu.stat` delta / runtime; GPU = `nvidia-smi pmon` per-pid sampling when a GPU is present, else 0; token spend self-reported by SDK, default 0 (AD-9).
- Error mapping: nonzero exit or missing result file → `failed` with stderr tail (max 500 chars, redacted) in event data (Consistency Conventions).
- Data formats: ISO 8601 / RFC 3339 UTC timestamps everywhere; API errors are RFC 7807 problem+json; candybar names unique slugs `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`; registry ids UUIDv7 (Consistency Conventions).
- NATS usage: nats-py JetStream publish with `Nats-Msg-Id` dedupe; durable pull consumer for ticks; workqueue durable for invoke commands; no Dapr sidecar in the daemon (AD-14).
- Envelope conventions: `kind`, `actor.type: "scheduler"`, `ordering_key` (`tick:<tick_id>` or `candybar:<id>`), `correlationid`/`causationid`; commands add `command_id`, `idempotency_key`, `delivery: single_consumer` (AD-3).
- Auth: edge-only (Cloudflare Access / LAN) in v1; no in-app authz (Consistency Conventions).
- Pinned stack: Python ≥ 3.13, fastapi 0.162.0, uvicorn 0.54.0, nats-py 2.16.0, croniter 6.2.4, psycopg 3.3.6, sqlalchemy 2.1.6 + alembic 1.20.1, prometheus-client 0.26.0, pydantic 2.14.1, typer 0.27.3; Node SDK floor Node 24 LTS; Rust SDK floor 1.99.1 (Stack table).

### UX Design Requirements

None in this repo. The only UI — the Holocene gmail-inbox-style schedule view — is DELEGATED to the `holocene` repo per the Director Rule. This repo's obligation is the API contract in `holocene-view-contract.md`, captured as FR8/FR9 and as a single contract-only delegated epic. No UX-DRs exist here.

### FR Coverage Map

- FR1: Epic 1 — heartbeat publisher emits minute ticks onto Bloodbank
- FR2: Epic 1 (core dispatch: croniter eval → invoke → execute → result recorded), Epic 2 (misfire coalescing)
- FR3: Epic 1 (runtime histogram + result-labeled series + /metrics), Epic 3 (memory/CPU/GPU/token-spend families + exemplars)
- FR4: Epic 1 — registry schema, registration, transactional counters
- FR5: Epic 4 — python/node/rust SDKs on the shared process protocol
- FR6: Epic 1 — bare-executable registration with exit-code fallback
- FR7: Epic 1 — `candybar run <id>` riding the scheduled path
- FR8: Epic 5 — full CAP-8 REST + SSE contract surface
- FR9: Epic 2 (skip mechanics honored by dispatcher), Epic 5 (skip API + schedule-row reflection)
- FR10: Epic 1 — `candybar.toml` manifest consumed by CLI/API register
- FR11: Epic 1 (invoked/completed/failed), Epic 2 (skipped), Epic 5 (created/updated via definitions CRUD)

## Epic List

### Epic 1: Walking Skeleton — one Candybar end to end
A bare executable can be registered from a `candybar.toml`, fires on its cron schedule off the minute heartbeat, runs as a cgroup-scoped subprocess, lands its result and counts in the registry, emits lifecycle events, exposes basic Prometheus metrics, and can be invoked manually with `candybar run <id>` — all deployed as one container behind Traefik. No crontab anywhere.
**FRs covered:** FR1, FR2 (core), FR3 (core), FR4, FR6, FR7, FR10, FR11 (core)

### Epic 2: Schedule Control & Dispatch Correctness
The operator can trust and steer the schedule: effectively-once dispatch under redelivery, anacron-style misfire coalescing after downtime, skip-a-single-occurrence and skip-rest-of-day overrides honored by the dispatcher and reflected in computed fire times.
**FRs covered:** FR2 (misfire), FR9 (mechanics), FR11 (skipped events)

### Epic 3: Full Metering & Observability
Every invocation is fully metered per CAP-3: memory peak, runtime ms, token spend, and average CPU/GPU load queryable in Prometheus per candybar and per invocation (via exemplars), with the complete per-invoke record in the invocations table and lifecycle events.
**FRs covered:** FR3 (full)

### Epic 4: Native SDKs (Python, Node, Rust)
A Candybar can be authored natively in any of the three languages against one identical process protocol — env in, result JSON out — with self-reported token spend, never touching NATS.
**FRs covered:** FR5

### Epic 5: Schedule Read/Control API (CAP-8)
A schedule consumer can do everything `holocene-view-contract.md` requires using only this API: definitions CRUD, schedule-window rows with past/current/upcoming state and exact skip-aware fire times, next-fire poll, SSE live stream, noise collection endpoints, and skip operations.
**FRs covered:** FR8, FR9 (API surface), FR11 (created/updated)

### Epic 6: Holocene Schedule View — DELEGATED (contract-only)
The gmail-inbox-style live schedule view (Definitions + Schedule tabs, current/upcoming/past rows, countdowns, noise toggle, skip operations). **Owner: `holocene` repo.** This epic holds no implementation stories — it pins the handoff contract (`_bmad-output/specs/spec-cron-framework/holocene-view-contract.md`) that the holocene PM builds against, per the Director Rule. The only candybar-side obligation is Epic 5's API conformance to that contract.
**FRs covered:** none (contract consumer of FR8/FR9)

### Epic 7: Cron Migration onto Candybar
Added 2026-10-26 (user directive lifting the SPEC non-goal). Foundation stories 7.1-7.6 (per-candybar IANA timezone, host executor, exec/systemd runners, Hermes runner, migration ledger + rollback CLI, off-candybar watchdog), then 11 migration waves (7.7-7.17) moving all 70 `migrate`-classified jobs from `cron-inventory.md` exactly once. Full text at the end of this file.
**FRs covered:** FR2/NFR2 (extended: per-candybar timezone), FR6 (extended: runner kinds), NFR6 (extended: overlap/backlog); architecture AD-16..AD-22

---

## Epic 1: Walking Skeleton — one Candybar end to end

A bare executable can be registered from a `candybar.toml`, fires on its cron schedule off the minute heartbeat, runs as a cgroup-scoped subprocess, lands its result and counts in the registry, emits lifecycle events, exposes basic Prometheus metrics, and can be invoked manually with `candybar run <id>` — all deployed as one container behind Traefik. No crontab anywhere.

**FRs covered:** FR1, FR2 (core), FR3 (core), FR4, FR6, FR7, FR10, FR11 (core)
**NFRs binding:** NFR1, NFR3, NFR4, NFR7, NFR9, NFR10, NFR11, NFR12

### Story 1.1: Project scaffold, pinned stack, and CI

As a pipeline operator,
I want the candybar repo scaffolded with the architecture's structural seed, pinned dependency stack, and GitHub-hosted CI,
So that every later story lands on a buildable, linted, test-run codebase.

**Acceptance Criteria:**

**Given** the greenfield candybar repo
**When** the scaffold is committed
**Then** the directory layout matches the spine's Structural Seed (`src/candybar/{heartbeat,dispatcher,executor,registry,api,cli,events}`, `sdks/{python,node,rust}`, `deploy/`, `schemas/`)
**And** Python packaging pins the spine's stack versions (Python ≥3.13, fastapi 0.162.0, nats-py 2.16.0, croniter 6.2.4, psycopg 3.3.6, sqlalchemy 2.1.6 + alembic 1.20.1, prometheus-client 0.26.0, pydantic 2.14.1, typer 0.27.3, uvicorn 0.54.0)
**And** a GitHub Actions workflow on GitHub-hosted runners (public repo) runs lint + unit tests on push to main
**And** `candybar --help` runs from the installed CLI package

### Story 1.2: Bloodbank `cron` event vocabulary, envelopes, and governance PR

As a pipeline operator,
I want the cron event domain registered in Bloodbank with typed envelope builders in the daemon,
So that every event the system emits conforms to the locked naming contract before the first one is published.

**Acceptance Criteria:**

**Given** the locked Bloodbank naming contract (`bloodbank/docs/event-naming.md`)
**When** this story is complete
**Then** `src/candybar/events/` builds and parses envelopes for exactly `bloodbank.cron.tick.fired`, `bloodbank.cron.candybar.{created,updated,invoked,completed,failed,skipped}`, and command `bloodbank.cron.candybar.invoke` (subject `bloodbank.cmd.cron.candybar.invoke`), each with `kind`, `actor.type="scheduler"`, `ordering_key`, `correlationid`/`causationid`, and commands adding `command_id`, `idempotency_key`, `delivery: single_consumer`
**And** draft JSON schemas for all eight types exist under this repo's `schemas/`
**And** a PR against the `bloodbank` repo registers domain `cron`, entities `tick`+`candybar`, action `fired` in `docs/event-naming.md` and adds the schemas under `bloodbank/schemas/` (merged before any story that emits events is considered done)
**And** envelope round-trip unit tests pass (build → parse → equal)

### Story 1.3: Heartbeat publisher fires the minute tick

As a cron Subscriber author,
I want a heartbeat event published onto Bloodbank every minute,
So that scheduled work reacts to time with no crontab and no other timer in the system.

**Acceptance Criteria:**

**Given** the daemon running with the heartbeat role enabled and NATS JetStream reachable
**When** a UTC minute boundary passes
**Then** exactly one `bloodbank.cron.tick.fired` fact is published with `tick_id = tick:<unix_epoch_minute>` as both `ordering_key` and `data.tick_id`, using JetStream publish with `Nats-Msg-Id` = event id for dedupe
**And** a tick redelivered by the bus carries the same `tick_id` (deterministic, wall-clock-derived — never receipt time)
**And** no other component in the process sleeps-until-a-time to trigger work (verified by code review checklist in the story's tests: only the heartbeat module owns a clock)
**And** a test subscriber observes one tick per minute with small skew over a 5-minute window

### Story 1.4: Registry bootstrap and register a Candybar from `candybar.toml`

As a candybar author,
I want to register a scheduled unit from a `candybar.toml` manifest and see its registry row,
So that the system has a persistent inventory of name, description, tags, schedule, and entrypoint.

**Acceptance Criteria:**

**Given** the shared Postgres with a `candybar` database owned solely by the daemon
**When** alembic migrations run
**Then** tables `candybars` (UUIDv7 id, unique slug name `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`, description, tags, schedule, noise bool default false, total_invoked, total_successful), `invocations`, and `skips` exist per the spine ERD
**And** given a valid `candybar.toml` (name, description, tags, standard-cron schedule, entrypoint), `candybar register ./path` (CLI → `POST /candybars`) inserts the row and emits `bloodbank.cron.candybar.created`
**And** an invalid cron string or duplicate/invalid name is rejected with an RFC 7807 problem+json error and no row
**And** the daemon is the only writer (registry layer module is the single access class; migration grants no other role)

### Story 1.5: Dispatcher evaluates schedules off the tick

As a pipeline operator,
I want registered Candybars dispatched at each minute their cron schedule matches,
So that scheduled work fires without any crontab entry.

**Acceptance Criteria:**

**Given** registered candybars and ticks flowing from Story 1.3
**When** a tick's UTC minute matches a candybar's standard 5-field cron expression (croniter)
**Then** the dispatcher's single durable pull consumer on `bloodbank.evt.cron.tick.>` publishes command `bloodbank.cron.candybar.invoke` with `idempotency_key = candybar:<id>:tick:<tick_id>` and `trigger=scheduled`
**And** all cron evaluation is in UTC (a unit test pins a non-UTC host TZ and proves identical results)
**And** duplicate tick redelivery does not produce a second effective dispatch (dedupe on `tick_id`)
**And** non-matching minutes produce no command

### Story 1.6: Executor runs a bare executable and records the result

As a candybar author,
I want any executable to run as a cgroup-scoped subprocess with its exit status landing as its result,
So that I can schedule existing scripts and binaries without an SDK.

**Acceptance Criteria:**

**Given** an invoke command on the workqueue durable
**When** the executor consumes it
**Then** it spawns the entrypoint as a subprocess under its own cgroup v2 scope with env `CANDYBAR_INVOCATION_ID`, `CANDYBAR_ID`, `CANDYBAR_TRIGGER`, `CANDYBAR_TICK_ID`, `CANDYBAR_RESULT_FILE`
**And** it emits `bloodbank.cron.candybar.invoked` at spawn and, on exit, `completed` (exit 0 or result JSON `{status:"success"}`) or `failed` (nonzero exit / missing result file, stderr tail max 500 chars redacted in event data)
**And** the terminal `invocations` row insert and the `total_invoked`/`total_successful` counter update commit in the SAME transaction
**And** a redelivered command with an already-recorded `idempotency_key` performs no second execution
**And** unmeasured metrics record zero for bare executables

### Story 1.7: Basic Prometheus metrics and `/metrics` endpoint

As a pipeline operator,
I want invocation runtime and results queryable in Prometheus per candybar,
So that I can see what ran, how long it took, and what failed.

**Acceptance Criteria:**

**Given** the executor completing invocations
**When** Prometheus scrapes the process `/metrics`
**Then** a runtime histogram (wall ms) and an invocation counter exist on series labeled `{candybar_id, result}` only
**And** each observation carries an `invocation_id` exemplar
**And** no per-invocation label exists on any series (cardinality guard test asserts label sets)
**And** Grafana/Prometheus queries can chart success vs fail rate per candybar

### Story 1.8: Manual invoke via `candybar run <id>`

As a pipeline operator,
I want to invoke any registered Candybar immediately from the CLI,
So that I can run jobs out-of-schedule with results and metrics identical to scheduled fires.

**Acceptance Criteria:**

**Given** a registered candybar id
**When** I run `candybar run <id>`
**Then** the CLI calls `POST /candybars/{id}/invoke`, the API publishes `bloodbank.cmd.cron.candybar.invoke` with `trigger=manual` and `idempotency_key = candybar:<id>:manual:<uuid>`, and the executor runs it down the identical dispatch/execution/registry/metrics path
**And** the invocation row records `trigger=manual`
**And** the CLI exits nonzero with a problem+json body printed when the id is unknown
**And** `CANDYBAR_TRIGGER=manual` is visible to the invoked process env

### Story 1.9: Deployment envelope on the delo.sh stack

As a pipeline operator,
I want the daemon deployed as one container behind Traefik at `candybar.delo.sh` with Prometheus scraping it,
So that the skeleton runs continuously on the homelab with zero new infrastructure.

**Acceptance Criteria:**

**Given** the `deploy/` directory
**When** the container is built and deployed
**Then** one docker container runs all roles (default single-process composition) on the existing proxy network with Traefik labels for `candybar.delo.sh`
**And** Prometheus's existing scrape config picks up `/metrics`
**And** all service references are `*.delo.sh` hostnames and all secrets are env vars holding `op://DeLoSecrets/...` references (CI grep gate: no LAN IPs, no plaintext secret shapes)
**And** the container restart of the daemon causes no missed-tick duplication on rejoin (idempotent consumers reconnect)

## Epic 2: Schedule Control & Dispatch Correctness

The operator can trust and steer the schedule: effectively-once dispatch under redelivery, anacron-style misfire coalescing after downtime, skip-a-single-occurrence and skip-rest-of-day overrides honored by the dispatcher and reflected in computed fire times.

**FRs covered:** FR2 (misfire), FR9 (mechanics), FR11 (skipped events)
**NFRs binding:** NFR5, NFR6

### Story 2.1: Effectively-once dispatch proven under fault injection

As a pipeline operator,
I want dispatch to be provably effectively-once under tick redelivery, command redelivery, and dispatcher restart,
So that no job ever double-fires from bus retries.

**Acceptance Criteria:**

**Given** the walking skeleton running
**When** fault-injection tests redeliver a tick, redeliver an invoke command, and restart the dispatcher mid-minute
**Then** exactly one execution occurs per `(candybar, tick)` and per manual invoke UUID
**And** the dedupe keys are exactly `tick_id` (ticks), `candybar:<id>:tick:<tick_id>` (scheduled commands), `candybar:<id>:manual:<uuid>` (manual)
**And** only the dispatcher's single durable consumer evaluates schedules (test asserts no second consumer group can be created by the dispatcher role)

### Story 2.2: Misfire coalesce-to-latest after downtime

As a pipeline operator,
I want a daemon that was down through missed ticks to run only the latest missed fire per candybar and record the rest as skipped,
So that restarts never thundering-herd hours of backlog against shared homelab resources.

**Acceptance Criteria:**

**Given** the dispatcher was down across N missed fires of a candybar (N > 1, within 24h)
**When** the dispatcher starts
**Then** only the LATEST missed fire executes with `trigger=misfire`
**And** each earlier missed fire is recorded as `bloodbank.cron.candybar.skipped` with `data.reason=misfire_coalesced`
**And** when downtime exceeds 24h, still only the latest missed fire runs (cap test)
**And** candybars with no missed fires dispatch normally on the next tick

### Story 2.3: Skip a single occurrence

As a pipeline operator,
I want to skip one specific fire of a candybar,
So that I can suppress a known-bad or unwanted occurrence without touching the schedule.

**Acceptance Criteria:**

**Given** a registered candybar with a computed upcoming fire time
**When** a skip for `{candybar_id, fireTime}` is stored in the `skips` registry table
**Then** the dispatcher emits no invoke command for that fire and records `bloodbank.cron.candybar.skipped` with the skip reason
**And** subsequent fires of the same candybar dispatch normally
**And** upcoming-fire computation (shared croniter helper) omits the skipped fire time

### Story 2.4: Skip the rest of a day

As a pipeline operator,
I want to suppress all remaining fires of a candybar for a given UTC date,
So that I can quiet a job for today without deleting its schedule.

**Acceptance Criteria:**

**Given** a registered candybar with remaining fires today (UTC)
**When** a `skip_rest_of_day` skip for `{candybar_id, date}` is stored
**Then** no invoke commands are emitted for that candybar until the next UTC midnight
**And** each suppressed fire is recorded as `skipped` (reason=rest_of_day) at the time it would have fired
**And** fires on the following UTC day dispatch normally
**And** the skip is stored as schedule override state in the registry, not in dispatcher memory

## Epic 3: Full Metering & Observability

Every invocation is fully metered per CAP-3: memory peak, runtime ms, token spend, and average CPU/GPU load queryable in Prometheus per candybar and per invocation (via exemplars), with the complete per-invoke record in the invocations table and lifecycle events.

**FRs covered:** FR3 (full)
**NFRs binding:** NFR8, NFR11

### Story 3.1: Memory peak and average CPU load per invocation

As a pipeline operator,
I want each invocation's peak memory and average CPU load measured from its cgroup,
So that I can find memory hogs and CPU burners per candybar in Prometheus.

**Acceptance Criteria:**

**Given** the executor's cgroup v2 scope per invocation
**When** an invocation completes
**Then** a memory max gauge (from cgroup `memory.peak`) and a CPU load summary (from `cpu.stat` delta / runtime) are observed on `{candybar_id, result}` series with `invocation_id` exemplars
**And** the same values land in the `invocations` row and the `completed`/`failed` event data
**And** a canary candybar allocating a known memory size reports peak within an asserted tolerance band

### Story 3.2: Average GPU load per invocation (nvidia-smi or zero)

As a pipeline operator,
I want GPU load attributed per invocation when an NVIDIA GPU is present, and zero otherwise,
So that GPU-consuming candybars are visible without breaking GPU-less hosts.

**Acceptance Criteria:**

**Given** a host with an NVIDIA GPU
**When** an invocation runs
**Then** `nvidia-smi pmon` per-pid sampling attributes average GPU load to the invocation's process tree and observes it on the GPU load summary
**And** on a GPU-less host (or non-NVIDIA), the metric reports 0 and no error is raised
**And** sampling overhead per invocation is asserted below a stated wall-ms budget in tests

### Story 3.3: Token spend counter and full exemplar coverage

As a pipeline operator,
I want self-reported LLM token spend counted per candybar with per-invocation exemplars on every metric family,
So that token burn is queryable per candybar and traceable to a single invocation.

**Acceptance Criteria:**

**Given** an SDK-authored candybar reporting `metrics.token_spend` (integer, input+output tokens) and optional `cost_usd` in its result JSON
**When** invocations complete
**Then** a token-spend counter on `{candybar_id, result}` accumulates the reported spend (default 0 for bare executables and non-LLM jobs)
**And** every CAP-3 metric family (runtime, memory, token spend, CPU, GPU) carries the `invocation_id` exemplar on every observation
**And** the full per-invoke record (all five measures + token fields) is retrievable from the `invocations` table via the API

## Epic 4: Native SDKs (Python, Node, Rust)

A Candybar can be authored natively in any of the three languages against one identical process protocol — env in, result JSON out — with self-reported token spend, never touching NATS.

**FRs covered:** FR5
**NFRs binding:** NFR11

### Story 4.1: Python SDK (`sdks/python`)

As a python candybar author,
I want a pip-installable SDK that handles the process protocol for me,
So that I write a function and get registration, invocation context, and result/metrics reporting for free.

**Acceptance Criteria:**

**Given** `sdks/python` installed (Node floor N/A; package `candybar-sdk`)
**When** an author wraps a function with the SDK and registers it via `candybar.toml`
**Then** the SDK reads the five `CANDYBAR_*` env vars, invokes the function, and writes the AD-8 result JSON (status, message, metrics.token_spend/cost_usd) to `CANDYBAR_RESULT_FILE`
**And** an unhandled exception maps to `{status:"fail"}` with the message captured
**And** the SDK opens no NATS connection (dependency-tree test asserts no nats client import)
**And** an example python candybar fires on schedule end-to-end with metrics recorded

### Story 4.2: Node SDK (`sdks/node`)

As a node candybar author,
I want an npm-installable SDK implementing the same process protocol,
So that I can author candybars natively in JavaScript/TypeScript.

**Acceptance Criteria:**

**Given** Node ≥ 24 LTS and `sdks/node` (package `candybar-sdk`)
**When** an author wraps an async function with the SDK and registers it
**Then** the SDK reads the same five env vars, awaits the function, and writes the identical AD-8 result JSON shape
**And** a rejected promise maps to `{status:"fail"}`
**And** the SDK opens no NATS connection
**And** an example node candybar fires on schedule end-to-end with metrics recorded

### Story 4.3: Rust SDK (`sdks/rust`)

As a rust candybar author,
I want a crate implementing the same process protocol,
So that I can author candybars natively in Rust.

**Acceptance Criteria:**

**Given** Rust ≥ 1.99.1 and `sdks/rust` (crate `candybar-sdk`)
**When** an author registers a handler with the SDK and compiles a candybar binary
**Then** the SDK reads the same five env vars, runs the handler, and writes the identical AD-8 result JSON shape
**And** a panicking/`Err` handler maps to `{status:"fail"}`
**And** the crate has no NATS dependency
**And** an example rust candybar fires on schedule end-to-end with metrics recorded

## Epic 5: Schedule Read/Control API (CAP-8)

A schedule consumer can do everything `holocene-view-contract.md` requires using only this API: definitions CRUD, schedule-window rows with past/current/upcoming state and exact skip-aware fire times, next-fire poll, SSE live stream, noise collection endpoints, and skip operations.

**FRs covered:** FR8, FR9 (API surface), FR11 (created/updated)
**NFRs binding:** NFR2, NFR4
**Contract:** `_bmad-output/specs/spec-cron-framework/holocene-view-contract.md` (verbatim conformance; AD-11)

### Story 5.1: Definitions API completion

As a schedule-view consumer,
I want full definitions CRUD over REST,
So that the Definitions tab can list, create, and inspect every registered cron.

**Acceptance Criteria:**

**Given** registered candybars
**When** the consumer calls the definitions endpoints
**Then** `GET /candybars` returns `{ id, name, description, tags[], schedule, noise, createdAt }` for each; `GET /candybars/{id}` adds `{ totalInvoked, totalSuccessful }`; `POST /candybars` creates from the contract shape
**And** updates emit `bloodbank.cron.candybar.updated` in the same logical unit as the registry mutation
**And** all timestamps are ISO 8601 UTC and errors are RFC 7807 problem+json
**And** a contract test asserts response shapes field-for-field against `holocene-view-contract.md`

### Story 5.2: Schedule window endpoint with exact skip-aware fire times

As a schedule-view consumer,
I want ordered past/current/upcoming rows for any time window,
So that the Schedule tab can render its autoscrolling day view with client-side countdowns.

**Acceptance Criteria:**

**Given** candybars with schedules, invocation history, and active skips
**When** the consumer calls `GET /schedule?from=<ts>&to=<ts>`
**Then** rows are ordered by fire time, each `{ candybarId, name, schedule, fireTime, state: "past"|"current"|"upcoming", lastResult?, skipped, noise }`
**And** exactly one row is `state:"current"` (the next to fire)
**And** upcoming fire times are computed with the same croniter UTC evaluation as the dispatcher, honoring active skips (skipped fires appear with `skipped: true`, not silently dropped)
**And** past rows carry `lastResult` from the invocations table

### Story 5.3: Next-fire poll fallback

As a schedule-view consumer,
I want a cheap `GET /schedule/next` endpoint,
So that the view still works when streaming is unavailable.

**Acceptance Criteria:**

**Given** the schedule window machinery
**When** the consumer calls `GET /schedule/next`
**Then** the single current next-to-fire row (same shape as `/schedule` rows) is returned
**And** the endpoint answers from computed schedule state without a full window scan (bounded query, asserted under a stated latency budget)

### Story 5.4: Live updates over SSE

As a schedule-view consumer,
I want an SSE stream of schedule-relevant changes carrying row-shaped payloads,
So that the live view patches in place without refetching.

**Acceptance Criteria:**

**Given** the daemon's lifecycle events
**When** a consumer subscribes to `GET /schedule/stream`
**Then** an SSE event is emitted whenever an invocation starts, an invocation completes (with result), a skip is applied, a definition changes, or the current row advances
**And** each event carries enough of the `/schedule` row shape to patch in place without a refetch (contract test per contract "Live updates" section)
**And** stream reconnection with `Last-Event-ID` resumes without duplicate application (idempotent patch keys)

### Story 5.5: Noise collection endpoints

As a schedule-view consumer,
I want to add/remove crons from the noise collection individually and in batch,
So that the view's noise toggle can hide high-frequency crons.

**Acceptance Criteria:**

**Given** registered candybars
**When** the consumer calls `PUT /candybars/{id}/noise`, `DELETE /candybars/{id}/noise`, or `PUT /noise/batch` with `{ ids: [] }`
**Then** the registry `noise` flag updates and subsequent `/schedule` rows reflect `noise: bool`
**And** the API stores only the explicit collection (the `<10m` auto-classification remains the consumer's job — no auto-noise logic in candybar)
**And** batch calls are atomic per request (all ids applied or problem+json error)

### Story 5.6: Skip operations API

As a schedule-view consumer,
I want `POST /candybars/{id}/skip` and `POST /candybars/{id}/skip-rest-of-day` endpoints,
So that the view's skip controls drive the Epic 2 skip mechanics.

**Acceptance Criteria:**

**Given** Epic 2 skip mechanics and a registered candybar
**When** the consumer posts `{ fireTime }` to `/skip` or `{ date }` to `/skip-rest-of-day`
**Then** the corresponding skip rows are written and honored by the dispatcher
**And** subsequent `/schedule` responses show affected rows `skipped: true` with recomputed upcoming fire times
**And** both operations emit SSE events on `/schedule/stream`
**And** a skip for a nonexistent fireTime/id returns RFC 7807 404

## Epic 6: Holocene Schedule View — DELEGATED (contract-only)

The gmail-inbox-style live schedule view (Definitions + Schedule tabs, current/upcoming/past rows, countdowns, noise toggle, skip operations). **Owner: `holocene` repo.**

**Delegation record:**

- **Contract:** `_bmad-output/specs/spec-cron-framework/holocene-view-contract.md` — the complete and only handoff. The holocene PM builds its own BMAD stories against it.
- **Candybar-side obligation:** Epic 5 implements the contract verbatim; Story 5.1, 5.2, 5.4, 5.5, 5.6 each carry contract-conformance acceptance criteria. Any contract change requires updating both the contract file and the affected Epic 5 story ACs in the same commit.
- **Explicitly no implementation stories here** per the Director Rule: row styling, autoscroll, tab layout, toggle interaction, and grouping/filtering beyond the contract queries are all holocene repo scope.
- **Handoff trigger:** when Epic 5 reaches done, file the cross-project initiative item with the holocene PM referencing the contract and the live base URL `https://candybar.delo.sh`.

## Epic 7: Cron Migration onto Candybar

> Appended 2026-10-26 (Epic 7 addendum to the existing epics.md; the original Epics 1-6 and their coverage map above are unchanged). Sources: `_bmad-output/planning-artifacts/cron-inventory.md` (117 host jobs: 70 migrate, 47 exclude) and the architecture spine's Epic 7 addendum (AD-16..AD-22).

The scattered-cron state the braindump describes ends here: every `migrate`-classified job on big-chungus runs under Candybar, its wall-clock semantics preserved across the 2026-11-01 DST transition, its systemd units and Hermes jobs keeping their own hardening, alerting, delivery and sessions, every cutover recorded and reversible.

**Requirements covered:** FR2 + NFR2 extended by AD-16 (per-candybar IANA timezone, DST-correct); NFR7 amended by AD-17 (executor role leaves the container for a host `systemd --user` unit); FR6 extended by AD-18/AD-19 (`exec`/`systemd`/`hermes` runner kinds); NFR6 extended by AD-20 (overlap + executor-backlog coalescing); new: AD-21 (migration ledger + reversible cutover, source-of-truth retirement), AD-22 (off-candybar self-watchdog).

### Story 7.1: Per-candybar IANA timezone (AD-16)

As a pipeline operator,
I want each candybar's cron evaluated in its own IANA timezone with correct DST semantics,
So that migrating local-time systemd timers and crontab lines never shifts their wall-clock schedule across the 2026-11-01 fall-back.

**Acceptance Criteria:**

**Given** the registry from Epic 1
**When** this story lands
**Then** `candybars.timezone` (text, NOT NULL, default `UTC`) exists via alembic, validated as a zoneinfo IANA name at registration, and the definitions API shape gains `timezone` (the `holocene-view-contract.md` field addition ships in the same commit)
**And** one shared helper `fire_times(schedule, tz, start_utc, end_utc)` is the ONLY fire-time source: dispatcher match is "tick ∈ fire_times(tick, tick+1m)", and the API window/next endpoints, misfire latest-fire, and the AD-20 backlog check all call it
**And** DST behavior matches the AD-16 rule, pinned by tests: `40 1 * * *` America/New_York fires ONCE at 2026-11-01T05:40Z (not 06:40Z); `30 2 * * *` fires once at 2027-03-14T07:00Z on the gap day; `*/30 * * * *` fires at every real minute through both transitions; an hourly cron is unaffected
**And** `skip-rest-of-day`'s `date` is interpreted in the candybar's timezone (tested across both transitions)
**And** ticks, tick_id, idempotency keys, events and API timestamps remain UTC everywhere

### Story 7.2: Host executor (AD-17)

As a pipeline operator,
I want the executor role running on the host as a `systemd --user` unit while heartbeat/dispatcher/api stay in the container,
So that jobs needing the host (`systemctl --user`, `op`, docker socket, `~/`, zellij, hermes CLI) can run, with one execution environment and no routing ambiguity.

**Acceptance Criteria:**

**Given** the container deployment from Story 1.9
**When** this story lands
**Then** `deploy/host/candybar-executor.service` (tracked) runs `candybar daemon --roles executor` from the repo checkout's `.venv` as user delorenj with `Delegate=yes`, connects to NATS/Postgres on host loopback, and attaches the existing `candybar-executor-invoke` durable (the WorkQueue stream is unchanged)
**And** the container runs `--roles heartbeat,dispatcher,api` (compose/entrypoint updated); no container executor remains and no host/container routing field exists
**And** the executor serves its own `/metrics` on port 8701 (scraped via `host.docker.internal` by the existing Prometheus), closing the "executor without api" gap
**And** the executor refuses to consume while the registry's alembic head differs from its own (guarded by the deploy procedure, not a DB lock)
**And** deploy/README documents the pair-deploy (container + `systemctl --user restart candybar-executor`), and a redeploy proves the smoke candybar still fires each minute

### Story 7.3: Runner kinds — exec and systemd (AD-18)

As a pipeline operator,
I want the executor to start either an argv (exec runner) or an existing systemd user service (systemd runner), both fully metered,
So that a migrated job keeps its unit's EnvironmentFile, hardening, TimeoutStartSec and OnFailure alerting while only its timer retires.

**Acceptance Criteria:**

**Given** the host executor from 7.2
**When** this story lands
**Then** `candybars.runner` (jsonb) exists with `kind: exec | systemd` (`entrypoint` remains the exec default for existing rows); the exec runner supports `{argv[], workdir?, env{}?, timeout_s?, on_failure?}` with `env` holding non-secret literals
**And** the systemd runner runs `systemctl --user start --wait <unit>` and meters from the unit after exit: `MemoryPeak` → memory gauge, `CPUUsageNSec` / monotonic runtime → CPU load, `ExecMainStatus`/`Result` → success/fail, the invocation's stderr tail from the journal for that `InvocationID`, GPU from pmon pids in the unit's cgroup
**And** the AD-8 protocol env reaches systemd jobs through a once-installed drop-in `50-candybar.conf` (`EnvironmentFile=-%t/candybar/inv/%n.env`) the runner rewrites before each start — no daemon-reload per run; jobs that don't read the env are unaffected
**And** `on_failure` is an argv run once, best-effort and unmetered, after a terminal `failed` with `CANDYBAR_FAILURE_MESSAGE` in env — never set on a systemd runner whose unit already has `OnFailure=`
**And** tests pin: systemd unit properties actually persist for an exited oneshot (as reality-checked on systemd 257), the drop-in round trip, and a unit whose start fails maps to `failed`

### Story 7.4: Hermes runner (AD-19)

As a pipeline operator,
I want Hermes agent-mode jobs triggered by candybar while Hermes keeps delivery, sessions, context_from and monitor gates,
So that agent jobs migrate without candybar re-implementing Hermes and without the job firing on two schedulers.

**Acceptance Criteria:**

**Given** delorenj/hermes-agent can be extended
**When** this story lands
**Then** hermes-agent supports `scheduler: external` (the ticker never fires the job on its own; a `cron run` trigger does NOT re-enable the natural schedule) and `hermes -p <profile> cron run <job_id> --wait` blocks until the durable attempt is terminal, exits 0/1 and prints its token usage — committed and pushed to delorenj/hermes-agent (deploy via one batched `hermes-gateway-restart --reason` when the fleet story 7.17 lands)
**And** the candybar executor's `hermes` runner (`{profile, job_id}`) invokes exactly that command on the host executor and writes the attempt's input+output tokens into the AD-8 result file (0 when unavailable)
**And** a test with a stub `hermes` binary pins exit-code mapping, timeout, and the usage parse; a double-trigger of the same job is rejected or joined (never two concurrent attempts)
**And** the no-agent path is documented: Hermes no-agent jobs migrate as exec-runner candybars with an `on_failure` hook reproducing their `failure_deliver`

### Story 7.5: Migration ledger, rollback and the migrate CLI (AD-21)

As a pipeline operator,
I want a committed ledger recording every migrated job with its exact disable and rollback commands, driven by `candybar migrate`,
So that every cutover is reversible — even with the daemon down — and no installer re-run can resurrect an old scheduler.

**Acceptance Criteria:**

**Given** the inventory and runners from 7.1-7.4
**When** this story lands
**Then** `deploy/migration/ledger.yaml` (committed) holds one entry per inventory id: candybar name, runner, native schedule, cron + tz, accepted deviations, source-of-truth repo + commit, exact disable and rollback commands, verification invocation id, status `planned|cutover|verified|rolled-back`
**And** `candybars.enabled` (bool, default true) exists: disabled candybars are never dispatched and produce no upcoming rows in `/schedule` (definitions still list them with `enabled`); `POST /candybars/{id}/enabled` toggles it
**And** `candybar migrate {plan,cutover,verify,rollback,status}` drives the ledger as a REST client of the api role (never a direct registry writer); cutover order is register disabled → retire native scheduler → enable candybar, and cutover is refused within 2 minutes of the job's next native fire
**And** rollback restores the native scheduler first and disables the candybar best-effort, so it works while the daemon is down (documented and tested against a stub ledger entry)
**And** the wave stories consume this CLI; no wave story hand-rolls its own registration SQL

### Story 7.6: Off-candybar self-watchdog (AD-22)

As a pipeline operator,
I want a systemd timer that is NOT a candybar paging me when candybar's heartbeat, dispatcher or host executor goes stale,
So that the scheduler is never its own watchdog.

**Acceptance Criteria:**

**Given** the live deployment
**When** this story lands
**Then** `deploy/watchdog/candybar-watchdog.{timer,service}` (systemd --user, every 2 minutes) checks `https://candybar.delo.sh/healthz`, that the every-minute canary candybar finished within 3 minutes, and that `candybar-executor.service` is active
**And** it pages ntfy once per outage and again on recovery, and touches an ok-stamp that a new infra-doctor `file_age` probe watches (probe committed to delorenj/infra, deployed per its sync.sh)
**And** the canary candybar itself (every-minute, exec runner, no side effects) is registered and enabled as part of this story
**And** a simulated outage test (dispatcher paused / executor unit stopped) produces exactly one page; `candybar-watchdog` is never registered as a candybar (CI grep gate)

### Story 7.7: Wave 1 — infra housekeeping (10 jobs)

As a pipeline operator,
I want the delorenj/infra host-maintenance timers running under candybar,
So that the lowest-risk, internal-only systemd jobs prove the systemd runner in production.

**Jobs:** `systemd:cache-prune-codex-tmp.timer`, `systemd:cache-prune-daily.timer`, `systemd:cache-prune.timer`, `systemd:docker-prune.timer`, `systemd:state-maintain`, `systemd:user-logrotate`, `systemd:zellij-ark`, `systemd:gnome-shell-restart.timer`, `systemd:srvls-metrics`, `systemd:srvls-snapshot`

**Acceptance Criteria:**

**Given** Stories 7.1-7.6 done
**When** this wave lands
**Then** every listed job has a ledger entry and runs under candybar with the cron + tz from the inventory (jitter dropped, recorded as accepted deviation), its unit file unchanged apart from the runner drop-in, its timer disabled and removed from `timers.target.wants`
**And** untracked units (`gnome-shell-restart`, `srvls-metrics`, `srvls-snapshot`) get a tracked home in delorenj/infra `systemd/user/` first; all owning-repo changes commit and push (delorenj/infra, delorenj/srvls READMEs)
**And** `srvls` learns to read the candybar registry (API) so its inventory does not under-report — committed to delorenj/srvls
**And** each job verifies by its first scheduled fire with a matching invocation row and full metering; rollback is demonstrated on ONE job (restore timer via ledger, disable candybar, confirm one native fire, then re-cutover)

### Story 7.8: Wave 2 — 33GOD platform sweeps (7 jobs)

As a pipeline operator,
I want the 33GOD/bloodbank/candystore/holocene/skillex platform sweeps under candybar,
So that the platform's own janitorial jobs dogfood the scheduler.

**Jobs:** `systemd:bloodbank-agent-hooks-health.timer`, `systemd:candystore-project-sync.timer`, `systemd:holocene-pad-roster.timer`, `systemd:hermes-single-install-guard.timer`, `systemd:skillex-hermes-resync`, `systemd:naipkins-registry-snapshot`, `systemd:delonet-n8n-workflows-sync.timer`

**Acceptance Criteria:**

**Given** Wave 1 done
**When** this wave lands
**Then** every listed job migrates per ledger with its recorded cron + tz and interval timers keep their interval (phase not preserved, recorded deviation)
**And** `skillex-hermes-resync`'s companion `.path` trigger stays on systemd (event trigger, out of scope) while only the timer retires; `scripts/install-hermes-resync.sh` stops enabling the timer
**And** installer changes land in delorenj/bloodbank, delorenj/candystore, delorenj/holocene, delorenj/hermes-agent-template, delorenj/skillex, delorenj/naipkins and delorenj/delonet-n8n-workflows — each committed and pushed so a re-install does not resurrect the timer
**And** `skillex-hermes-resync` (exits 1 by design today) verifies on result parity, not success
**And** every job verifies by first scheduled fire; each failure keeps its existing ntfy alerting

### Story 7.9: Wave 3 — backups and vault (8 jobs)

As a pipeline operator,
I want the backup and vault-maintenance chain under candybar with its ordering preserved,
So that offsite snapshots, restore drills and the vault sync keep their exact cadence through the DST transition.

**Jobs:** `systemd:forever-ago-vault.timer`, `systemd:restic-vault`, `systemd:backup-drill.timer`, `systemd:restic-dbdumps`, `systemd:milkshake-ledger-backup.timer`, `systemd:ifvault-nightly.timer`, `systemd:delodocs-inbox-cleanup.timer`, `crontab:22`

**Acceptance Criteria:**

**Given** Waves 1-2 done
**When** this wave lands
**Then** the ordering chain holds via cron minutes: `forever-ago-vault` 03:00 → `restic-vault` 04:30 → `backup-drill` (monthly); the excluded anacron dump ~07:35 → `restic-dbdumps` 09:15
**And** `ifvault-nightly` (01:40, in the 2026-11-01 repeated hour) fires exactly once on the transition night per AD-16, and `milkshake-ledger-backup` (02:30, in the 2027-03-14 gap) fires once at 03:00 EDT — both covered by a simulated-clock executor test plus the live transition observation
**And** untracked units (`forever-ago-vault`, `restic-vault`, `backup-drill`) get a tracked home (delorenj/DeLoContainers `systemd/user/` or the owning repo) before cutover; intelliforia and DeLoDocs vault unit updates committed (DeLoDocs: vault file, not git — record the file diff in the ledger)
**And** `crontab:22` migrates as an exec-runner candybar (`timeout_s` ≥ 101m, overlap=skip); its crontab line is removed, the stale disabled `sync-vault.timer` is deleted or neutralized, and skillex's stale `folder-curator/references/drive-sync.md` is fixed — zshyzsh + skillex committed and pushed
**And** long-runner jobs prove the executor's `in_progress` heartbeats survive past the 1h ack_wait; `restic-dbdumps` and `ifvault-nightly` (pre-existing failures) verify on parity

### Story 7.10: Wave 4 — Hindsight pipeline (4 jobs)

As a pipeline operator,
I want the Hindsight sweep/probe/analysis timers under candybar,
So that memory-pipeline jobs are inventoried and metered like everything else.

**Jobs:** `systemd:hindsight-capture-sweep.timer`, `systemd:hindsight-write-probe.timer`, `systemd:hindsight-memory-analysis.timer`, `systemd:hindsight-template-sweep.timer`

**Acceptance Criteria:**

**Given** Wave 1 done (LLM-adjacent but internal-only, so scheduled after one safe wave)
**When** this wave lands
**Then** all four jobs migrate per ledger; unit files in delorenj/bloodbank (hook-hub), delorenj/agents (`~/.agents` hooks/hindsight — record the live-file diff in the ledger since the live dir is not the repo) and delorenj/DeLoContainers are committed and pushed
**And** the `op run --env-file` secrets flow of the two analysis jobs is preserved via the unit's own Environment directives (verified: first fire resolves `HINDSIGHT_DB_PASSWORD`)
**And** `hindsight-write-probe`'s infra-doctor 2h deadman keeps passing (stamp written by the same script under candybar)
**And** `hindsight-template-sweep` (pre-existing op/psql failure) verifies on parity; every job's failure keeps its ntfy alerting

### Story 7.11: Wave 5 — product pollers (4 jobs)

As a pipeline operator,
I want the HeyMa/gruvato/mobile-deploy-hub pollers under candybar with single-instance semantics,
So that phone-facing and quota-facing jobs keep their no-overlap guarantees.

**Jobs:** `systemd:telnyx-s3-intake`, `systemd:gruvato-acoustid-check.timer`, `systemd:mobile-deploy-hub-catchup.timer`, `systemd:mobile-deploy-hub-macwatch.timer`

**Acceptance Criteria:**

**Given** Waves 1-2 done (first wave with external side effects)
**When** this wave lands
**Then** `telnyx-s3-intake` (every minute, `* * * * *`) and both mobile-deploy-hub jobs run with overlap=skip proven by a forced-long-run test (AD-20 skip recorded, no concurrent units)
**And** mobile-deploy-hub's `tools/catchup/units/install.sh` (re-run by `tools/hosts.mjs` on EVERY hub release) stops enabling the timers — committed to delorenj/mobile-deploy-hub — or the next release resurrects them; rollback restores `enable --now`
**And** `gruvato-acoustid-check` keeps its exit-2 → `OnFailure` ntfy path (the alert service still fires via the unit, not the executor)
**And** external-side-effect jobs are never `candybar run` to test: each verifies by its first scheduled fire
**And** delorenj/HeyMa and delorenj/gruvato unit/installer changes committed and pushed

### Story 7.12: Wave 6 — IntelliForia operations (4 jobs)

As a pipeline operator,
I want the IntelliForia pull-prod/client-health/staging chain under candybar,
So that the client product's nightly data refresh and staging deploys are inventoried, metered and ordered.

**Jobs:** `systemd:intelliforia-pull-prod.timer`, `systemd:intelliforia-client-health.timer`, `systemd:intelliforia-client-health-monday.timer`, `systemd:intelliforia-staging-track-main.timer`

**Acceptance Criteria:**

**Given** Wave 5 done
**When** this wave lands
**Then** ordering holds: `pull-prod` 04:15 → `client-health` 05:30 (its ExecStartPre wait on pull-prod still functions, because the unit is unchanged) and `staging-track-main` (weekday 07:05) still stands down while pull-prod is active
**And** the hand-maintained `intelliforia-pull-prod.{timer,service}` units get a tracked home in IntelliForia/intelliforia `infra/` before cutover; all IntelliForia/intelliforia changes (incl. `infra/client-health/install.sh` and the staging systemd copies) committed and pushed
**And** `SuccessExitStatus=75` (pull-prod) and the ExecStartPre/ExecStartPost chain still map to candybar success/fail correctly (75 counts as success; a non-listed exit counts as failed)
**And** `CLIENT_HEALTH_NOTIFY_CAIT=0` and the ntfy-token `op://` env are preserved verbatim through the systemd runner
**And** staging-touching jobs verify by first scheduled fire only

### Story 7.13: Wave 7 — AutomaticAI gateway operations (9 jobs)

As a pipeline operator,
I want the client-portal gateway ops timers under candybar,
So that the revenue-facing gateway's token renewals, catalog sync and health checks are inventoried and alert through their existing paths.

**Jobs:** `systemd:newapi-allowances`, `systemd:newapi-vm-health`, `systemd:newapi-db-pull`, `systemd:newapi-claude-token`, `systemd:newapi-oauth-connections`, `systemd:newapi-openrouter-sync`, `systemd:newapi-staff-sso`, `systemd:newapi-token-watch`, `systemd:aai-free-scout.timer`

**Acceptance Criteria:**

**Given** Waves 5-6 done
**When** this wave lands
**Then** all nine jobs migrate per ledger; `gateway/ops/install-systemd.sh` and the enable steps in `gateway/ops/README.md` (lines 356, 374) and `gateway/ops/OPENROUTER-PASSTHROUGH.md` (171-174) stop enabling the retired timers — committed and pushed to AutomaticAI-io/client-portal
**And** relative-phase jobs (`newapi-staff-sso` hourly at :07 etc.) keep their minute with the recorded cron; `newapi-db-pull` keeps its position after the ~07:35 excluded anacron dump
**And** `SuccessExitStatus=1` on `newapi-vm-health` maps to candybar success (problems already pushed to ntfy), pinned by a unit test on the exit-code mapping
**And** pre-existing failures (`newapi-claude-token` 219/219 exit 1) verify on parity; `aai-free-scout` (kapture browser + deploy) verifies only by its first scheduled fire
**And** the user-manager `OP_SERVICE_ACCOUNT_TOKEN` env reaches every gateway job through the systemd runner (first-fire verification: the job's op calls succeed)

### Story 7.14: Wave 8 — James Brennan / IPM (8 jobs)

As a pipeline operator,
I want the James Brennan and IPM timers under candybar with their inter-job ordering preserved,
So that the client's nightly updates, dashboards and billing ledger stay on schedule.

**Jobs:** `systemd:ipm-metrics.timer`, `systemd:ipm-internal-dashboard.timer`, `systemd:james-brennan-billing-ledger.timer`, `systemd:sync-jim-dropbox`, `systemd:activity-report@james-brennan.timer`, `systemd:james-brennan-nightly-suite.timer`, `systemd:james-brennan-daily-update.timer`, `systemd:james-brennan-nightly-fix.timer`

**Acceptance Criteria:**

**Given** Waves 5-6 done
**When** this wave lands
**Then** ordering holds: `ipm-metrics` → `ipm-internal-dashboard` (+1 minute per the inventory's recorded translation) and `james-brennan-nightly-suite` 03:11 → `james-brennan-nightly-fix` 04:23
**And** the uncommitted `ipm-observability` work (`~/docker/.worktrees/feat-ipm-call-observability`) is resolved first: committed, pushed and merged to its home, with the units given a tracked home — recorded in the ledger; `sync-jim-dropbox`'s untracked unit likewise (delorenj/infra or zshyzsh)
**And** `activity-report@james-brennan` keeps its `schedule.conf`/`dry.conf` drop-ins (dry parallel night, exit-2 parity); the skillex `activity-report install-timer` path stops enabling the template timer for migrated projects — committed to delorenj/skillex
**And** james-brennan installer scripts (`install-billing-ledger-timer.sh`, `install-daily-update-timer.sh`, `install-nightly-fix-timer.sh`) retire the timers — committed and pushed to AutomaticAI-io/james-brennan
**And** `sync-jim-dropbox` (exit 2 CLI drift) and `activity-report` (exit 2) verify on parity; LLM jobs keep their ntfy/portal behaviors via unchanged units

### Story 7.15: Wave 9 — Dev Journal and reporting (5 jobs)

As a pipeline operator,
I want the Dev Journal nightly chain and the jev-rollup sidecar under candybar,
So that the daily report pipeline's 06:00 → 06:15 → 06:30 chain is one inventoried, ordered flow.

**Jobs:** `hermes:33god-pm:8da3f24263f9`, `systemd:dev-journal-ensure-nightly.timer`, `systemd:dev-journal-reconcile.timer`, `systemd:sync-daily-reports-to-s3`, `hermes:33god-pm:9b5dadbd1e4d`

**Acceptance Criteria:**

**Given** Waves 1-4 done (reporting chain is internal-heavy but touches S3/Slack)
**When** this wave lands
**Then** the chain holds: Dev Journal nightly 06:00 (exec runner on `dev-journal-hermes-nightly.sh`; its Hermes job is paused via `hermes cron pause 8da3f24263f9` with rollback = `hermes cron resume`) → `dev-journal-ensure-nightly` 06:15 → `sync-daily-reports-to-s3` 06:30
**And** `hermes:33god-pm:9b5dadbd1e4d` migrates as an exec-runner candybar with `TZ=America/New_York` in its env map (the script branches on local hour) and an `on_failure` hook reproducing its `failure_deliver` telegram; its Hermes job paused with the same rollback pattern
**And** the Dev Journal `fleet_health` collector learns to read the candybar registry (API) instead of `systemctl --user list-timers` / Hermes jobs.json for migrated jobs — committed to delorenj/infra + delorenj/skillex
**And** `dev-journal-reconcile` (exit 3 parity) and the unversioned Hermes wrapper (`~/.hermes/profiles/33god-pm/scripts/`) get tracked homes recorded in the ledger
**And** infra installer changes (dev-journal-ensure-nightly, dev-journal-reconcile, sync-daily-reports-to-s3) committed and pushed to delorenj/infra

### Story 7.16: Wave 10 — personal LLM jobs (5 jobs)

As a pipeline operator,
I want the ToLT Foundry, pr-crusher and DeLoDocs LLM jobs under candybar,
So that the personal LLM workloads are metered for token spend and land under one control surface.

**Jobs:** `systemd:tolt-forge-daily`, `systemd:tolt-items-daily`, `systemd:pr-crusher`, `systemd:honda-hunt-daily-check.timer`, `systemd:delodocs-wiki-self-tuning-weekly.timer`

**Acceptance Criteria:**

**Given** Wave 8 done (LLM and spend-bearing jobs go late)
**When** this wave lands
**Then** tower-of-lost-things' `ops/README.md` link/enable instructions retire the timers (the `.path` trigger on `tolt-items-daily` stays) — committed and pushed to delorenj/tower-of-lost-things; pr-crusher's `scripts/install.sh` line-89 `enable --now` retires — committed to delorenj/pr-crusher
**And** DeLoDocs vault units (`honda-hunt-daily-check`, `delodocs-wiki-self-tuning-weekly`) are recorded in the ledger with their file diffs (vault is not git)
**And** `tolt-forge-daily` (9h TimeoutStartSec) and `pr-crusher` (2h, bwrap sandbox, systemd hardening) prove long systemd-runner invocations: metering reads the unit's MemoryPeak/CPUUsageNSec and the OnFailure/alert services still fire
**And** pre-existing failures (tolt-forge-daily weekly limit, tolt-items-daily 2/3, pr-crusher 0/5, honda-hunt codex token, wiki-self-tuning hermes timeout) verify on parity — migration never silently changes a failing job's semantics
**And** LLM jobs report token spend where the unit's own tooling exposes it (else 0); external-side-effect jobs verify by first scheduled fire only

### Story 7.17: Wave 11 — Hermes agent jobs (6 jobs)

As a pipeline operator,
I want the six live Hermes agent-mode jobs triggered by candybar's Hermes runner,
So that agent jobs keep Hermes delivery/sessions while their schedule lives in the candybar registry.

**Jobs:** `hermes:33god-pm:777c511a009c`, `hermes:33god-pm:f3db72089593`, `hermes:delodocs-pm:fc39bd8b33c8`, `hermes:dumply:8aecd027dbd0`, `hermes:peter-griffin:b240575fae0c`, `hermes:peter-griffin:3684c5aa8eac`

**Acceptance Criteria:**

**Given** Story 7.4 (Hermes `scheduler=external` + `--wait`) and Waves 1-10 done
**When** this wave lands
**Then** each job's Hermes record is set `scheduler: external` (the ticker never fires it on its own) and its candybar fires `hermes -p <profile> cron run <id> --wait` on the recorded cron + tz; rollback = restore the natural schedule with `hermes cron resume` (recorded per job in the ledger)
**And** the crank beat (`777c511a009c`) migrates with its deadman intact: the excluded crank-watchdog (`061c4dbedc1c`) keeps reading the Hermes output dir for `777c511a009c`, which the Hermes runner still writes — verified after cutover
**And** the peter-griffin pair keeps its ordering: `3684c5aa8eac` (06:23) still gates on `b240575fae0c`'s (05:53) manifest < 2h old — verified on first scheduled fires
**And** token spend lands in candybar metrics for every Hermes-runner invocation (attempt usage written into the AD-8 result file), and a Telegram-delivered job (`dumply` daily-story) still delivers after cutover
**And** the hermes-agent change from 7.4 is deployed with one batched `hermes-gateway-restart --reason` AFTER every migrated job is re-saved external (never during); DeLoDocs/dumply/peter-griffin profile job records are unversioned — diffs recorded in the ledger
