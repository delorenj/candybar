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
