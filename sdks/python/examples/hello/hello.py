#!/usr/bin/env python3
"""Example candybar authored with candybar-sdk.

The executor spawns this file directly (it must be executable, and the
`python3` on the executor's PATH must have candybar-sdk installed — the
candybar image ships it). It pretends to do an LLM call and reports the
tokens it "spent", which land in the invocation row and the completed event.
"""

from candybar_sdk import Context, candybar

PROMPT_TOKENS = 120
COMPLETION_TOKENS = 22


@candybar
def main(ctx: Context) -> str:
    # A real job would call its model here and add the provider's usage.
    ctx.add_tokens(PROMPT_TOKENS + COMPLETION_TOKENS)
    ctx.add_cost(0.0007)
    tick = ctx.tick_id or "no tick (manual)"
    return f"hello from {ctx.candybar_id} [{ctx.trigger}, {tick}]"


if __name__ == "__main__":
    main.run()
