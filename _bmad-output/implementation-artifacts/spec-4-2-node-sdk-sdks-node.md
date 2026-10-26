---
title: 'Story 4.2: Node SDK (sdks/node)'
type: 'feature'
created: '2026-10-26'
status: 'in-progress'
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
- [ ] `sdks/node/package.json`, `package-lock.json`, `tsconfig.json` -- package `candybar-sdk`, `type: module`, `engines.node >=24`, `exports` with types, zero `dependencies`, devDep `typescript@5.9.3`; scripts `test` (node --test) and `typecheck` -- npm-installable, self-referenceable by name.
- [ ] `sdks/node/src/index.js`, `src/index.d.ts` -- `candybar`, `readContext`, `toResult`, `writeResult`, `ProtocolError`, `EXIT_*` -- the protocol, typed for TS authors.
- [ ] `sdks/node/test/*.test.js`, `test/types.ts` -- node:test over every matrix row; no-NATS guards (no deps, builtin allowlist, runtime net/tls/dns trap); typecheck fixture with `@ts-expect-error` pins.
- [ ] `sdks/node/examples/hello-node/{candybar.toml,candybar.mjs}` -- executable example importing `candybar-sdk`, reports token spend.
- [ ] `sdks/node/README.md` -- usage, protocol, decisions.
- [ ] `tests/test_node_sdk.py` -- runs the SDK through node and parses output with the daemon's real `ResultFile` + `resolve_outcome` (skip without node unless `CANDYBAR_REQUIRE_NODE=1`).
- [ ] `tests/test_node_sdk_integration.py` -- live: example manifest registered, tick -> real Dispatcher -> isolated command stream -> real Executor -> node example -> row `success` with `token_spend`, counters and metrics moved; a rejecting candybar lands `fail` with its message.
- [ ] `Dockerfile` -- copy the Node 24.22.1 binary into the runtime image; `ci.yml` -- setup-node, SDK test+typecheck, require node for pytest, integration file, image `node --version` smoke; `mise.toml` -- `node = "24"`; `.gitignore` -- `node_modules/`, `*.tgz`.

**Acceptance Criteria:**
- Given Node >= 24 and `sdks/node`, when an author wraps an async function with `candybar()` and registers it via `candybar.toml`, then the SDK reads the five env vars, awaits the function, and writes the AD-8 shape the daemon's `ResultFile` accepts.
- Given a rejected promise, then the result is `{status:"fail"}` with the message and the executor records `fail`.
- Given the package, then it has no runtime dependencies, imports no network-capable module, and a run under a net/tls/dns trap makes no connection.
- Given the example registered on the live stack, when its schedule matches a tick, then a `scheduled|success` row with the reported `token_spend` lands and `/metrics` counts it.

## Implementation Notes

## Spec Change Log

## Review Triage Log

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
