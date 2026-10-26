// In-process protocol tests: one test per I/O matrix row in the story spec.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, chmod, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import candybarDefault, {
  candybar,
  readContext,
  toResult,
  writeResult,
  ProtocolError,
  MESSAGE_MAX,
  ENV,
} from "candybar-sdk";

async function harness(overrides = {}) {
  const dir = await mkdtemp(join(tmpdir(), "candybar-sdk-"));
  const resultFile = join(dir, "result.json");
  const env = {
    CANDYBAR_INVOCATION_ID: "0193b2a0-0000-7000-8000-000000000001",
    CANDYBAR_ID: "0193b2a0-0000-7000-8000-0000000000aa",
    CANDYBAR_TRIGGER: "scheduled",
    CANDYBAR_TICK_ID: "tick:29883066",
    CANDYBAR_RESULT_FILE: resultFile,
    ...overrides,
  };
  const lines = [];
  const stderr = { write: (chunk) => lines.push(String(chunk)) };
  const opts = { env, stderr, setExitCode: false };
  const read = async () => JSON.parse(await readFile(resultFile, "utf8"));
  return { dir, resultFile, env, opts, lines, read };
}

test("success with no value writes the bare success shape", async () => {
  const h = await harness();
  const returned = await candybar(async () => {}, h.opts);
  const written = await h.read();
  assert.deepEqual(written, { status: "success", metrics: { token_spend: 0 } });
  assert.deepEqual(returned, written);
  assert.deepEqual(Object.keys(written.metrics), ["token_spend"]);
});

test("success with a string sets the message", async () => {
  const h = await harness();
  await candybar(async () => "done", h.opts);
  assert.deepEqual(await h.read(), { status: "success", message: "done", metrics: { token_spend: 0 } });
});

test("returned spend is added on top of context-reported spend", async () => {
  const h = await harness();
  await candybar(async (ctx) => {
    ctx.addTokens(100);
    ctx.addTokens(20);
    ctx.addCost(0.25);
    return { message: "summarized", tokenSpend: 5, costUsd: 0.5 };
  }, h.opts);
  assert.deepEqual(await h.read(), {
    status: "success",
    message: "summarized",
    metrics: { token_spend: 125, cost_usd: 0.75 },
  });
});

test("cost_usd is omitted when never reported, kept when explicitly zero", async () => {
  const h = await harness();
  await candybar(async () => ({ tokenSpend: 7, costUsd: null }), h.opts);
  assert.deepEqual((await h.read()).metrics, { token_spend: 7 });
  const z = await harness();
  await candybar(async () => ({ costUsd: 0 }), z.opts);
  assert.deepEqual((await z.read()).metrics, { token_spend: 0, cost_usd: 0 });
});

test("explicit fail keeps message and metrics", async () => {
  const h = await harness();
  const res = await candybar(async (ctx) => {
    ctx.addTokens(9);
    return { status: "fail", message: "upstream 503" };
  }, h.opts);
  assert.deepEqual(res, { status: "fail", message: "upstream 503", metrics: { token_spend: 9 } });
  assert.deepEqual(await h.read(), res);
});

test("rejected promise maps to fail with the error message and keeps spend", async () => {
  const h = await harness();
  const res = await candybar(async (ctx) => {
    ctx.addTokens(33);
    ctx.addCost(0.01);
    throw new Error("boom");
  }, h.opts);
  assert.deepEqual(res, { status: "fail", message: "boom", metrics: { token_spend: 33, cost_usd: 0.01 } });
  assert.deepEqual(await h.read(), res);
  assert.match(h.lines.join(""), /Error: boom\n    at /);
  assert.match(h.lines.at(-1), /^candybar-sdk: handler failed: boom\n$/);
});

test("a synchronous throw is handled like a rejection", async () => {
  const h = await harness();
  const res = await candybar(() => {
    throw new TypeError("sync bad");
  }, h.opts);
  assert.equal(res.status, "fail");
  assert.equal(res.message, "sync bad");
});

