// Type-level contract pins for src/index.d.ts — compiled by `npm run typecheck`
// (tsc --noEmit), never executed. Each @ts-expect-error MUST be an error, so a
// loosened declaration fails the check.

import candybarDefault, {
  candybar,
  readContext,
  toResult,
  type CandybarContext,
  type ResultJson,
} from "candybar-sdk";

// A TypeScript author wraps an async function.
const pending: Promise<ResultJson | null> = candybar(async (ctx: CandybarContext) => {
  const tick: string | null = ctx.tickId;
  ctx.addTokens(10);
  ctx.addCost(0.01);
  if (tick === null) return "manual run";
  return { message: `fired on ${tick}`, tokenSpend: 5, costUsd: null };
});
void pending;

// Sync handlers and void returns are fine; the default export is the same function.
void candybarDefault(() => {});
void candybar(async () => ({ status: "fail", message: "nope" }));

// @ts-expect-error status is a closed union
void candybar(async () => ({ status: "ok" }));
// @ts-expect-error tokenSpend is a number
void candybar(async () => ({ tokenSpend: "12" }));
// @ts-expect-error a handler is required
void candybar();

const ctx = readContext({});
// @ts-expect-error the context is read-only
ctx.invocationId = "x";

const written: ResultJson = toResult({ status: "success", tokenSpend: 3 });
const tokens: number = written.metrics.token_spend;
void tokens;
// @ts-expect-error metrics carry snake_case keys only
void written.metrics.tokenSpend;
