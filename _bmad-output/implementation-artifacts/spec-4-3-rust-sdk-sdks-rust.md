---
title: 'Story 4.3: Rust SDK (sdks/rust)'
type: 'feature'
created: '2026-10-26'
status: 'done'
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
- [x] `sdks/rust/Cargo.toml`, `Cargo.lock`, `src/lib.rs` -- crate with `run`, `invoke` (testable core over an env lookup, returns the exit code), `Context`, `Trigger`, `Outcome`, `MetricError`, `ProtocolError`, `EXIT_*`, hand-rolled JSON encoder, atomic write; unit tests over every matrix row; `#![forbid(unsafe_code)]`, docs.
- [x] `sdks/rust/examples/outcomes.rs` -- probe binary selecting each outcome by env, for the cross-language tests.
- [x] `sdks/rust/examples/hello/{Cargo.toml,Cargo.lock,src/main.rs,candybar.toml}` -- standalone author-style crate on `candybar-sdk` (path dep) that reports token spend; schedule `* * * * *`, `noise = true`.
- [x] `sdks/rust/README.md` -- author guide, protocol, decisions (async, `panic=abort`, glibc/musl deploy note).
- [x] `tests/test_sdk_rust.py` -- builds via cargo; runs probes through the real `run_invocation` + `resolve_outcome` and `ResultFile`; `cargo metadata` proves zero deps and no `nats`; shipped manifest loads. Skips without cargo unless `CANDYBAR_REQUIRE_RUST=1`.
- [x] `tests/test_sdk_rust_integration.py` -- NATS+PG: register the example, dispatch a matching tick with the real `Dispatcher`, run the real `Executor`, and expect a row `scheduled|success` with SDK `token_spend`/`cost_usd`, counters and `candybar_invocations_total` +1, and a completed event carrying the metrics. A panicking probe lands `fail` with its message.
- [x] `.github/workflows/ci.yml`, `mise.toml`, `.gitignore`, `README.md` -- Rust 1.99.1 toolchain + fmt/clippy/test, require-rust flag, `target/` ignore, rust pin, docs link.

**Acceptance Criteria:**
- Given the hello example registered from its `candybar.toml`, when the real daemon's heartbeat fires a matching minute, then one invocation row records `scheduled|success` with the SDK-reported `token_spend`/`cost_usd`, counters move, and `/metrics` shows the success sample.
- Given the crate, when `cargo metadata` resolves it, then its only package is `candybar-sdk` and nothing named `nats` appears.

## Implementation Notes

I implemented this inline because this session has no subagent-spawn tool (same as 1.1–1.9). The g33 preflight passed (krebs-enrollment WARN: legacy adapter, as before), and ecosystem-route pointed only at the Bloodbank naming contract. No new event types were added.

The crate is zero-dependency and 1.1k lines, most of them tests. `run(handler)` → `invoke` (the testable core over any env lookup) → `Context` (Send + Sync: an `AtomicU64` token counter and a `Mutex<Option<f64>>` cost, so scoped threads can report spend) → `catch_unwind` → `IntoOutcome`. `IntoOutcome` covers `()`, `&str`/`String`, `Outcome`, and `Result<T: Into<Outcome>, E: Display>`. The JSON encoder is hand-rolled, and the write is atomic (a temp file plus a rename). `fetch_update` is deprecated on 1.99 and was replaced by `Atomic::update`. The executor, dispatcher and registry needed no change.

Verification:
- `cargo fmt --check`, `cargo clippy --locked --all-targets -D warnings`, and `cargo test --locked` pass on the SDK and the hello crate: 27 unit tests and 3 doctests.
- `tests/test_sdk_rust.py` passes (20 tests): the probe runs through the real `run_invocation` + `ResultFile` + `resolve_outcome` in every mode, and `cargo metadata --locked` shows the SDK's only package is itself.
- `tests/test_sdk_rust_integration.py` passes on throwaway nats:2.10.29 + postgres:17-alpine. It checks the shipped `candybar.toml` → real `Dispatcher` → isolated JetStream stream → real `Executor` path: hello lands `scheduled|success|1545|0.0042`, the panic probe lands `fail` with `panic: kaboom` and spend 13, counters and metrics move by one, and the completed and failed events are correct.
- `uv run ruff check .` is clean.

