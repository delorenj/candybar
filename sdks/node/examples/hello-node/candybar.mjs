#!/usr/bin/env node
// Example node candybar: the golden author shape for candybar-sdk.
import { candybar } from "candybar-sdk";

await candybar(async (ctx) => {
  // A real job would call an LLM here and report what it spent.
  const tokens = Number.parseInt(process.env.HELLO_NODE_TOKENS ?? "42", 10);
  ctx.addTokens(tokens);
  if (process.env.HELLO_NODE_FAIL) {
    throw new Error(`hello-node asked to fail (${ctx.trigger})`);
  }
  const when = ctx.tickId ?? "a manual run";
  return { message: `hello from node on ${when} (${ctx.trigger})` };
});
