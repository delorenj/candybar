# Epic 2 Context: Schedule Control & Dispatch Correctness

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Make the schedule trustworthy and steerable for the operator. The walking skeleton (Epic 1) fires candybars off the minute heartbeat; this epic proves dispatch is effectively-once under bus redelivery and restarts, coalesces missed fires after downtime instead of replaying a backlog, and lets the operator suppress a single occurrence or the rest of a UTC day through registry-stored schedule overrides that the dispatcher honors before emitting invoke commands and that upcoming-fire computation reflects.

## Stories

- Story 2.1: Effectively-once dispatch proven under fault injection
- Story 2.2: Misfire coalesce-to-latest after downtime
- Story 2.3: Skip a single occurrence
- Story 2.4: Skip the rest of a day

## Requirements & Constraints

- Ticks are at-least-once; dispatch must be effectively-once. Exactly one execution per `(candybar, tick)` and per manual invoke UUID, under tick redelivery, invoke-command redelivery, and dispatcher restart.
- Dedupe keys are fixed: ticks dedupe on `tick_id` (`tick:<unix_epoch_minute>`); scheduled invoke commands carry `idempotency_key = candybar:<id>:tick:<tick_id>`; manual ones `candybar:<id>:manual:<uuid>`.
- Only the dispatcher's single durable consumer evaluates schedules; any other raw-tick subscriber must dedupe on `tick_id`.
- Misfire: on dispatcher startup, per candybar, only the LATEST missed fire since the last-seen tick executes (`trigger=misfire`); earlier ones are recorded as `bloodbank.cron.candybar.skipped` with `data.reason=misfire_coalesced`. Catch-up window capped at 24h; beyond it still only the latest runs. Candybars with no missed fires dispatch normally.
- Skips are schedule-override state in the registry `skips` table (never dispatcher memory), applied before emitting invoke commands; each suppressed fire emits `skipped` with its reason (`rest_of_day` for day skips). Rest-of-day skips cover the remainder of a UTC date; the next UTC day dispatches normally.
- Upcoming-fire computation (shared croniter helper, also used by the later API) must omit skipped fire times.

## Technical Decisions

- One clock: only the heartbeat publisher may sleep-until-a-time; all time-driven behavior keys off consumed ticks. No scheduler libraries owning clocks.
- Schedule evaluation is croniter matching standard 5-field cron against the UTC tick minute; all fire-time math is UTC.
- Transport is raw nats-py JetStream: publish with `Nats-Msg-Id` = event id for broker dedupe, durable pull consumer for ticks, workqueue durable for invoke commands. No Dapr in the daemon.
- Envelopes: `kind`, `actor.type: "scheduler"`, `ordering_key` (`tick:<tick_id>` or `candybar:<id>`), `correlationid`/`causationid`; commands add `command_id`, `idempotency_key`, `delivery: single_consumer`.
- Registry is one Postgres database owned solely by the daemon, alembic migrations; ERD `candybars` / `invocations` / `skips` (skips carry a `kind`). Counters update in the same transaction as the terminal invocation row.
- Every lifecycle state change emits its `bloodbank.cron.*` fact as one logical unit with the registry mutation.
- Single node, no HA; one container running all roles.

## Cross-Story Dependencies

- 2.1 hardens the Epic 1 dispatcher/executor path (it was deferred from the dispatcher story's fault-injection gap) and is the base the other stories extend.
- 2.2 needs a durable "last-seen tick" notion for the dispatcher; 2.3/2.4 need the `skips` table and a shared skip-aware croniter helper.
- Epic 5 builds the skip API and schedule-window rows on top of 2.3/2.4's registry state and helper.
