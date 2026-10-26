//! Probe candybar for the cross-language contract tests (tests/test_sdk_rust.py).
//!
//! It demonstrates every outcome mapping the SDK supports. The test suite runs
//! it through the daemon's real executor runner with CANDYBAR_EXAMPLE_MODE
//! selecting the behaviour, then parses the result with the daemon's own
//! result-file model. Build with `cargo build --example outcomes`.

use std::error::Error;
use std::process::ExitCode;

use candybar_sdk::{Context, Outcome};

fn main() -> ExitCode {
    candybar_sdk::run(probe)
}

fn probe(ctx: &Context) -> Result<Outcome, Box<dyn Error>> {
    let mode = std::env::var("CANDYBAR_EXAMPLE_MODE").unwrap_or_else(|_| "success".into());
    match mode.as_str() {
        "success" => Ok(Outcome::success()),
        "metrics" => {
            ctx.add_tokens(100);
            ctx.add_tokens(23);
            ctx.add_cost(0.25)?;
            Ok(Outcome::success_with("done"))
        }
        "fail" => {
            ctx.add_tokens(7);
            Ok(Outcome::fail("quota"))
        }
        "err" => {
            ctx.add_tokens(11);
            Err("boom".into())
        }
        "nul" => {
            ctx.add_tokens(17);
            Err("bad\0byte".into())
        }
        "panic" => {
            ctx.add_tokens(13);
            panic!("kaboom");
        }
        "bad-cost" => {
            ctx.add_tokens(5);
            ctx.add_cost(-1.0)?;
            Ok(Outcome::success())
        }
        "overflow" => {
            ctx.add_tokens(u64::MAX);
            ctx.add_tokens(5);
            Ok(Outcome::success())
        }
        "exotic" => {
            let long = "🍬\"\\\n\t\u{1b}".repeat(400);
            Ok(Outcome::success_with(format!("exotic {long}")))
        }
        "context" => Ok(Outcome::success_with(format!(
            "{}|{}|{}|{}",
            ctx.invocation_id(),
            ctx.candybar_id(),
            ctx.trigger(),
            ctx.tick_id().unwrap_or("")
        ))),
        "threads" => {
            std::thread::scope(|scope| {
                for _ in 0..4 {
                    scope.spawn(|| ctx.add_tokens(250));
                }
            });
            ctx.add_cost(0.0042)?;
            Ok(Outcome::success())
        }
        other => Err(format!("unknown CANDYBAR_EXAMPLE_MODE {other:?}").into()),
    }
}
