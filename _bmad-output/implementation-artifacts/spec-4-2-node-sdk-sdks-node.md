---
title: 'Story 4.2: Node SDK (sdks/node)'
type: 'feature'
created: '2026-10-26'
status: 'done'
baseline_commit: 'c56b127e7313095b2c1705da3d62a7fc6365fc7c'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-4-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** A candybar can only be a bare executable today; a JavaScript/TypeScript author has to hand-roll the AD-8 protocol (read five env vars, write the result JSON) and gets token-spend reporting and failure mapping wrong in ad-hoc ways.

**Approach:** Ship `sdks/node` as the zero-dependency npm package `candybar-sdk` (Node >= 24, ESM, hand-written `.d.ts`). `candybar(handler)` reads the five `CANDYBAR_*` vars into a typed context, awaits the handler, and atomically writes the exact AD-8 result JSON to `CANDYBAR_RESULT_FILE`, mapping a rejected promise to `{status:"fail"}`. An example node candybar with a `candybar.toml` proves the tick -> dispatcher -> executor -> SDK -> registry/metrics path end to end, and the daemon image gains a Node runtime so node candybars run in production.

## Boundaries & Constraints

**Always:** Output is byte-compatible with the executor's reader (`ResultFile` in `executor/runner.py`): `status` is `success|fail`; `message` only when set (capped 500 chars); `metrics` holds ONLY `token_spend` (non-negative safe integer, default 0) and `cost_usd` (finite >= 0, omitted when unreported) — that object is `extra="forbid"`. The exit code agrees with the status (0 success, 1 fail) because the executor treats exit code as primary. Handler token/cost spend is reported on failures too. The package has no runtime dependencies and its source imports only an allowlist of `node:` builtins with no network capability.

**Never:** No NATS/network client, no bus or registry access, no self-registration (registration stays `candybar register` + `candybar.toml`), no signal handlers (SIGTERM keeps its default kill semantics), no npm publish (deferred packaging per the spine), no executor/runner/dispatcher changes.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Success, no value | handler resolves `undefined` | `{"status":"success","metrics":{"token_spend":0}}`, exit 0 | N/A |
| Success with message/metrics | resolves `"done"` or `{message, tokenSpend, costUsd}`; plus `ctx.addTokens(n)` calls | message set; `token_spend` = accumulated + returned; `cost_usd` likewise | N/A |
| Explicit fail | resolves `{status:"fail", message}` | status fail with message, exit 1, metrics kept | N/A |
| Rejected promise / throw | rejects `Error("boom")` or a non-Error | `{status:"fail", message:"boom"}` (or `String(value)`), stack on stderr, exit 1, accumulated metrics kept | N/A |
| Invalid metrics | `tokenSpend: 1.5`, negative, NaN, `costUsd: -1` | fail with `invalid result: ...` message, exit 1, token_spend from valid accumulation only | `ctx.addTokens(bad)` throws TypeError -> handled as rejection |
| Manual trigger | `CANDYBAR_TICK_ID=""` | `ctx.tickId === null`, `ctx.trigger === "manual"` | N/A |
| Outside the executor | any of INVOCATION_ID / ID / TRIGGER / RESULT_FILE missing or empty | handler NOT called, error on stderr, exit 2, resolves `null` | exit-code fallback marks it failed |
| Result file unwritable | write/rename throws | error on stderr, exit 1 | exit-code fallback marks it failed |

</frozen-after-approval>

## Code Map

- `src/candybar/executor/runner.py` -- `child_env` (the five vars; TICK_ID empty for manual), `ResultFile` (reader contract), `run_invocation` execs `entrypoint` directly with cwd = its dir. Do not change.
- `src/candybar/executor/worker.py` -- `resolve_outcome`: exit-code primary; result-file message wins on nonzero exit; metrics kept on fail. Do not change; tests import it to prove compatibility.
- `src/candybar/events/envelope.py` -- `InvocationMetrics` (`extra="forbid"`, `token_spend` int >= 0, `cost_usd` >= 0 | None).
- `src/candybar/registry/manifest.py` -- `load_manifest` resolves a relative entrypoint against the toml's dir; reuse for the example.
- `src/candybar/dispatcher/worker.py` -- `Dispatcher(schedules, publish)`; `tests/test_executor_integration.py` -- isolated-stream + throwaway-row pattern to copy.
- `Dockerfile`, `.github/workflows/ci.yml`, `mise.toml`, `.gitignore` -- shared files; minimal additive edits only (concurrent agents).

