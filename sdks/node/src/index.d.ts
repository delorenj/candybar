// Type declarations for candybar-sdk (hand-written; checked by `npm run typecheck`).

/** The three triggers the executor sends today; unknown future values pass through. */
export type Trigger = "scheduled" | "manual" | "misfire" | (string & {});

/** The AD-8 invocation context, read from the five CANDYBAR_* env vars. */
export interface InvocationContext {
  readonly invocationId: string;
  readonly candybarId: string;
  readonly trigger: Trigger;
  /** `tick:<unix_epoch_minute>` for scheduled/misfire fires; `null` for manual ones. */
  readonly tickId: string | null;
  readonly resultFile: string;
}

/** What the handler receives: the context plus spend reporting. */
export interface CandybarContext extends InvocationContext {
  /** Add self-reported tokens (input + output). Non-negative safe integer, else TypeError. */
  addTokens(n: number): void;
  /** Add self-reported spend in USD. Finite and non-negative, else TypeError. */
  addCost(usd: number): void;
}

/** An object a handler may resolve; spend here is added to anything reported via the context. */
export interface HandlerOutcome {
  status?: "success" | "fail";
  message?: string;
  tokenSpend?: number;
  costUsd?: number | null;
}

export type HandlerReturn = void | undefined | null | string | HandlerOutcome;

export type Handler = (ctx: CandybarContext) => HandlerReturn | Promise<HandlerReturn>;

/** The exact AD-8 result JSON written to CANDYBAR_RESULT_FILE. */
export interface ResultJson {
  status: "success" | "fail";
  message?: string;
  metrics: {
    token_spend: number;
    cost_usd?: number;
  };
}

/** Minimal writable used for diagnostics (defaults to process.stderr). */
export interface DiagnosticStream {
  write(chunk: string): unknown;
}

export interface CandybarOptions {
  /** Environment to read the context from. Default: `process.env`. */
  env?: Record<string, string | undefined>;
  /** Where diagnostics go. Default: `process.stderr`. */
  stderr?: DiagnosticStream;
  /** Set `process.exitCode` from the outcome. Default: `true`. */
  setExitCode?: boolean;
}

export const EXIT_SUCCESS: 0;
export const EXIT_FAIL: 1;
export const EXIT_PROTOCOL: 2;
export const MESSAGE_MAX: 500;

export const ENV: Readonly<{
  invocationId: "CANDYBAR_INVOCATION_ID";
  candybarId: "CANDYBAR_ID";
  trigger: "CANDYBAR_TRIGGER";
  tickId: "CANDYBAR_TICK_ID";
  resultFile: "CANDYBAR_RESULT_FILE";
}>;

/** Raised by `readContext` when the env contract is unmet. */
export class ProtocolError extends Error {
  constructor(message: string);
}

export function readContext(env?: Record<string, string | undefined>): InvocationContext;

export function toResult(input: {
  status: "success" | "fail";
  message?: string;
  tokenSpend?: number;
  costUsd?: number | null;
}): ResultJson;

export function writeResult(path: string, result: ResultJson): Promise<void>;

/**
 * Wrap and run a handler under the AD-8 protocol. Never rejects: resolves the
 * written result, or `null` when not running under the executor.
 */
export function candybar(handler: Handler, options?: CandybarOptions): Promise<ResultJson | null>;

export default candybar;
