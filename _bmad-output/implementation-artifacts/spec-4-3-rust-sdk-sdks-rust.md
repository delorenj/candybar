---
title: 'Story 4.3: Rust SDK (sdks/rust)'
type: 'feature'
created: '2026-10-26'
status: 'in-progress'
baseline_commit: 'e3b2e62e9fc8624e419ca0df884f2962f361da8a'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-4-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** A Rust author can only ship a candybar as a bare executable today. They have to hand-roll the AD-8 protocol (five env vars in, result JSON out), so token spend is never reported and panics and errors end up as bare exit codes with no message.

**Approach:** Ship `sdks/rust` as the zero-dependency crate `candybar-sdk` (edition 2024, `rust-version = "1.99.1"`). `candybar_sdk::run(handler)` parses the five `CANDYBAR_*` vars into a `Context` and runs the handler. A panic is caught with `catch_unwind`. The SDK then atomically writes the exact AD-8 result JSON and returns the matching `ExitCode`. Token spend and cost accumulate on the `Context`, so they are reported on `Err` and panic too. An example binary crate with a `candybar.toml` proves the tick → dispatcher → executor → SDK → registry/metrics path end to end.

## Boundaries & Constraints

**Always:** The output matches the 4.1/4.2 SDKs and the executor's reader (`ResultFile`): `{"status":"success"|"fail","message"?:str,"metrics":{"token_spend":u64,"cost_usd"?:f64}}`. `message` and `cost_usd` are left out when unset, `metrics` never carries another key, and the message is capped at 500 chars. `token_spend` saturates at `i64::MAX`, the registry's BIGINT bound. `cost_usd` is finite and ≥ 0. Exit codes: 0 success, 1 fail (`Err`, panic, `Outcome::fail`, or an unwritable result file), 2 protocol error (INVOCATION_ID / ID / TRIGGER / RESULT_FILE missing or empty; the handler is not run, and a fail result naming the vars is written when RESULT_FILE is known). Failure detail also goes to stderr. The crate has zero dependencies, so a dependency check can prove there is no NATS.

**Never:** No executor/runner/dispatcher/registry changes. No network, bus, or self-registration (registration stays `candybar register` + `candybar.toml`). No async runtime dependency (authors bring their own). No strict trigger allow-list: an unknown trigger passes through as `Trigger::Other`. No signal handlers. No crates.io publish (deferred).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Bare success | handler returns `Ok(())` | `{"status":"success","metrics":{"token_spend":0}}`, exit 0 | N/A |
| Success + message/metrics | `Ok("done")`, `ctx.add_tokens(100)` + `add_tokens(23)`, `add_cost(0.25)` | message `done`, `token_spend:123`, `cost_usd:0.25`, exit 0 | N/A |
| Explicit fail | `Ok(Outcome::fail("quota"))` | fail, message `quota`, metrics kept, exit 1 | N/A |
| `Err` | `Err("boom")` | fail, message `boom`, metrics kept, stderr line, exit 1 | caught |
| Panic | `panic!("kaboom")` | fail, message `panic: kaboom`, metrics kept, exit 1 | `catch_unwind` |
| Bad cost | `add_cost(-1.0)` / NaN / ∞ | `Err(MetricError)`; accumulator unchanged | author propagates with `?` → fail |
| Token overflow | adds summing past `i64::MAX` | `token_spend = 9223372036854775807` | saturating |
| Manual trigger | `CANDYBAR_TICK_ID=""` | `tick_id() == None`, `trigger() == Manual` | N/A |
| Unknown trigger | `CANDYBAR_TRIGGER=replay` | `Trigger::Other("replay")`, handler runs | N/A |
| Outside executor | RESULT_FILE unset | stderr names missing vars, no file, exit 2 | N/A |
| Partial env | RESULT_FILE set, `CANDYBAR_ID` unset | fail result naming the var, exit 2 | N/A |
| Unwritable result | RESULT_FILE dir missing | stderr error, exit 1 | exit-code fallback → failed |
| Long / exotic message | 2000 chars, quotes, `\`, control chars, emoji | valid JSON, ≤ 500 chars | N/A |

</frozen-after-approval>

## Code Map

- `src/candybar/executor/runner.py` (`child_env`, `ResultFile`, `run_invocation`) and `worker.py` (`resolve_outcome`) define the reader contract and are read-only. The entrypoint runs with no args, with cwd set to its own directory. A nonzero exit plus a result file keeps that file's message and metrics.
- `src/candybar/events/envelope.py:153` `InvocationMetrics` uses `extra="forbid"`; `token_spend` is `int >= 0` and `cost_usd` is `>= 0 | None`. The registry column is BIGINT.
- `src/candybar/registry/manifest.py` `load_manifest` resolves a relative entrypoint against the toml directory. The example's toml must load through it.
- `tests/test_executor_integration.py` (`_ensure_schema`, `_make_script`, isolated streams), `src/candybar/dispatcher/worker.py` (`Dispatcher`) and `test_manual_invoke_integration.py` (RerouteSink): copy these patterns for the scheduled e2e test.
- `.github/workflows/ci.yml`, `mise.toml`, `.gitignore`, `README.md` are shared with concurrent agents. Make minimal additive edits only, and stage only this story's hunks.

## Tasks & Acceptance

**Execution:**
- [ ] `sdks/rust/Cargo.toml`, `Cargo.lock`, `src/lib.rs` -- crate with `run`, `invoke` (testable core over an env lookup, returns the exit code), `Context`, `Trigger`, `Outcome`, `MetricError`, `ProtocolError`, `EXIT_*`, hand-rolled JSON encoder, atomic write; unit tests over every matrix row; `#![forbid(unsafe_code)]`, docs.
- [ ] `sdks/rust/examples/outcomes.rs` -- probe binary selecting each outcome by env, for the cross-language tests.
- [ ] `sdks/rust/examples/hello/{Cargo.toml,Cargo.lock,src/main.rs,candybar.toml}` -- standalone author-style crate on `candybar-sdk` (path dep) that reports token spend; schedule `* * * * *`, `noise = true`.
- [ ] `sdks/rust/README.md` -- author guide, protocol, decisions (async, `panic=abort`, glibc/musl deploy note).
- [ ] `tests/test_sdk_rust.py` -- builds via cargo; runs probes through the real `run_invocation` + `resolve_outcome` and `ResultFile`; `cargo metadata` proves zero deps and no `nats`; shipped manifest loads. Skips without cargo unless `CANDYBAR_REQUIRE_RUST=1`.
- [ ] `tests/test_sdk_rust_integration.py` -- NATS+PG: register the example, dispatch a matching tick with the real `Dispatcher`, run the real `Executor`, and expect a row `scheduled|success` with SDK `token_spend`/`cost_usd`, counters and `candybar_invocations_total` +1, and a completed event carrying the metrics. A panicking probe lands `fail` with its message.
- [ ] `.github/workflows/ci.yml`, `mise.toml`, `.gitignore`, `README.md` -- Rust 1.99.1 toolchain + fmt/clippy/test, require-rust flag, `target/` ignore, rust pin, docs link.

