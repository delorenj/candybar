# Epic 3 Context: Full Metering & Observability

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Make every invocation fully metered so an operator can find the expensive candybars: peak memory, wall runtime, self-reported token spend, and average CPU and GPU load, all queryable in Prometheus per candybar and traceable to a single invocation through exemplars. The complete per-invoke record also lives in the `invocations` table and in the terminal lifecycle event, so Prometheus never needs a per-invocation label. Epic 1 already ships the runtime histogram and the result counter; this epic adds the remaining measures.

## Stories

- Story 3.1: Memory peak and average CPU load per invocation
- Story 3.2: Average GPU load per invocation (nvidia-smi or zero)
- Story 3.3: Token spend counter and full exemplar coverage

## Requirements & Constraints

- Measures per invocation: runtime (wall ms histogram), memory (max gauge from cgroup peak), token spend (counter), CPU and GPU load (summaries).
- Prometheus label cardinality is bounded to `{candybar_id, result}` on every family. Per-invocation identity rides only as `invocation_id` exemplars; never add an invocation label.
- The full record (all measures) is carried in `completed`/`failed` event data and stored in the `invocations` row, retrievable via the API.
- Unmeasured metrics report zero (bare executables, GPU-less hosts, non-LLM jobs). Metering failure must never fail the invocation.
- Every invocation runs as a cgroup v2 scoped subprocess spawned by the executor; SDKs never open NATS connections.
- Existing infrastructure only (shared Postgres, live Prometheus, Bloodbank); no new stores.

## Technical Decisions

- Attribution is executor-owned: runtime = wall ms; memory = cgroup `memory.peak`; CPU = cgroup `cpu.stat` usage delta divided by runtime; GPU = `nvidia-smi pmon` per-pid sampling when an NVIDIA GPU is present, else 0; token spend self-reported by the SDK in the result JSON (`metrics.token_spend` integer input+output, optional `cost_usd`), default 0.
- Result JSON shape and env-in protocol are fixed and identical across languages; adding measures must not change what a candybar process writes, except token fields it may self-report.
- Event payload changes are schema changes: the canonical cron schemas live in the bloodbank repo and must stay in sync with this repo's draft copies and pydantic envelopes.
- Registry counters and the terminal invocation row commit in one transaction; the daemon is the sole registry writer; schema changes go through alembic.
- GPU attribution beyond NVIDIA (AMD, multi-tenant sharing) is deferred.

## Cross-Story Dependencies

- All three stories extend the Epic 1 executor (cgroup scope, terminal-row-then-event flow) and metrics surface (`/metrics`, exemplars, zero-initialized result series).
- 3.3's "exemplar on every family" and "full record via the API" close over the families 3.1 and 3.2 add; the API read surface for invocations is shared with Epic 5.
- In the container deployment the executor runs with cgroup scoping disabled (docker is the isolation), so cgroup-sourced measures need a defined fallback there.
