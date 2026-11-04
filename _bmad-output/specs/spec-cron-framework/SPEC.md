---
id: SPEC-cron-framework
companions:
  - holocene-view-contract.md
  - architecture-diagrams.md
  - finite-schedules-contract.md
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
  - **intent:** Every scheduled unit is a `Candybar` implementing `invoke`, one schedule variant (existing standard five-field cron or finite local date/time list), and `result` (success/fail).
  - **success:** A registered cron Candybar fires at each dictated time as before; a finite Candybar fires and durably settles each listed occurrence once, with its result recorded — see `finite-schedules-contract.md`.
- **CAP-3**
  - **intent:** Every Candybar invocation is metered to Prometheus: total memory, runtime in ms, token spend, and average CPU/GPU load.
  - **success:** All four metric families are queryable in Prometheus, labeled per Candybar and per invocation.
- **CAP-4**
  - **intent:** A Postgres registry holds the active Candybar inventory and durable invocation identity/history after deletion.
  - **success:** Active definitions return name/description/tags and matching invocation totals; a deleted UUID still resolves its retained history and dedupe evidence, never conflated with a reused name.
- **CAP-5**
  - **intent:** SDKs exist for Node, Python, and Rust so a Candybar can be authored natively in any of the three.
  - **success:** A Candybar authored with each SDK registers, fires on schedule, and reports result and metrics.
- **CAP-6**
  - **intent:** Any executable can be registered as a Candybar without using an SDK.
  - **success:** A plain shell script or binary registers, runs on schedule, and its exit status lands as its result.
- **CAP-7**
  - **intent:** Any registered Candybar can be invoked manually with `candybar run {candybarRegistryId}`.
  - **success:** The CLI command triggers an immediate out-of-schedule invocation with result/metrics recorded; it never consumes a finite scheduled date.
- **CAP-8**
  - **intent:** The system exposes a read-side and control API for schedule consumers: list definitions, query a time window of past invocations and upcoming fire times, skip a single occurrence, skip the rest of a day, and manage a "noise" collection.
  - **success:** A consumer renders current/upcoming/past cron and finite rows, filters noisy crons and applies skip controls without deleted definitions reappearing — contracts in `holocene-view-contract.md` and `finite-schedules-contract.md`.
- **CAP-9**
  - **intent:** An operator can register a one-time or finite list of exact local dates instead of a recurring cron.
  - **success:** One-date and three-date jobs each invoke once per listed minute, with distinct durable terminal dispositions and UTC fire times.
- **CAP-10**
  - **intent:** An operator can permanently delete a definition without losing its historical identity.
  - **success:** API/CLI deletion removes the active definition, repeats by known UUID are idempotent, active work blocks removal, and UUID history and invocation lookup remain available.
- **CAP-11**
  - **intent:** An exhausted finite Candybar removes its active definition after all of its occurrences settle.
  - **success:** Out-of-order terminal completion and restart produce one recoverable deletion without early removal or historical loss; a governed deletion fact is eventually published.

## Constraints

- Bloodbank is the only scheduling transport: heartbeat and invocation lifecycle events publish as `bloodbank.evt.*` facts over NATS/Dapr, and scheduling business logic lives in cron Subscribers — never in a central crontab or a private timer loop.
- Cron schedules retain standard five-field format; a finite schedule is the only alternative and uses a nonempty minute-aligned list of local ISO datetimes with IANA timezone. One shared schedule Strategy serves all schedule readers; UTC fire times and existing cron behavior remain unchanged.
- A finite date claim/edit/settlement/delete is serialized under its candybar's DB lock; every listed occurrence settles durably before self-deletion. Manual runs never consume dates; no wall-clock-only or publication-only completion.
- Real deletion removes the active definition, not the reversible archive, while keeping historical UUID identity, invocations and dedupe keys under existing retention. The delete transaction includes the stable-ID outbox fact; Bloodbank governance approves the schema/type before any publication.
- Owner choices for mixed-past inputs, missed/pause policy, DST folds/gaps and edit/settlement rules remain gated; details in `finite-schedules-contract.md`.
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
- A second clock, broad rewrite of existing API writes to an outbox, new history browser endpoint, new purge policy or rewriting the owner-approved reversible archive as deletion.

## Success signal

Register a new scheduled job as a Candybar — via SDK or a bare executable — and it fires on its cron schedule with Prometheus metrics and registry counts updating, `candybar run <id>` invokes it immediately, and no crontab was touched. A finite three-date job additionally produces three terminal scheduled runs and intended effects, then disappears from active listings while its UUID history remains queryable; a one-date job behaves the same. The scattered-cron state the braindump describes is gone for every job that has been moved over.

## Delegated (owner: holocene)

The gmail-inbox-style live schedule view (Definitions and Schedule tabs, current/upcoming/past rows, countdowns, noise toggle, skip operations) is implemented in the sibling `holocene` repo. This spec commits only to the API contract that view consumes, specified in `holocene-view-contract.md`. No UI implementation stories exist here.

## Assumptions

- Daemon and CLI are written in Python (`.project.json` declares `primary_language: python`); SDK languages are independent of the daemon's language.
- Heartbeat granularity is one minute, matching the braindump; sub-minute schedules are out of scope.
- The token-spend metric applies only to LLM-invoking Candybars; others report zero.
- Skip operations are schedule overrides stored in the registry and honored by the dispatch path; the noise collection is registry-level state.

## Open Questions

- ~~What is the catch-up policy when the daemon is down through one or more ticks: skip missed fires, run the misfire once, or replay all?~~ Resolved for recurring cron by architecture AD-6 (coalesce latest within 24h); finite missed-fire policy is still open below.
- ~~Is heartbeat delivery at-least-once (subscribers dedupe within a minute tick) or effectively-once via daemon-side dispatch?~~ Resolved for cron by architecture AD-5 (at-least-once ticks with idempotent dispatch).
- Jarad: for mixed past/future date lists, reject the entire list or accept future entries and record past skips? All-past is rejected.
- Jarad: do missed finite occurrences run late or settle as recorded skips, including executor backlog and pause? On pause, settle during pause or on resume, and may a paused exhausted definition delete?
- Jarad: reject nonexistent/ambiguous local DST times or specify exact gap and fold mappings?
- Jarad: may claimed/settled dates or timezone change, which future dates are editable, and do failure/operator skip/overlap count as terminal only after durable settlement? Owner gate `t_7204a764` must resolve these before implementation.
