---
id: SPEC-cron-framework
companions:
  - holocene-view-contract.md
  - architecture-diagrams.md
sources:
  - ../../../BRAINDUMP.md
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# Candybar — Bloodbank-driven cron framework

## Why

A pain to solve. Scheduled work in the 33GOD pipeline is scattered: Hermes runs its own cron framework, other jobs live in user `crontab` entries, and nothing shares an inventory, metrics, or a control surface. Candybar replaces all of it with a single Bloodbank-driven cron system purpose-built for the pipeline: one heartbeat on the event bus, business logic in subscribers, every scheduled unit registered, metered, and manually invocable.

## Capabilities

- **CAP-1**
  - **intent:** The system fires a heartbeat event onto Bloodbank every minute so cron Subscribers can react to time without any crontab.
  - **success:** A subscriber receives one heartbeat event per minute (60s tick, small skew tolerated) and no scheduled work requires a crontab entry.
- **CAP-2**
  - **intent:** Every scheduled unit is a `Candybar` implementing `invoke`, `schedule` (standard cron format), and `result` (success/fail).
  - **success:** A registered Candybar fires at each time its cron schedule dictates and its success/fail result is recorded.
- **CAP-3**
  - **intent:** Every Candybar invocation is metered to Prometheus: total memory, runtime in ms, token spend, and average CPU/GPU load.
  - **success:** All four metric families are queryable in Prometheus, labeled per Candybar and per invocation.
- **CAP-4**
  - **intent:** A Postgres registry holds the full Candybar inventory — name, description, tags — plus invocation metadata (total times invoked, total successful runs).
  - **success:** A registry query for any Candybar returns its name/description/tags and counts that match the recorded invocation history.
- **CAP-5**
  - **intent:** SDKs exist for Node, Python, and Rust so a Candybar can be authored natively in any of the three.
  - **success:** A Candybar authored with each SDK registers, fires on schedule, and reports result and metrics.
- **CAP-6**
  - **intent:** Any executable can be registered as a Candybar without using an SDK.
  - **success:** A plain shell script or binary registers, runs on schedule, and its exit status lands as its result.
- **CAP-7**
  - **intent:** Any registered Candybar can be invoked manually with `candybar run {candybarRegistryId}`.
  - **success:** The CLI command triggers an immediate out-of-schedule invocation whose result and metrics are recorded identically to a scheduled one.
- **CAP-8**
  - **intent:** The system exposes a read-side and control API for schedule consumers: list definitions, query a time window of past invocations and upcoming fire times, skip a single occurrence, skip the rest of a day, and manage a "noise" collection.
  - **success:** A consumer can render a current/upcoming/past schedule view with countdowns, toggle noisy (<10m) crons out of the listing, and apply both skip operations using only this API — contract in `holocene-view-contract.md`.

## Constraints

- Bloodbank is the only scheduling transport: heartbeat and invocation lifecycle events publish as `bloodbank.evt.*` facts over NATS/Dapr, and scheduling business logic lives in cron Subscribers — never in a central crontab or a private timer loop.
- Schedules are expressed in the standard cron format; no bespoke schedule DSL.
- Existing infrastructure only: Prometheus + Grafana for metrics, Postgres for the registry, Candystore as the durable projection of invocation events. No new data stores.
- Secrets appear only as `op://DeLoSecrets/...` references or environment variables, never plaintext.
- All service references use hostnames on `delo.sh`; never hardcode LAN IPs.
- Director Rule: this repo owns the cron framework only — heartbeat daemon, dispatch, registry, SDKs, CLI, and the CAP-8 API. The Holocene schedule view is **DELEGATED** to the `holocene` repo; see `holocene-view-contract.md`.

## Non-goals

- Implementing the Holocene schedule view UI (DELEGATED to the `holocene` repo — contract-only here).
- ~~Migrating existing Hermes crons or user crontab entries onto Candybar (adoption is a later, separate effort).~~ Lifted 2026-10-26 by user directive: Epic 7 migrates them (inventory: `_bmad-output/planning-artifacts/cron-inventory.md`; architecture: spine AD-16..AD-22).
- Replacing or extending NATS/Dapr, or building a new event bus.
- Multi-node scheduling, leader election, or HA — single-node homelab operation.
- Sub-minute scheduling granularity.

## Success signal

Register a new scheduled job as a Candybar — via SDK or a bare executable — and it fires on its cron schedule with Prometheus metrics and registry counts updating, `candybar run <id>` invokes it immediately, and no crontab was touched. The scattered-cron state the braindump describes is gone for every job that has been moved over.

## Delegated (owner: holocene)

The gmail-inbox-style live schedule view (Definitions and Schedule tabs, current/upcoming/past rows, countdowns, noise toggle, skip operations) is implemented in the sibling `holocene` repo. This spec commits only to the API contract that view consumes, specified in `holocene-view-contract.md`. No UI implementation stories exist here.

## Assumptions

- Daemon and CLI are written in Python (`.project.json` declares `primary_language: python`); SDK languages are independent of the daemon's language.
- Heartbeat granularity is one minute, matching the braindump; sub-minute schedules are out of scope.
- The token-spend metric applies only to LLM-invoking Candybars; others report zero.
- Skip operations are schedule overrides stored in the registry and honored by the dispatch path; the noise collection is registry-level state.

## Open Questions

- What is the catch-up policy when the daemon is down through one or more ticks: skip missed fires, run the misfire once, or replay all?
- Is heartbeat delivery at-least-once (subscribers dedupe within a minute tick) or effectively-once via daemon-side dispatch?
