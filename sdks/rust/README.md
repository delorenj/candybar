# candybar-sdk (Rust)

Author candybar cron jobs in Rust. The crate implements the executor's
process protocol (AD-8): the invocation context comes in through five
`CANDYBAR_*` env vars, and the result goes out as JSON at
`CANDYBAR_RESULT_FILE`. It has **zero dependencies** and never opens a
network or NATS connection. The executor owns execution, metering and events.

- Crate: `candybar-sdk` (lib name `candybar_sdk`), edition 2024
- Toolchain floor: Rust 1.99.1 (`rust-version`, pinned in CI and `mise.toml`)
- Not on crates.io yet (publishing is deferred). Depend on it by path or git:

```toml
[dependencies]
candybar-sdk = { git = "https://github.com/delorenj/candybar" }
```

## Write a candybar

```rust
use std::error::Error;
use std::process::ExitCode;

use candybar_sdk::{Context, Outcome};

fn main() -> ExitCode {
    candybar_sdk::run(job)
}

fn job(ctx: &Context) -> Result<Outcome, Box<dyn Error>> {
    // ... call your model, then report what it consumed (input + output):
    ctx.add_tokens(1200 + 345);
    ctx.add_cost(0.0042)?; // optional, US dollars
    Ok(Outcome::success_with(format!("ran on a {} trigger", ctx.trigger())))
}
```

Register the compiled binary with a normal `candybar.toml` (the executor
runs `entrypoint` directly, with no arguments):

```toml
[candybar]
name = "my-job"
schedule = "*/15 * * * *"
entrypoint = "target/release/my-job"   # relative to this file
```

```bash
cargo build --release
candybar register .
```

The working example is [`examples/hello`](examples/hello), a standalone crate
like an author's own. Build and register it with
`cargo build --release --manifest-path sdks/rust/examples/hello/Cargo.toml`
and then `candybar register sdks/rust/examples/hello`. If you set
`CARGO_TARGET_DIR`, add `--target-dir sdks/rust/examples/hello/target` so the
binary lands where the example's `entrypoint` expects it.

## What the handler can return

| Handler returns | Result file | Exit |
|---|---|---|
| `()` / `Ok(())` | `{"status":"success","metrics":{"token_spend":N}}` | 0 |
| `"text"` / `String` / `Ok("text")` / `Outcome::success_with(..)` | success with `message` | 0 |
| `Ok(Outcome::fail("quota"))` / `Outcome::fail(..)` | fail with `message` | 1 |
| `Err(e)` (any `e: Display`, e.g. `Box<dyn Error>`, `anyhow::Error`) | fail, `message` = `e.to_string()` | 1 |
| a panic | fail, `message` = `panic: <payload>` | 1 |

Token spend and cost accumulate on the `Context`, never on the `Outcome`, so
they are reported on failures and panics too: a failed LLM call still spent
tokens. Failure detail also goes to stderr, which the executor keeps as the
failed event's stderr tail.

## Protocol

| Env var | Meaning |
|---|---|
| `CANDYBAR_INVOCATION_ID` | this invocation's id |
| `CANDYBAR_ID` | the candybar's registry id |
| `CANDYBAR_TRIGGER` | `scheduled`, `manual` or `misfire`. Unknown values pass through as `Trigger::Other` |
| `CANDYBAR_TICK_ID` | `tick:<unix_epoch_minute>`; empty for manual runs (`ctx.tick_id()` is `None`) |
| `CANDYBAR_RESULT_FILE` | where the result JSON is written |

The result is written atomically (a temp file in the same directory, then a
rename): `{"status":"success"|"fail","message"?:string,"metrics":{"token_spend":int,"cost_usd"?:number}}`.

- `message` and `cost_usd` are omitted when unset, and `metrics` never holds
  another key (the executor rejects any).
- `message` is capped at 500 characters, and NUL is replaced by U+FFFD
  (the registry's Postgres text columns reject NUL).
- `token_spend` saturates at `i64::MAX` (the registry stores a BIGINT).
- `add_cost` rejects negative, NaN and infinite values with a `MetricError`
  and leaves the total unchanged. Propagate it with `?` to fail the run.

Exit codes: `0` success; `1` fail (`Err`, a panic, `Outcome::fail`, or an
unwritable result file); `2` protocol error. A protocol error means a required
var is missing or empty because the process was not started by the executor.
In that case the handler does not run, and if `CANDYBAR_RESULT_FILE` is known
a fail result naming the missing vars is written. The executor reads the exit
code first, so it always agrees with the status.

## Notes

- **Async:** the SDK is runtime-agnostic. Block on your runtime inside the
  handler, e.g. `tokio::runtime::Runtime::new()?.block_on(async { .. })`.
- **Threads:** `Context` is `Send + Sync`, so threads spawned by the handler
  can share `&Context` (for example with `std::thread::scope`) and call
  `add_tokens`/`add_cost` concurrently.
- **`panic = "abort"`:** panics can only be caught with the default unwind
  strategy. Under `abort` the process dies by SIGABRT and the executor
  records a bare failure with no result file. Token spend is lost, but the
  verdict is still `failed`.
- **`std::process::exit` in a handler** skips the result file. The executor
  then falls back to the exit code alone.
- **Unit-testing your handler:** build a `Context` without touching the
  process env with `Context::from_lookup(|k| map.get(k).map(OsString::from))`.
- **Deploying into the candybar container:** the daemon image is
  `python:3.13-slim` (Debian trixie, glibc 2.41). A binary built on a newer
  host still loads there as long as it needs no newer glibc symbols (the
  example needs only `GLIBC_2.34`). For full portability build a static binary:
  `rustup target add x86_64-unknown-linux-musl && cargo build --release --target x86_64-unknown-linux-musl`.

## Develop

```bash
cd sdks/rust
cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test
CANDYBAR_REQUIRE_RUST=1 uv run pytest ../../tests/test_sdk_rust.py   # cross-language contract
```

`tests/test_sdk_rust.py` runs the `examples/outcomes.rs` probe through the
daemon's real executor runner and reads the result with the daemon's own
result-file model. It also uses `cargo metadata` to prove the crate has no
dependencies. `tests/test_sdk_rust_integration.py` (live NATS + Postgres)
registers the hello example from its `candybar.toml` and drives it through the
real dispatcher and executor.