test("non-Error rejections are stringified", async () => {
  for (const [value, expected] of [
    ["plain string", "plain string"],
    [42, "42"],
    [undefined, "handler rejected"],
    [Object.create(null), "handler rejected"], // String() throws on a null-prototype object
    [new Error(""), "Error"],
  ]) {
    const h = await harness();
    const res = await candybar(async () => {
      throw value;
    }, h.opts);
    assert.equal(res.status, "fail");
    assert.equal(res.message, expected);
  }
});

test("invalid returned metrics become a fail with only valid accumulated spend", async () => {
  for (const bad of [
    { tokenSpend: 1.5 },
    { tokenSpend: -1 },
    { tokenSpend: Number.NaN },
    { tokenSpend: "12" },
    { tokenSpend: Number.MAX_SAFE_INTEGER + 2 },
    { costUsd: -1 },
    { costUsd: Number.POSITIVE_INFINITY },
    { status: "ok" },
    12,
  ]) {
    const h = await harness();
    const res = await candybar(async (ctx) => {
      ctx.addTokens(4);
      return bad;
    }, h.opts);
    assert.equal(res.status, "fail", JSON.stringify(bad));
    assert.match(res.message, /^invalid result: /);
    assert.deepEqual(res.metrics, { token_spend: 4 });
  }
});

test("addTokens/addCost reject bad values as a handler failure", async () => {
  const h = await harness();
  const res = await candybar(async (ctx) => {
    ctx.addTokens(10);
    ctx.addTokens(2.5);
  }, h.opts);
  assert.equal(res.status, "fail");
  assert.match(res.message, /addTokens\(n\) must be a non-negative safe integer/);
  assert.deepEqual(res.metrics, { token_spend: 10 });
  const c = await harness();
  const resC = await candybar(async (ctx) => ctx.addCost(-0.1), c.opts);
  assert.match(resC.message, /addCost\(usd\) must be a finite non-negative number/);
});

test("accumulated tokens that overflow the safe range fail instead of corrupting", async () => {
  const h = await harness();
  const res = await candybar(async (ctx) => {
    ctx.addTokens(Number.MAX_SAFE_INTEGER);
    ctx.addTokens(10);
  }, h.opts);
  assert.equal(res.status, "fail");
  assert.deepEqual(res.metrics, { token_spend: Number.MAX_SAFE_INTEGER });
});

test("accumulated cost that overflows to Infinity fails instead of rejecting", async () => {
  // Review finding: only the increment was checked, so the sum reached
  // Infinity and toResult threw inside the failure path — candybar() rejected
  // and no result file was written.
  const h = await harness();
  const res = await candybar(async (ctx) => {
    ctx.addCost(1e308);
    ctx.addCost(1e308);
  }, h.opts);
  assert.equal(res.status, "fail");
  assert.match(res.message, /accumulated cost_usd must be a finite non-negative number/);
  assert.deepEqual((await h.read()).metrics, { token_spend: 0, cost_usd: 1e308 });
});

test("messages are capped at the executor's 500-char limit", async () => {
  const h = await harness();
  const res = await candybar(async () => {
    throw new Error("x".repeat(2000));
  }, h.opts);
  assert.equal(res.message.length, MESSAGE_MAX);
  assert.equal((await h.read()).message.length, 500);
});

test("the cap counts code points and never splits a surrogate pair", async () => {
  // Review finding: a UTF-16 slice at 500 left a lone surrogate, which the
  // registry's Postgres write rejects (UnicodeEncodeError -> nak -> re-run).
  const h = await harness();
  const res = await candybar(async () => {
    throw new Error("x".repeat(499) + "\u{1F600}" + "tail");
  }, h.opts);
  const points = Array.from(res.message);
  assert.equal(points.length, MESSAGE_MAX);
  assert.equal(points.at(-1), "\u{1F600}");
  assert.ok(res.message.isWellFormed());
  assert.ok((await h.read()).message.isWellFormed());
});

test("NUL bytes are stripped from messages (Postgres text rejects 0x00)", async () => {
  const h = await harness();
  const res = await candybar(async () => {
    throw new Error("nul\0in\0message");
  }, h.opts);
  assert.equal(res.message, "nulinmessage");
  assert.ok(!JSON.stringify(await h.read()).includes("\\u0000"));
});