The AC was checked against a real daemon in a throwaway smoke. I ran `candybar daemon` with all four roles on throwaway NATS and Postgres, with the production-shaped BLOODBANK_EVENTS and BLOODBANK_COMMANDS streams, then ran `candybar register sdks/rust/examples/hello`. At 06:17:00 UTC the real heartbeat published `tick:29883257`, the dispatcher dispatched it, and the executor ran the Rust binary in 1 ms. The results:
- Registry row: `scheduled|tick:29883257|success|1545|0.0042`. Counters 1/1.
- `candybar run <id>` exited 0, and its row is `manual||success|1545|0.0042`.
- `/metrics` (OpenMetrics): `candybar_invocations_total{result="success"} 2.0` with an `invocation_id` exemplar, and `{result="fail"} 0.0`.
- BLOODBANK_EVENTS holds created, then invoked and completed twice. Both completed events carry `token_spend:1545, cost_usd:0.0042`.

Everything was torn down afterwards, and SIGTERM drained all roles in under 1 s. The release binary also runs inside `candybar:local`: it needs only `GLIBC_2.34`, and the image has glibc 2.41. CI now proves this on every push.

Shared working tree: four agents worked in the same checkout at once. Two other agents' plain `git commit`s swept up my staged README and CI hunks (74068d3, d64338b). The 4.2 agent then restored the CI hunks to my working tree (8a1f2af), and I committed them in 4da4116. From then on I committed every file with a single add-and-commit limited to my paths. CI briefly failed on a543576 and b37ce64: the runner's preinstalled cargo was 1.99.0, and `rust-version = "1.99.1"` correctly refused it. That was fixed once the pinned-toolchain CI step landed.

## Spec Change Log

## Review Triage Log

I ran the review inline because no subagent-spawn tool was available (same deviation as 1.1–1.9). It applied the blind, edge-case (path tracing plus claims), and verification-gap checks to `/tmp/story-4-3-diff.vJlHm7.txt`, which holds only this story's paths and hunks (82.7 kB). Finding floor N = min(floor(sqrt(82.7) + 1), 10) = 10.