**Acceptance Criteria:**
- Given the hello example registered from its `candybar.toml`, when the real daemon's heartbeat fires a matching minute, then one invocation row records `scheduled|success` with the SDK-reported `token_spend`/`cost_usd`, counters move, and `/metrics` shows the success sample.
- Given the crate, when `cargo metadata` resolves it, then its only package is `candybar-sdk` and nothing named `nats` appears.

## Implementation Notes

## Spec Change Log

## Review Triage Log

## Design Notes

Alignment with the sibling SDKs was decided from their in-flight specs (4.1, 4.2): the same exit codes, the omit-when-unset keys, the 500-char cap, and the pass-through trigger. Metrics live only on the `Context`, never on `Outcome`, so a value cannot be counted twice. `Err` keeps its `Display` text. A panic becomes `panic: <payload>`. The default panic hook still prints the location to stderr, which the executor keeps as the stderr tail.

```rust
fn main() -> std::process::ExitCode {
    candybar_sdk::run(|ctx| -> Result<Outcome, Box<dyn std::error::Error>> {
        ctx.add_tokens(1545);
        ctx.add_cost(0.0042)?;
        Ok(Outcome::success_with("greeted"))
    })
}
```

## Verification

**Commands:**
- `cargo +1.99.1 fmt --check && cargo +1.99.1 clippy --all-targets -- -D warnings && cargo +1.99.1 test` in `sdks/rust` -- expected: clean/green
- `CANDYBAR_REQUIRE_RUST=1 uv run pytest tests/test_sdk_rust.py -q` and `uv run ruff check .` -- expected: green/clean
- `CANDYBAR_TEST_NATS=1 CANDYBAR_TEST_PG=1 uv run pytest tests/test_sdk_rust_integration.py` on throwaway nats + postgres -- expected: pass

**Manual checks (if no CLI):**
- Throwaway full daemon (all roles, real heartbeat): `candybar register sdks/rust/examples/hello`, wait for the next minute, then check for the row with token spend and the `/metrics` success sample. The release binary also runs inside `candybar:local` (glibc 2.41).
