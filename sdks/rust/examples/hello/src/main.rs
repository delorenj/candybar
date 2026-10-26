//! Example candybar authored with candybar-sdk.
//!
//! The executor spawns the compiled binary directly with the five
//! `CANDYBAR_*` env vars. It pretends to call an LLM and reports the tokens
//! and cost it "spent", which land in the invocation row and in the
//! completed event's data.

use std::error::Error;
use std::process::ExitCode;

use candybar_sdk::{Context, Outcome};

const PROMPT_TOKENS: u64 = 1200;
const COMPLETION_TOKENS: u64 = 345;
const COST_USD: f64 = 0.0042;

fn main() -> ExitCode {
    candybar_sdk::run(greet)
}

fn greet(ctx: &Context) -> Result<Outcome, Box<dyn Error>> {
    let tick = ctx.tick_id().unwrap_or("no tick (manual)");
    println!(
        "hello from rust: candybar {} invocation {} [{}, {}]",
        ctx.candybar_id(),
        ctx.invocation_id(),
        ctx.trigger(),
        tick
    );
    // A real job would call its model here and add the provider's usage.
    ctx.add_tokens(PROMPT_TOKENS + COMPLETION_TOKENS);
    ctx.add_cost(COST_USD)?;
    Ok(Outcome::success_with(format!(
        "hello from rust [{}, {tick}]",
        ctx.trigger()
    )))
}