## Tasks & Acceptance

**Execution:**
- [x] `sdks/node/package.json`, `package-lock.json`, `tsconfig.json` -- package `candybar-sdk`, `type: module`, `engines.node >=24`, `exports` with types, zero `dependencies`, devDep `typescript@5.9.3`; scripts `test` (node --test) and `typecheck` -- npm-installable, self-referenceable by name.
- [x] `sdks/node/src/index.js`, `src/index.d.ts` -- `candybar`, `readContext`, `toResult`, `writeResult`, `ProtocolError`, `EXIT_*` -- the protocol, typed for TS authors.
- [x] `sdks/node/test/*.test.js`, `test/types.ts` -- node:test over every matrix row; no-NATS guards (no deps, builtin allowlist, runtime net/tls/dns trap); typecheck fixture with `@ts-expect-error` pins.
- [x] `sdks/node/examples/hello-node/{candybar.toml,candybar.mjs}` -- executable example importing `candybar-sdk`, reports token spend.
- [x] `sdks/node/README.md` -- usage, protocol, decisions.
- [x] `tests/test_node_sdk.py` -- runs the SDK through node and parses output with the daemon's real `ResultFile` + `resolve_outcome` (skip without node unless `CANDYBAR_REQUIRE_NODE=1`).
- [x] `tests/test_node_sdk_integration.py` -- live: example manifest registered, tick -> real Dispatcher -> isolated command stream -> real Executor -> node example -> row `success` with `token_spend`, counters and metrics moved; a rejecting candybar lands `fail` with its message.
- [x] `Dockerfile` -- copy the Node 24.22.1 binary into the runtime image; `ci.yml` -- setup-node, SDK test+typecheck, require node for pytest, integration file, image `node --version` smoke; `mise.toml` -- `node = "24"`; `.gitignore` -- `node_modules/`, `*.tgz`.

**Acceptance Criteria:**
- Given Node >= 24 and `sdks/node`, when an author wraps an async function with `candybar()` and registers it via `candybar.toml`, then the SDK reads the five env vars, awaits the function, and writes the AD-8 shape the daemon's `ResultFile` accepts.
- Given a rejected promise, then the result is `{status:"fail"}` with the message and the executor records `fail`.
- Given the package, then it has no runtime dependencies, imports no network-capable module, and a run under a net/tls/dns trap makes no connection.
- Given the example registered on the live stack, when its schedule matches a tick, then a `scheduled|success` row with the reported `token_spend` lands and `/metrics` counts it.

## Implementation Notes

