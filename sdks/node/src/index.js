// candybar-sdk (Node) — the AD-8 process protocol harness.
//
// Context in via five CANDYBAR_* env vars; result JSON out at
// CANDYBAR_RESULT_FILE. The executor owns execution, metering, accounting and
// events; this module only turns "the handler resolved/rejected" into the
// result file the executor already reads. It never touches the network: the
// import list below is an allowlist pinned by test/process.test.js.

import { rename, writeFile, rm } from "node:fs/promises";
import process from "node:process";

/** Exit codes. The executor treats exit code as primary, so it must agree with the status. */
export const EXIT_SUCCESS = 0;
export const EXIT_FAIL = 1;
export const EXIT_PROTOCOL = 2;

/** The executor caps failure messages at 500 chars; never write more. */
export const MESSAGE_MAX = 500;

/** How long open handles may keep the process alive after the result is written. */
const EXIT_GRACE_MS = 2000;

export const ENV = Object.freeze({
  invocationId: "CANDYBAR_INVOCATION_ID",
  candybarId: "CANDYBAR_ID",
  trigger: "CANDYBAR_TRIGGER",
  tickId: "CANDYBAR_TICK_ID",
  resultFile: "CANDYBAR_RESULT_FILE",
});

const REQUIRED = ["invocationId", "candybarId", "trigger", "resultFile"];

/** Raised when the process was not spawned by the executor (env contract unmet). */
export class ProtocolError extends Error {
  constructor(message) {
    super(message);
    this.name = "ProtocolError";
  }
}

/**
 * Read the AD-8 invocation context from `env` (default `process.env`).
 * `CANDYBAR_TICK_ID` is set-but-empty for manual triggers; that becomes `null`.
 * The trigger is passed through unvalidated (forward-compatible).
 */
export function readContext(env = process.env) {
  const missing = REQUIRED.filter((key) => !env[ENV[key]]);
  if (missing.length > 0) {
    throw new ProtocolError(
      `not running under the candybar executor: missing ${missing.map((k) => ENV[k]).join(", ")}`,
    );
  }
  return {
    invocationId: env[ENV.invocationId],
    candybarId: env[ENV.candybarId],
    trigger: env[ENV.trigger],
    tickId: env[ENV.tickId] ? env[ENV.tickId] : null,
    resultFile: env[ENV.resultFile],
  };
}

function checkTokens(value, label) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError(`${label} must be a non-negative safe integer, got ${String(value)}`);
  }
  return value;
}

function checkCost(value, label) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new TypeError(`${label} must be a finite non-negative number, got ${String(value)}`);
  }
  return value;
}

function capMessage(message) {
  if (message === undefined || message === null) return undefined;
  // Well-formed first, then cap by CODE POINT (Python's str semantics, as the
  // executor counts). A UTF-16 slice can split a surrogate pair; the lone
  // surrogate survives JSON into the registry write, which Postgres rejects —
  // the executor then naks and re-executes the invocation forever.
  // NUL is stripped for the same reason: Postgres text rejects 0x00.
  const points = Array.from(String(message).toWellFormed().replaceAll("\0", ""));
  return points.length > MESSAGE_MAX ? points.slice(0, MESSAGE_MAX).join("") : points.join("");
}

function errorMessage(err) {
  if (err instanceof Error) return err.message || err.name || "handler rejected";
  if (err === undefined) return "handler rejected";
  try {
    return String(err);
  } catch {
    return "handler rejected";
  }
}

/** Per-invocation spend accumulator handed to the handler as part of its context. */
function makeMeter() {
  let tokens = 0;
  let cost = null;
  return {
    addTokens(n) {
      const next = tokens + checkTokens(n, "addTokens(n)");
      tokens = checkTokens(next, "accumulated token_spend");
    },
    addCost(usd) {
      const next = (cost ?? 0) + checkCost(usd, "addCost(usd)");
      cost = checkCost(next, "accumulated cost_usd"); // e.g. 1e308 + 1e308 = Infinity
    },
    get tokens() {
      return tokens;
    },
    get cost() {
      return cost;
    },
  };
}

/**
 * Build the AD-8 result object. `metrics` holds ONLY token_spend/cost_usd —
 * the executor's metrics model forbids anything else.
 */
export function toResult({ status, message, tokenSpend = 0, costUsd = null }) {
  if (status !== "success" && status !== "fail") {
    throw new TypeError(`status must be "success" or "fail", got ${String(status)}`);
  }
  const metrics = { token_spend: checkTokens(tokenSpend, "token_spend") };
  if (costUsd !== null && costUsd !== undefined) {
    metrics.cost_usd = checkCost(costUsd, "cost_usd");
  }
  const result = { status };
  const capped = capMessage(message);
  if (capped !== undefined) result.message = capped;
  result.metrics = metrics;
  return result;
}

