# Epic 4 Context: Native SDKs (Python, Node, Rust)

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Let a candybar author write a scheduled job natively in Python, Node, or Rust and get the invocation context, result reporting, and self-reported token spend for free, without learning the executor's internals. All three SDKs are thin harnesses over one identical process protocol (env vars in, result JSON out), so a candybar behaves the same whatever language it is written in, and no SDK ever becomes a second executor or touches the event bus.

## Stories

- Story 4.1: Python SDK (`sdks/python`)
- Story 4.2: Node SDK (`sdks/node`)
- Story 4.3: Rust SDK (`sdks/rust`)

## Requirements & Constraints

- One protocol, three languages. Input: the executor sets five env vars on every spawn: `CANDYBAR_INVOCATION_ID`, `CANDYBAR_ID`, `CANDYBAR_TRIGGER` (`scheduled|manual|misfire`), `CANDYBAR_TICK_ID` (always present, empty for manual), `CANDYBAR_RESULT_FILE`. Output: UTF-8 JSON at `CANDYBAR_RESULT_FILE` shaped `{status: "success"|"fail", message?, metrics: {token_spend?, cost_usd?}}`.
- `token_spend` is total tokens consumed (input + output) as a non-negative integer; `cost_usd` is optional and non-negative. Jobs that call no LLM report 0.
- A handler failure (exception, rejected promise, panic, or `Err`) maps to `{status: "fail"}` with the message captured.
- SDKs never open a NATS connection; each story proves it with a dependency check.
- Each SDK story ends with an example candybar that fires on schedule end to end with metrics recorded.
- Registration is not an SDK concern. The `candybar.toml` manifest (name, description, tags, schedule, entrypoint, noise) goes through the existing `candybar register` CLI/API path. The entrypoint is executed directly with no arguments, so it must be an executable file.
- Toolchain floors: Python ≥ 3.13, Node ≥ 24 LTS, Rust ≥ 1.99.1. Package names are `candybar-sdk` in all three ecosystems. Registry publishing (pypi/npm/crates.io) is deferred.
- CI runs on GitHub-hosted runners (public repo). No secrets and no LAN IPs in any SDK surface.

## Technical Decisions

- **Executor owns execution:** every invocation, SDK-authored or bare, is a cgroup v2 scoped subprocess spawned by the executor. The executor measures runtime, memory, CPU and GPU itself; the SDK only self-reports token spend and cost.
- **How the executor reads the result (already built, do not change it to suit an SDK):** the exit code comes first. A nonzero exit is `failed`, using the result file's message and metrics when present. Exit 0 with a valid result file uses that file's status. Exit 0 with no result file is a bare-executable success with zero metrics. Exit 0 with an invalid result file is `failed`. Unknown top-level keys in the result file are ignored, but `metrics` accepts only `token_spend` and `cost_usd`; any other key makes the whole file invalid.
- **Bounds the daemon enforces:** `token_spend` is stored as a signed 64-bit integer; `cost_usd` must be ≥ 0. An SDK must never write a value outside those bounds.
- **Failure detail:** the executor keeps the child's stderr tail (max 500 chars, redacted) on failed invocations, so an SDK should print failure context to stderr as well as into `message`.
- **Metrics land per candybar:** Prometheus series are labeled `{candybar_id, result}` only, with `invocation_id` exemplars. The full record (including token spend) lives in the `invocations` row and the `completed`/`failed` event data.

## Cross-Story Dependencies

- All three stories depend only on Epic 1: the executor's process protocol (1.6), metrics (1.7), the register path (1.4), and the dispatcher and heartbeat (1.3/1.5) for scheduled end-to-end runs. The three SDKs are independent of each other and can be built in parallel.
- The token-spend Prometheus counter and full exemplar coverage are Epic 3 (Story 3.3). Until then, SDK-reported token spend is recorded in the `invocations` row and the terminal event data, not as a Prometheus series.