- Implemented directly (this session has no subagent-spawn capability — same deviation as stories 1.1–1.9). Commits on origin/main: af98335 (SDK, example, tests, image, CI), 76f0e73 (review patches), d64338b + 8a1f2af (CI fix-ups, see below).
- Public surface: `candybar(handler, options?)` (also the default export), `readContext`, `toResult`, `writeResult`, `ProtocolError`, `EXIT_SUCCESS|EXIT_FAIL|EXIT_PROTOCOL`, `MESSAGE_MAX`, `ENV`. Handler returns `undefined | string | {status?, message?, tokenSpend?, costUsd?}`; `ctx.addTokens/addCost` accumulate and returned spend is added on top.
- Found while implementing: the executor keeps only the stderr TAIL (500 chars), so a stack trace pushed the error headline out. The SDK now writes the stack first and the one-line `candybar-sdk: handler failed: <msg>` last.
- Review patch (outside the frozen block, recorded here): the SDK still sets `process.exitCode` instead of calling `process.exit` in the normal path. It also arms an unref'd 2s timer that force-exits with the verdict's code when a leaked handle keeps the loop alive. Without that timer the invocation never terminates.
- Decisions where the spine is silent: zero runtime deps + hand-written `.d.ts` (no build step, no generated output tracked); devDep `typescript@5.9.3` (pinned, older than 2 weeks) only for `tsc --noEmit`; the trigger value passes through unvalidated (forward-compatible); a missing `CANDYBAR_TICK_ID` is tolerated as `null`; token cap is `Number.MAX_SAFE_INTEGER` (below the registry BIGINT); message cap is 500 CODE POINTS after `toWellFormed()`; Docker image gets only the node 24.22.1 binary (no npm), and jobs install the SDK from an `npm pack` tarball (a directory install symlinks to a host path the container cannot see); `mise.toml` pins `node = "24"`.
- Cross-SDK divergences from 4.1 (Python), all equivalent at the executor: the failure message is `err.message` here vs `Type: msg` in Python (the frozen matrix fixes `"boom"`); an unwritable result file exits 1 here vs 2 in Python; on a protocol error with `CANDYBAR_RESULT_FILE` set, Python writes a fail doc and Node writes nothing (exit 2 either way). Logged in deferred-work.md for a shared conformance fixture.
- Shared-checkout hazard: this repo's working tree AND index are shared with concurrent agents (2.1, 3.1, 4.1, 4.3). Commits were built with a private `GIT_INDEX_FILE` and `git commit-tree`, so other agents' staged and unstaged hunks were never committed. Once, a concurrent commit swept my uncommitted in-image CI hunk, and my review commit then duplicated it. My dedupe commit (d64338b) in turn swept story 4.3's uncommitted Rust CI steps, which reference files not yet on main. 8a1f2af restored the workflow to my changes only; the 4.3 hunks stay in the working tree for its own commit.
- Verification: 39/39 node:test, tsc clean, `npm ls --omit=dev` = package only; full pytest 368 passed / 23 skipped (CANDYBAR_REQUIRE_NODE=1), ruff clean, all on a clean origin/main worktree plus this story's files, because the shared tree carries other stories' WIP. Live integration (`test_node_sdk_integration.py` + executor suite) passed on throwaway postgres:17-alpine + nats:2.10.29. A run in the shared tree failed one metrics assertion. The cause was story 3.1's then-uncommitted `worker.py`, which passed `memory_peak_bytes` to the old `metrics.observe`; the same assertion passed on the clean tree.
- Live smoke on https://candybar.delo.sh: rebuilt the image from af98335 (node v24.22.1 inside) and registered `~/candybar/jobs/hello-node` (id 01a176d4-e9a3-726d-b63c-57c5a4a24a34, `*/5`, noise). `candybar run` produced `manual|success|0|token_spend 42`. The scheduled fire on tick:29883255 produced `scheduled|success|0|token_spend 42`, with counters at 2/2 and `/metrics` showing success=1 on the container that story 3.1 had redeployed by then (built from main, so it includes node). The job is left running as the continuous-operation proof, with the patched SDK tarball installed.

## Spec Change Log

## Review Triage Log

Three layers (blind-hunter, edge-case-hunter, verification-gap) ran inline because no subagent spawn is available (the same deviation as stories 1.1–1.9). Diff: 61 kB, so the blind-hunter floor is N = min(floor(sqrt(61)+1), 10) = 8.

