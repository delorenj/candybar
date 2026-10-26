# Epic 1 Context: Walking Skeleton — one Candybar end to end

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Prove the whole Candybar paradigm on one executable: register a bare executable from a `candybar.toml`, have it fire on its cron schedule off the minute heartbeat, run it as a cgroup-scoped subprocess, land its result and counters in the Postgres registry, emit lifecycle events onto Bloodbank, expose basic Prometheus metrics, and support manual invocation via `candybar run <id>` — all shipped as one container behind Traefik. This epic exists to kill crontab: no crontab, no private timers, no scheduler libraries that own clocks anywhere in the system.

## Stories

- Story 1.1: Project scaffold, pinned stack, and CI
- Story 1.2: Bloodbank `cron` event vocabulary, envelopes, and governance PR
- Story 1.3: Heartbeat publisher fires the minute tick
- Story 1.4: Registry bootstrap and register a Candybar from `candybar.toml`
- Story 1.5: Dispatcher evaluates schedules off the tick
- Story 1.6: Executor runs a bare executable and records the result
- Story 1.7: Basic Prometheus metrics and `/metrics` endpoint
- Story 1.8: Manual invoke via `candybar run <id>`
- Story 1.9: Deployment envelope on the delo.sh stack

## Requirements & Constraints

- One heartbeat tick per minute published as a Bloodbank fact with deterministic identity `tick:<unix_epoch_minute>`; it is the only clock in the system.
- Registered candybars fire at each UTC minute their standard 5-field cron schedule matches (croniter evaluation in the dispatcher); invocation success/fail is recorded.
- Basic metering in this epic: runtime wall-ms histogram and invocation result counter on Prometheus series labeled only `{candybar_id, result}`, with per-invocation `invocation_id` exemplars; full memory/CPU/GPU/token metering is Epic 3.
- Postgres registry holds the inventory (name, description, tags, schedule, noise flag) plus `total_invoked`/`total_successful` counters updated in the SAME transaction as the terminal invocation row.
- Any executable registers without an SDK: exit code 0 = success, nonzero = fail, unmeasured metrics report zero. `candybar.toml` manifest carries registration metadata.
- `candybar run <id>` triggers a manual invocation that rides the identical dispatcher/executor/registry/metrics path as a scheduled fire (`trigger=manual`).
- Every lifecycle state change emits its `bloodbank.cron.*` fact (created/invoked/completed/failed in this epic) as one logical unit with the registry mutation.
- Error mapping: nonzero exit or missing result file → `failed` with stderr tail (max 500 chars, redacted) in event data.
- Success looks like: the skeleton runs continuously on the homelab with zero new infrastructure.

## Technical Decisions

- **One deployable, role-composed** (AD-2): heartbeat, dispatcher, executor, and api are roles of one Python asyncio process, selected by CLI role flag (default: all roles). One container, one systemd unit, single node, no HA.
- **Bloodbank is the only scheduling transport** (AD-1): all time-driven behavior keys off `bloodbank.evt.cron.tick.fired` consumed from the bus. Nothing besides the heartbeat publisher may sleep-until-a-time.
- **Event vocabulary** (AD-3): types are exactly `bloodbank.cron.tick.fired` and `bloodbank.cron.candybar.{created,updated,invoked,completed,failed,skipped}`; command `bloodbank.cron.candybar.invoke` goes on subject `bloodbank.cmd.cron.candybar.invoke`. Envelopes carry `kind`, `actor.type="scheduler"`, `ordering_key` (`tick:<tick_id>` or `candybar:<id>`), `correlationid`/`causationid`; commands add `command_id`, `idempotency_key`, `delivery: single_consumer`. The `cron` domain MUST be registered PR-first in the bloodbank repo (`docs/event-naming.md`, `schemas/`) before first emission — Story 1.2 owns that; draft schemas live in this repo's `schemas/`.
- **Deterministic tick identity** (AD-4): `tick_id = tick:<unix_epoch_minute>` from wall-clock minute boundary; consumers key on it, never receipt time.
- **Effectively-once dispatch** (AD-5): only the dispatcher's single durable pull consumer evaluates schedules; invoke commands carry `idempotency_key = candybar:<id>:tick:<tick_id>` (scheduled) or `candybar:<id>:manual:<uuid>` (manual); JetStream publish uses `Nats-Msg-Id` = event id for dedupe.
- **Executor-owns-execution** (AD-7): every invocation runs as a subprocess under its own cgroup v2 scope, spawned by the executor. No per-candybar bus subscribers.
- **SDK process protocol** (AD-8): env in (`CANDYBAR_INVOCATION_ID`, `CANDYBAR_ID`, `CANDYBAR_TRIGGER` ∈ `scheduled|manual|misfire`, `CANDYBAR_TICK_ID`, `CANDYBAR_RESULT_FILE`); result JSON out at `CANDYBAR_RESULT_FILE` (`{status, message?, metrics:{token_spend?, cost_usd?}}`). SDKs never open NATS connections.
- **Registry** (AD-10): database `candybar` on the shared homelab Postgres, daemon is sole writer, alembic migrations; ERD = `candybars` / `invocations` / `skips`.
- **Raw NATS JetStream via nats-py** (AD-14): no Dapr sidecar in the daemon; durable pull consumer for ticks, workqueue durable for invoke commands.
- **Deployment envelope** (AD-15): one docker container behind Traefik at `candybar.delo.sh`, Prometheus scrapes `/metrics`, secrets only as env vars holding `op://DeLoSecrets/...` references, services referenced by `*.delo.sh` hostnames (never LAN IPs), CI on GitHub-hosted runners (public repo).
- **UTC everywhere** (AD-13): all cron evaluation and fire-time computation is UTC; timestamps ISO 8601/RFC 3339 UTC; API errors RFC 7807 problem+json; candybar names unique slugs `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`; registry ids UUIDv7.
- **Pinned stack**: Python ≥ 3.13, fastapi 0.162.0, uvicorn 0.54.0, nats-py 2.16.0, croniter 6.2.4, psycopg 3.3.6, sqlalchemy 2.1.6 + alembic 1.20.1, prometheus-client 0.26.0, pydantic 2.14.1, typer 0.27.3.

## Cross-Story Dependencies

- Story 1.1 (scaffold) blocks everything: it creates the layout, packaging, and CI all other stories land on.
- Story 1.2 (event vocabulary + bloodbank governance PR) blocks any story that emits events (1.3 onward): the `cron` domain registration must merge before first emission counts as done.
- Stories 1.3 (heartbeat) and 1.4 (registry) are the two independent substrate stories; 1.5 (dispatcher) depends on both, 1.6 (executor) depends on 1.4/1.5, and 1.7/1.8 depend on 1.6.
- Story 1.9 (deployment) depends on the full skeleton but its CI grep gate (no LAN IPs, no plaintext secrets) is a convention every earlier story must already honor.
- Epic 5's API builds on the registry (1.4) and dispatcher croniter evaluation (1.5); Epic 2's misfire/skip mechanics extend 1.5's dispatcher.