test("lone surrogates in an author message are replaced, not written", async () => {
  const h = await harness();
  const res = await candybar(async () => ({ status: "fail", message: "bad \uD83D data" }), h.opts);
  assert.equal(res.message, "bad � data");
  assert.ok(!JSON.stringify(await h.read()).includes("\\ud83d"));
});


test("manual trigger: empty tick id becomes null", async () => {
  const h = await harness({ CANDYBAR_TRIGGER: "manual", CANDYBAR_TICK_ID: "" });
  let seen;
  await candybar(async (ctx) => {
    seen = { trigger: ctx.trigger, tickId: ctx.tickId };
  }, h.opts);
  assert.deepEqual(seen, { trigger: "manual", tickId: null });
});

test("the handler sees the five-var context, frozen", async () => {
  const h = await harness({ CANDYBAR_TRIGGER: "misfire" });
  let seen;
  await candybar(async (ctx) => {
    seen = ctx;
  }, h.opts);
  assert.equal(seen.invocationId, h.env.CANDYBAR_INVOCATION_ID);
  assert.equal(seen.candybarId, h.env.CANDYBAR_ID);
  assert.equal(seen.trigger, "misfire");
  assert.equal(seen.tickId, "tick:29883066");
  assert.equal(seen.resultFile, h.resultFile);
  assert.ok(Object.isFrozen(seen));
});

test("outside the executor: handler never runs, resolves null, no file", async () => {
  for (const key of ["CANDYBAR_INVOCATION_ID", "CANDYBAR_ID", "CANDYBAR_TRIGGER", "CANDYBAR_RESULT_FILE"]) {
    for (const value of [undefined, ""]) {
      const h = await harness({ [key]: value });
      let called = false;
      const res = await candybar(async () => {
        called = true;
      }, h.opts);
      assert.equal(res, null, key);
      assert.equal(called, false, key);
      assert.match(h.lines.join(""), new RegExp(`missing ${key}`));
      assert.deepEqual(await readdir(h.dir), []);
    }
  }
});

test("a missing tick id var is tolerated as null", async () => {
  const h = await harness({ CANDYBAR_TICK_ID: undefined });
  assert.equal(readContext(h.env).tickId, null);
});

test("readContext throws ProtocolError naming every missing var", () => {
  assert.throws(() => readContext({}), (err) => {
    assert.ok(err instanceof ProtocolError);
    for (const key of [ENV.invocationId, ENV.candybarId, ENV.trigger, ENV.resultFile]) {
      assert.ok(err.message.includes(key));
    }
    assert.ok(!err.message.includes(ENV.tickId));
    return true;
  });
});

test("unwritable result file: diagnostics, result still resolved", async () => {
  const h = await harness();
  const locked = join(h.dir, "locked");
  await mkdir(locked);
  await chmod(locked, 0o500);
  const opts = { ...h.opts, env: { ...h.env, CANDYBAR_RESULT_FILE: join(locked, "result.json") } };
  const res = await candybar(async () => "fine", opts);
  assert.equal(res.status, "success");
  assert.match(h.lines.join(""), /could not write result file/);
  assert.deepEqual(await readdir(locked), []); // no temp file left behind
});

test("writeResult is atomic: no temp file remains after a write", async () => {
  const h = await harness();
  await writeResult(h.resultFile, toResult({ status: "success" }));
  assert.deepEqual(await readdir(h.dir), ["result.json"]);
});

test("toResult enforces the closed status union and metrics key set", () => {
  assert.throws(() => toResult({ status: "ok" }), TypeError);
  assert.deepEqual(toResult({ status: "fail", message: "m", tokenSpend: 1, costUsd: 2 }), {
    status: "fail",
    message: "m",
    metrics: { token_spend: 1, cost_usd: 2 },
  });
});

test("a non-function handler is a protocol error", async () => {
  const h = await harness();
  assert.equal(await candybar(undefined, h.opts), null);
  assert.match(h.lines.join(""), /requires a function/);
});

test("default export is candybar", () => {
  assert.equal(candybarDefault, candybar);
});