| # | Layer | Finding | Verdict | Evidence / route |
|---|-------|---------|---------|------------------|
| 1 | blind | `capMessage` sliced UTF-16 units, so an emoji at char 500 left a lone surrogate | high | Reproduced: psycopg raises UnicodeEncodeError on `'\ud83d'`, so the terminal write fails, the executor naks, and the invocation re-executes forever. **Patched**: `toWellFormed()` plus a code-point cap; node and pytest pins added. |
| 2 | blind | A leaked handle (interval, pool, keep-alive socket) kept the process alive after the result was written | medium | Reproduced: a `setInterval` job was still running at 8s under `timeout` (exit 124). The executor has no invocation timeout, so the run never ends. **Patched**: an unref'd 2s grace timer calls `process.exit(code)`, armed only in process mode. Tests cover both verdicts and the clean fast path. |
| 3 | blind | `candybar(h, null)` rejected on `options.env`, which contradicts "never rejects" | low | A direct correction (`options ?? {}`). **Patched** + subprocess test. |
| 4 | blind | The failure-message format differs from the Python SDK (`boom` vs `Error: boom`) | low | Both carry the message and no consumer parses it. The frozen matrix fixes `"boom"`. Rejected; harmonization logged in deferred-work.md. |
| 5 | blind | An array return resolves as success | low | Unlikely in practice, and the fix adds a branch. Rejected. |
| 6 | blind | The exported `writeResult` does not validate its shape | low | Documented as a low-level primitive. A bad file maps to `failed` at the executor, never to a loop. Rejected. |
| 7 | blind | The repo-wide `*.tgz` ignore is too broad | false | Intentional for `npm pack` output; `git ls-files '*.tgz'` is empty. |
| 8 | blind | Image ships no npm; native addons need a matching build | low | Documented in the README (jobs bring their own `node_modules`). Not a defect. Rejected. |
| 9 | edge | `addCost` checked only the increment, so an Infinity sum made `toResult` throw inside the failure path; `candybar()` rejected and wrote no file | medium | Reproduced by test (`1e308` twice). **Patched**: the accumulated sum is checked as tokens already were. |
| 10 | edge | The handler calls `process.exit(0)` before a result is written | low | The executor applies the bare-executable fallback (exit 0, no file, success); intercepting it would need monkeypatching. Rejected. |
| 11 | edge | SIGTERM mid-handler | false | Default kill gives returncode -15, and executor `resolve_outcome(stopped=True)` marks it `failed`. Signal handlers are excluded by the spec. |
| 12 | edge | Two `candybar()` calls in one process overwrite the file | low | Not a supported shape. Rejected. |
| 13 | edge | A throwing `err.message` getter inside the catch | low | Exotic; it surfaces as a nonzero exit, which the executor marks failed. Rejected. |
| 14 | claims | "Writes go to `<file>.tmp`" | false | The temp file is `<file>.<pid>.tmp` with rename: the same claim, still atomic. |
| 15 | verif | The Dockerfile node runtime was only checked with `node --version`; no SDK candybar ran in the image in CI | medium | **Patched**: the CI image step runs the example inside `candybar:ci` and greps for success. |
| 16 | verif | `test_node_sdk.py` skips without node | false | CI sets `CANDYBAR_REQUIRE_NODE=1`, so a missing node is a collection error. |
| 17 | verif | `@ts-expect-error` pins may be vacuous | false | tsc fails an unused directive (TS2578); typecheck is clean. |
| 18 | blind (cross-check vs 4.1's deferred NUL finding) | A NUL byte in a failure message reaches the Postgres text column, which rejects 0x00; the same nak and re-execute loop as #1 follows | high | Same root path as #1 (Postgres text rejects it; 4.1 deferred the executor-side half). **Patched**: `capMessage` strips U+0000, with a node pin. |

## Design Notes

Author surface (the example is the golden shape):

```js
#!/usr/bin/env node
import { candybar } from "candybar-sdk";
await candybar(async (ctx) => {
  ctx.addTokens(42); // accumulates; returned tokenSpend is added on top
  return { message: `hello from ${ctx.trigger}` };
});
```

`candybar()` never rejects; it resolves the written result (or `null` on a protocol error) and sets `process.exitCode` rather than calling `process.exit`, so pending stdout/stderr flush. Writes go to `<file>.tmp` then `rename`, so the executor never reads a torn file.

## Verification

**Commands:**
- `cd sdks/node && npm ci && npm test && npm run typecheck` -- expected: all pass, tsc clean
- `CANDYBAR_REQUIRE_NODE=1 uv run pytest -q` -- expected: green incl. tests/test_node_sdk.py
- `CANDYBAR_TEST_NATS=1 CANDYBAR_TEST_PG=1 uv run pytest tests/test_node_sdk_integration.py -q` (throwaway NATS + Postgres) -- expected: pass
- `uv run ruff check .` -- expected: clean
- `docker build -t candybar:ci . && docker run --rm --entrypoint node candybar:ci --version` -- expected: v24.22.1