/** Atomically write `result` as UTF-8 JSON: temp file then rename (no torn reads). */
export async function writeResult(path, result) {
  const tmp = `${path}.${process.pid}.tmp`;
  try {
    await writeFile(tmp, JSON.stringify(result), { encoding: "utf8" });
    await rename(tmp, path);
  } catch (err) {
    await rm(tmp, { force: true }).catch(() => {});
    throw err;
  }
}

/** Fold the handler's resolved value + the meter into a result (may throw TypeError). */
function settle(value, meter) {
  let status = "success";
  let message;
  let tokenSpend = meter.tokens;
  let costUsd = meter.cost;
  if (typeof value === "string") {
    message = value;
  } else if (value !== undefined && value !== null) {
    if (typeof value !== "object") {
      throw new TypeError(`handler must resolve undefined, a string, or an object, got ${typeof value}`);
    }
    if (value.status !== undefined) {
      if (value.status !== "success" && value.status !== "fail") {
        throw new TypeError(`status must be "success" or "fail", got ${String(value.status)}`);
      }
      status = value.status;
    }
    message = value.message;
    if (value.tokenSpend !== undefined) {
      tokenSpend = checkTokens(
        checkTokens(value.tokenSpend, "tokenSpend") + tokenSpend,
        "total token_spend",
      );
    }
    if (value.costUsd !== undefined && value.costUsd !== null) {
      costUsd = checkCost(value.costUsd, "costUsd") + (costUsd ?? 0);
    }
  }
  return toResult({ status, message, tokenSpend, costUsd });
}

/**
 * Wrap and run a candybar handler under the AD-8 protocol.
 *
 * Reads the context, awaits `handler(ctx)`, writes the result file and sets
 * `process.exitCode` (0 success, 1 fail, 2 protocol error). Never rejects:
 * resolves the written result, or `null` when the env contract is unmet (the
 * handler is then never called).
 */
export async function candybar(handler, options) {
  const opts = options ?? {};
  const env = opts.env ?? process.env;
  const stderr = opts.stderr ?? process.stderr;
  const setExit = opts.setExitCode ?? true;
  const exit = (code) => {
    if (!setExit) return;
    process.exitCode = code;
    // The verdict is final once written. A handle the handler left open (an
    // interval, a DB pool, a keep-alive socket) would otherwise keep the
    // process — and the executor's invocation — alive forever. The timer is
    // unref'd: it only fires if something else is still holding the loop.
    setTimeout(() => {
      stderr.write(
        `candybar-sdk: open handles kept the process alive ${EXIT_GRACE_MS}ms after the result was written; exiting\n`,
      );
      process.exit(code);
    }, EXIT_GRACE_MS).unref();
  };

  if (typeof handler !== "function") {
    stderr.write("candybar-sdk: candybar(handler) requires a function\n");
    exit(EXIT_PROTOCOL);
    return null;
  }

  let context;
  try {
    context = readContext(env);
  } catch (err) {
    stderr.write(`candybar-sdk: ${errorMessage(err)}\n`);
    exit(EXIT_PROTOCOL);
    return null;
  }

  const meter = makeMeter();
  const ctx = Object.freeze({
    ...context,
    addTokens: (n) => meter.addTokens(n),
    addCost: (usd) => meter.addCost(usd),
  });

  let result;
  try {
    const value = await handler(ctx);
    try {
      result = settle(value, meter);
    } catch (err) {
      result = toResult({
        status: "fail",
        message: `invalid result: ${errorMessage(err)}`,
        tokenSpend: meter.tokens,
        costUsd: meter.cost,
      });
    }
  } catch (err) {
    // Stack first, headline last: the executor keeps only the stderr TAIL
    // (500 chars), so the one-line summary must be what survives truncation.
    if (err instanceof Error && err.stack) stderr.write(`${err.stack}\n`);
    stderr.write(`candybar-sdk: handler failed: ${errorMessage(err)}\n`);
    result = toResult({
      status: "fail",
      message: errorMessage(err),
      tokenSpend: meter.tokens,
      costUsd: meter.cost,
    });
  }

  try {
    await writeResult(context.resultFile, result);
  } catch (err) {
    stderr.write(`candybar-sdk: could not write result file ${context.resultFile}: ${errorMessage(err)}\n`);
    exit(EXIT_FAIL);
    return result;
  }
  exit(result.status === "success" ? EXIT_SUCCESS : EXIT_FAIL);
  return result;
}

export default candybar;