- Blind/Edge (same root cause): a NUL in a handler's message became `\u0000` in the result file, and the stderr failure line echoed it raw (and uncapped). The executor stores `error_message` and `stderr_tail` in Postgres text columns, which reject 0x00. The terminal write would then fail, and the nak'd command would re-run on every redelivery. The 4.1 and 4.2 SDKs found the same issue. Verdict: high. Patched in `a49ca6b`: `clean_message` caps the text and replaces NUL with U+FFFD, matching Python, in both the result file and the stderr line. It is pinned by a Rust unit test and the cross-language `nul` probe test.
- Edge: the default panic hook still prints a panic payload verbatim, so a `panic!` whose text contains NUL can reach `stderr_tail`. Verdict: low. Deferred: the root cause is the pre-existing executor stderr path, which 4.1 already deferred. An SDK-side fix needs a custom panic hook (new behavior and complexity).
- Blind: `Outcome` lacked `#[must_use]`, so `Outcome::fail("x");` dropped without returning compiles silently. Verdict: low. Patched as a direct one-attribute correction.
- Edge: the unanchored `target/` in `.gitignore` ignores any directory named `target` anywhere in the repo. Verdict: low. Patched: it is now anchored to `sdks/**/target/`.
- Edge: with `CARGO_TARGET_DIR` set, `cargo build --release` puts the binary somewhere the example's `entrypoint` does not point, so `candybar register` fails with "entrypoint is not an existing file". Verdict: low. Patched with a doc line in the toml and the README; the tests pass `--target-dir` explicitly.
- Verification gap: the hello crate was never checked by `cargo fmt --check` locally, because `cargo fmt` in `sdks/rust` does not reach a separate package. CI run 40278987452 failed on it. Verdict: medium. Patched: the crate is now formatted, and the local verification runs fmt on both manifests.
- Verification gap: the floor pin was unproven on CI. CI runs on a543576 and b37ce64 failed in Unit tests with "rustc 1.99.0 is not supported" (the runner's preinstalled cargo), before the `Set up Rust` step landed. Verdict: false as a defect: this is the MSRV guard working. It is resolved by the pinned `rustup toolchain install 1.99.1` step.
- Blind: `MetricError` derives `PartialEq` over `f64`, so a NaN rejection compares unequal to itself. Verdict: low. Rejected: nothing compares `MetricError`s, and the fix would add a manual impl.
- Blind: the public `encode_result` silently drops an invalid `cost_usd` instead of erroring. Verdict: low. Rejected: it is documented, `Context` never produces an invalid cost, and the filter is the last guard that keeps the file valid for the executor.
- Blind: `add_tokens` uses `Relaxed` ordering, so the final read could miss thread updates. Verdict: false. `thread::scope` (and any join) establishes happens-before, the read happens after the handler returns, and `threads_can_report_spend_through_a_shared_context` pins it.
- Blind: the temp name `.result.json.<pid>.tmp` could collide across processes. Verdict: false. `run_invocation` mkdtemps a fresh work directory per invocation (`runner.py`), and the pid makes the name unique within it.
- Blind: `catch_unwind` cannot catch `panic = "abort"` or a double panic. Verdict: low. Rejected: it is documented in the README, and the executor still records `failed` from the signal exit code.
- Edge: a non-UTF-8 `CANDYBAR_TICK_ID` is a protocol error even though the var is optional. Verdict: low. Rejected: the executor builds tick ids as ASCII `tick:<int>`, so failing loudly on a corrupt value is correct.
- Edge: with `CANDYBAR_RESULT_FILE` unset, nothing is written and the executor sees only exit 2. Verdict: false as a defect: it is the frozen matrix row "Outside executor". With no path there is nowhere to write.
- Claims check: every Intent, Always/Never, matrix, Tasks and AC claim was checked against the code and the run evidence, and none was falsified. Both ACs are proven: the real-heartbeat smoke at 06:17:00 UTC (see Implementation Notes) and `test_crate_has_no_nats_or_any_other_dependency`.

Zero intent_gap or bad_spec entries, so no loopback was needed. One entry was deferred (the panic-hook NUL path).

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

Decisions where the spine is silent:
- **Zero dependencies.** The JSON is hand-encoded (status, message, metrics in canonical order) rather than pulled in through serde. This keeps the no-NATS guarantee structural, so `cargo metadata` lists one package, and it needs no crates.io access to build. The cross-language tests parse every output with the daemon's own `ResultFile`.
- **Metrics live only on `Context`.** `Outcome` carries no metrics, so spend cannot be counted twice and is kept on `Err` and panic. `token_spend` saturates at `i64::MAX`, the registry's BIGINT. `add_cost` returns `Result<(), MetricError>` (idiomatic `?`) and does not panic.
- **`IntoOutcome` trait.** A handler may return `()`, `&str`/`String` (success with message), `Outcome`, or `Result<T: Into<Outcome>, E: Display>`. An `Err` keeps its `Display` text (like Node). A panic becomes `panic: <payload>`.
- **Exit codes** are 0, 1 and 2. An unwritable result file exits 1 (like Node; Python uses 2). The executor maps both to `failed`, and 4.2 deferred the cross-SDK conformance fixture.
- **`Context` is `Send + Sync`** (an atomic counter and a mutex), so threads spawned by a handler can report spend.
- **Floor pinning.** `rust-version = "1.99.1"` acts as the MSRV guard, `mise.toml` sets `rust = "1.99.1"`, and CI runs `rustup toolchain install 1.99.1`. `publish = false` until distribution is decided (spine Deferred).
- **The example schedule is `* * * * *` with `noise = true`**, so the real-heartbeat smoke fires within a minute. The siblings use `*/5`.

## Verification

**Commands:**
- `cargo +1.99.1 fmt --check && cargo +1.99.1 clippy --all-targets -- -D warnings && cargo +1.99.1 test` in `sdks/rust` -- expected: clean/green
- `CANDYBAR_REQUIRE_RUST=1 uv run pytest tests/test_sdk_rust.py -q` and `uv run ruff check .` -- expected: green/clean
- `CANDYBAR_TEST_NATS=1 CANDYBAR_TEST_PG=1 uv run pytest tests/test_sdk_rust_integration.py` on throwaway nats + postgres -- expected: pass

**Manual checks (if no CLI):**
- Throwaway full daemon (all roles, real heartbeat): `candybar register sdks/rust/examples/hello`, wait for the next minute, then check for the row with token spend and the `/metrics` success sample. The release binary also runs inside `candybar:local` (glibc 2.41).
