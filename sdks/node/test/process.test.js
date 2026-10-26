// Real-process tests: exit codes the executor sees, the shipped example, and
// the "never opens a NATS connection" guard (static + runtime).
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, readFile, readdir, writeFile } from "node:fs/promises";
import { existsSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const pkgRoot = join(here, "..");
const example = join(pkgRoot, "examples", "hello-node", "candybar.mjs");
const trap = join(here, "fixtures", "net-trap.mjs");

async function spawnCandybar(script, extraEnv = {}, args = []) {
  const dir = await mkdtemp(join(tmpdir(), "candybar-sdk-proc-"));
  const resultFile = join(dir, "result.json");
  const env = {
    PATH: process.env.PATH,
    CANDYBAR_INVOCATION_ID: "0193b2a0-0000-7000-8000-000000000002",
    CANDYBAR_ID: "0193b2a0-0000-7000-8000-0000000000bb",
    CANDYBAR_TRIGGER: "scheduled",
    CANDYBAR_TICK_ID: "tick:29883070",
    CANDYBAR_RESULT_FILE: resultFile,
    ...extraEnv,
  };
  for (const [k, v] of Object.entries(env)) if (v === undefined) delete env[k];
  const proc = spawnSync(process.execPath, [...args, script], { env, cwd: dirname(script), encoding: "utf8" });
  const result = existsSync(resultFile) ? JSON.parse(await readFile(resultFile, "utf8")) : null;
  return { proc, result, dir };
}

async function writeScript(body) {
  const dir = await mkdtemp(join(tmpdir(), "candybar-sdk-src-"));
  const file = join(dir, "job.mjs");
  const sdk = new URL("../src/index.js", import.meta.url).href;
  await writeFile(file, body.replaceAll("__SDK__", sdk));
  return file;
}

test("example: success exits 0 with the AD-8 shape", async () => {
  const { proc, result } = await spawnCandybar(example);
  assert.equal(proc.status, 0, proc.stderr);
  assert.deepEqual(result, {
    status: "success",
    message: "hello from node on tick:29883070 (scheduled)",
    metrics: { token_spend: 42 },
  });
});

test("example: manual trigger renders the null tick", async () => {
  const { proc, result } = await spawnCandybar(example, { CANDYBAR_TRIGGER: "manual", CANDYBAR_TICK_ID: "" });
  assert.equal(proc.status, 0, proc.stderr);
  assert.equal(result.message, "hello from node on a manual run (manual)");
});

test("rejected promise exits 1, writes fail, stack on stderr", async () => {
  const { proc, result } = await spawnCandybar(example, { HELLO_NODE_FAIL: "1", HELLO_NODE_TOKENS: "7" });
  assert.equal(proc.status, 1);
  assert.deepEqual(result, {
    status: "fail",
    message: "hello-node asked to fail (scheduled)",
    metrics: { token_spend: 7 },
  });
  assert.match(proc.stderr, /candybar-sdk: handler failed: hello-node asked to fail \(scheduled\)\n$/);
});

test("outside the executor exits 2 without calling the handler", async () => {
  const script = await writeScript(`
    import { candybar } from "__SDK__";
    await candybar(async () => { console.log("HANDLER RAN"); });
  `);
  const { proc, result, dir } = await spawnCandybar(script, { CANDYBAR_RESULT_FILE: undefined });
  assert.equal(proc.status, 2);
  assert.equal(result, null);
  assert.doesNotMatch(proc.stdout, /HANDLER RAN/);
  assert.match(proc.stderr, /missing CANDYBAR_RESULT_FILE/);
  assert.deepEqual(await readdir(dir), []);
});

test("unwritable result path exits 1 so the exit-code fallback marks it failed", async () => {
  const { proc } = await spawnCandybar(example, { CANDYBAR_RESULT_FILE: "/nonexistent-dir/result.json" });
  assert.equal(proc.status, 1);
  assert.match(proc.stderr, /could not write result file/);
});

test("open handles cannot keep the invocation alive past the exit grace", async () => {
  // Review finding: a leaked interval/pool/socket kept the process (and the
  // executor's invocation) alive forever after the result was written.
  for (const [body, code, status] of [
    [`setInterval(() => {}, 1000); return "done";`, 0, "success"],
    [`setInterval(() => {}, 1000); throw new Error("x");`, 1, "fail"],
  ]) {
    const script = await writeScript(`
      import { candybar } from "__SDK__";
      await candybar(async () => { ${body} });
    `);
    const t0 = Date.now();
    const { proc, result } = await spawnCandybar(script);
    const elapsed = Date.now() - t0;
    assert.equal(proc.status, code, proc.stderr);
    assert.equal(result.status, status);
    assert.ok(elapsed >= 1500 && elapsed < 10000, `exited after ${elapsed}ms`);
    assert.match(proc.stderr, /open handles kept the process alive 2000ms/);
  }
});

test("a clean handler exits immediately, without the grace notice", async () => {
  const t0 = Date.now();
  const { proc } = await spawnCandybar(example);
  assert.equal(proc.status, 0);
  assert.ok(Date.now() - t0 < 1500);
  assert.doesNotMatch(proc.stderr, /open handles/);
});

test("null options are treated as defaults, never a rejection", async () => {
  const script = await writeScript(`
    import { candybar } from "__SDK__";
    const res = await candybar(async () => "ok", null);
    console.log(JSON.stringify(res));
  `);
  const { proc, result } = await spawnCandybar(script);
  assert.equal(proc.status, 0, proc.stderr);
  assert.equal(result.message, "ok");
});

test("pending output flushes: the SDK sets exitCode instead of exiting", async () => {
  const script = await writeScript(`
    import { candybar } from "__SDK__";
    await candybar(async () => { process.stdout.write("x".repeat(200000)); return "ok"; });
  `);
  const { proc, result } = await spawnCandybar(script);
  assert.equal(proc.status, 0);
  assert.equal(proc.stdout.length, 200000);
  assert.equal(result.status, "success");
});

// --- no NATS: the SDK opens no connection and depends on nothing ----------------

test("package has zero runtime dependencies", () => {
  const pkg = JSON.parse(readFileSync(join(pkgRoot, "package.json"), "utf8"));
  for (const field of ["dependencies", "peerDependencies", "optionalDependencies", "bundleDependencies"]) {
    assert.deepEqual(pkg[field] ?? {}, {}, `${field} must be empty`);
  }
  const devDeps = Object.keys(pkg.devDependencies ?? {});
  assert.ok(devDeps.every((d) => !/nats/i.test(d)), "no nats package anywhere in the manifest");
});

test("SDK source imports only network-free node builtins", async () => {
  const ALLOWED = new Set(["node:fs/promises", "node:process"]);
  const srcDir = join(pkgRoot, "src");
  for (const name of await readdir(srcDir)) {
    if (!name.endsWith(".js")) continue;
    const text = await readFile(join(srcDir, name), "utf8");
    const specifiers = [
      ...text.matchAll(/\bfrom\s+["']([^"']+)["']/g),
      ...text.matchAll(/\bimport\s*\(\s*["']([^"']+)["']/g),
      ...text.matchAll(/\brequire\s*\(\s*["']([^"']+)["']/g),
      ...text.matchAll(/^\s*import\s+["']([^"']+)["']/gm),
    ].map((m) => m[1]);
    assert.ok(specifiers.length > 0, `${name}: import scan found nothing (scanner broken?)`);
    for (const spec of specifiers) {
      assert.ok(ALLOWED.has(spec), `${name} imports ${spec}, outside the network-free allowlist`);
    }
    assert.doesNotMatch(text, /\bnats\b/i, `${name} mentions nats`);
  }
});

test("import scanner catches a forbidden import (control)", async () => {
  // Sanity counter-test: the regexes above must see a network import.
  const sample = `import { connect } from "nats";\nconst n = await import("node:net");`;
  const found = [...sample.matchAll(/\bfrom\s+["']([^"']+)["']/g), ...sample.matchAll(/\bimport\s*\(\s*["']([^"']+)["']/g)].map((m) => m[1]);
  assert.deepEqual(found, ["nats", "node:net"]);
});

test("runtime: a full run under a net/tls/dns/fetch trap makes no connection", async () => {
  for (const extra of [{}, { HELLO_NODE_FAIL: "1" }]) {
    const { proc, result } = await spawnCandybar(example, extra, ["--import", trap]);
    assert.doesNotMatch(proc.stderr, /NETWORK TRAP/, proc.stderr);
    assert.ok(result !== null);
  }
});

test("runtime trap control: the trap does fire on a connection attempt", async () => {
  const script = await writeScript(`
    import net from "node:net";
    net.connect(4222, "localhost").on("error", () => {});
  `);
  const { proc } = await spawnCandybar(script, {}, ["--import", trap]);
  assert.match(proc.stderr, /NETWORK TRAP/);
  assert.notEqual(proc.status, 0);
});
