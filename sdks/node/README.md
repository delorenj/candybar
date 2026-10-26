# candybar-sdk (Node)

Author a [Candybar](../../README.md) cron job natively in JavaScript or
TypeScript. Node >= 24 LTS, ESM, **zero runtime dependencies**, typed.

The SDK implements the AD-8 process protocol, the one contract shared by the
Python, Node and Rust SDKs: the executor spawns your candybar as a subprocess,
the invocation context arrives in five `CANDYBAR_*` env vars, and the result
goes back as JSON at `CANDYBAR_RESULT_FILE`. The SDK never opens a network
connection. The executor owns execution, metering, the registry, metrics and
Bloodbank events.

## Write one

```js
#!/usr/bin/env node
import { candybar } from "candybar-sdk";

await candybar(async (ctx) => {
  const reply = await callSomeModel(); // your work
  ctx.addTokens(reply.usage.total_tokens); // input + output tokens
  ctx.addCost(reply.costUsd);              // optional
  return `summarized ${reply.items} items`;
});
```

`candybar(handler)` reads the context, awaits the handler, writes the result
file atomically and sets `process.exitCode`. It never rejects. The handler can
return:

| Handler outcome | Result written | Exit code |
| --- | --- | --- |
| resolves `undefined` | `{"status":"success","metrics":{"token_spend":N}}` | 0 |
| resolves a string | success with that `message` | 0 |
| resolves `{ message?, tokenSpend?, costUsd? }` | success; this spend is added to anything reported with `ctx.addTokens`/`ctx.addCost` | 0 |
| resolves `{ status: "fail", message }` | fail, metrics kept | 1 |
| rejects / throws | `{"status":"fail","message":<error.message>}`, metrics kept, stack on stderr | 1 |
| invalid spend (`1.5`, `-1`, `NaN`, ...) | fail, `invalid result: ...` | 1 |
| not started by the executor (env vars missing) | nothing written; handler never called | 2 |

The context (`ctx`) is frozen and holds `invocationId`, `candybarId`,
`trigger` (`scheduled` | `manual` | `misfire`), `tickId` (`tick:<epoch_minute>`,
or `null` for manual runs) and `resultFile`.

Token and cost spend are reported **on failures too**, because a failed LLM
call still spent tokens. `token_spend` is a non-negative integer and
`cost_usd` is a finite non-negative number; the executor rejects anything else.

## Register it

The executor runs the registered `entrypoint` path directly, with no arguments
and with its directory as cwd. The entrypoint must therefore be an executable
file with a node shebang (`chmod +x`). Registration metadata lives in a
`candybar.toml` next to it; see [`examples/hello-node`](examples/hello-node):

```toml
[candybar]
name = "hello-node"
schedule = "*/5 * * * *"   # standard 5-field cron, evaluated in UTC
entrypoint = "candybar.mjs"
```

```sh
candybar register path/to/job-dir
candybar run <id>            # fire it now, out of schedule
```

## Install

This package is not on the npm registry yet (publishing is deferred). Install
it from this repo:

```sh
(cd sdks/node && npm pack)                       # -> candybar-sdk-0.1.0.tgz
cd ~/candybar/jobs/my-job && npm install /path/to/candybar-sdk-0.1.0.tgz
```

Install from the packed tarball, not the directory, for jobs that run in the
deployed container. A directory install symlinks to a host path that the
container cannot see. In production the daemon image ships a Node 24.22.1
runtime (no npm), and jobs bring their own `node_modules`.

TypeScript authors get the bundled `.d.ts` (`CandybarContext`,
`HandlerOutcome`, `ResultJson`, and so on). Compile to an `.mjs` entrypoint, or
point a shebang at a runner of your choice.

## Develop

```sh
npm ci
npm test          # node:test: protocol matrix, exit codes, no-network guards
npm run typecheck # tsc over src/index.d.ts + test/types.ts
```

From the repo root, `uv run pytest tests/test_node_sdk.py` runs the SDK through
the daemon's real executor spawn path and verdict logic, and
`tests/test_node_sdk_integration.py` (live NATS + Postgres) proves a scheduled
fire end to end.
