# candybar

Event-driven cron framework for the 33GOD pipeline. One clock — the heartbeat
publisher — emits minute ticks onto Bloodbank (`bloodbank.cron.tick.fired`);
every downstream behavior (schedule evaluation, dispatch, execution,
accounting, live-view updates) is a consumer of bus facts and commands. No
crontab, no private timers, no scheduler libraries that own clocks.

Roles live in one Python asyncio deployable (heartbeat, dispatcher, executor,
api), selected by CLI role flag; the Postgres registry is the sole inventory
and the daemon its only writer.

## Layout

```text
src/candybar/        # the one deployable (roles: heartbeat, dispatcher, executor, api, cli, events, registry)
sdks/python/         # candybar-sdk (pip)     — Story 4.1 (done)
sdks/node/           # candybar-sdk (npm)     — Story 4.2
sdks/rust/           # candybar-sdk (crate)   — Story 4.3 (done)
deploy/              # container + Traefik labels, alembic env — Story 1.9
schemas/             # draft JSON schemas to PR into bloodbank/schemas/ — Story 1.2
```

## Develop

```bash
mise install          # toolchain (python)
uv sync               # install the pinned stack
uv run pytest -q      # tests
uv run ruff check .   # lint
uv run candybar --help
```

CI runs ruff + pytest on GitHub-hosted runners on every push to `main`,
plus the heartbeat JetStream integration tests against a pinned nats-server,
the registry integration tests against a pinned postgres:17-alpine, and the
documented PromQL under promtool (pinned prom/prometheus:v3.5.0).

## Registry (Postgres, AD-10)

The registry lives in the `candybar` database on the shared homelab Postgres;
the daemon is its sole writer and all DB access is in `src/candybar/registry/`
(enforced by a guard test). Migrations are alembic, run from the repo root:

```bash
export DATABASE_URL=postgresql://candybar:<password>@localhost:5432/candybar
uv run alembic -c deploy/alembic.ini upgrade head
```

The role password lives in 1Password (`op://DeLoSecrets/Candybar Postgres Role/password`).
Live-PG tests: `CANDYBAR_TEST_PG=1 DATABASE_URL=... uv run pytest tests/test_registry_integration.py`.

## Register a candybar

A candybar is a directory with a `candybar.toml` manifest (AD-8):

```toml
[candybar]
name = "my-job"            # unique lowercase slug
description = "what it does"
tags = ["team-x"]
schedule = "*/5 * * * *"   # standard 5-field cron, evaluated in UTC
entrypoint = "./run.sh"    # resolved against the manifest dir; must exist
noise = false              # optional
```

```bash
uv run candybar daemon --roles api   # serves POST /candybars on 127.0.0.1:8700
uv run candybar register ./my-job    # -> 201 + row + bloodbank.cron.candybar.created
```

Invalid cron/name → `application/problem+json` (422); duplicate name → 409;
bus down → 503 and no row. API bind: `CANDYBAR_API_HOST`/`CANDYBAR_API_PORT`;
CLI target: `CANDYBAR_API_URL`.

## Definitions API (CAP-8, Story 5.1)

The shapes are `_bmad-output/specs/spec-cron-framework/holocene-view-contract.md`,
field for field (`tests/contract_shapes.py` parses them out of that file):

```bash
curl https://candybar.delo.sh/candybars          # [{id, name, description, tags, schedule, noise, createdAt}, ...] oldest first
curl https://candybar.delo.sh/candybars/<id>     # the same + {totalInvoked, totalSuccessful}
curl -X POST https://candybar.delo.sh/candybars -H 'content-type: application/json' \
  -d '{"name":"my-job","schedule":"*/5 * * * *","entrypoint":"/abs/run.sh"}'   # 201, single shape
curl -X PATCH https://candybar.delo.sh/candybars/<id> -H 'content-type: application/json' \
  -d '{"schedule":"0 * * * *"}'                 # 200, single shape
```

`PATCH` takes any non-empty subset of `name`, `description`, `tags`,
`schedule`, `entrypoint`, `noise`. The merged definition is validated like a
manifest. If anything actually changed, one `bloodbank.cron.candybar.updated`
fact goes out first, with `changes` = only the changed fields, and then the
row is written. Sending the stored values back is a 200 with no fact. Unknown
or read-only keys and `null` → 422; unknown id → 404; name taken → 409; bus
down → 503 with the row unchanged. `createdAt` is always UTC. The executor's
`entrypoint` is accepted on writes but never echoed.

## Run a candybar now

```bash
uv run candybar run <id>   # POST /candybars/<id>/invoke -> 202
```

The api role publishes one `bloodbank.cmd.cron.candybar.invoke` command with
`trigger=manual` and `idempotency_key = candybar:<id>:manual:<command_id>`
(AD-12); the executor runs it exactly like a scheduled fire, so the
invocation row, registry counters, metrics, and `invoked`/`completed`/`failed`
facts all carry `trigger=manual`, and the child sees `CANDYBAR_TRIGGER=manual`
(with an empty `CANDYBAR_TICK_ID`). Every run is a new invocation. The CLI
returns once the command is accepted, not when the job finishes. Unknown id → 404,
malformed id → 422, bus down → 503: each prints the problem+json body and
exits 1. Needs the `api` and `executor` roles running.

## Write a candybar in Python (SDK)

`sdks/python` is the pip package `candybar-sdk` (import `candybar_sdk`, zero
dependencies, no NATS). Decorate a function with `@candybar`, call
`main.run()`, report usage with `ctx.add_tokens(...)`, and register the file
with a normal `candybar.toml`; the SDK writes the AD-8 result file, and an
unhandled exception becomes `{status: "fail"}` with its message. The image
ships the SDK, so jobs under the jobs mount can import it. See
[`sdks/python/README.md`](sdks/python/README.md) and the
[`hello` example](sdks/python/examples/hello) (`candybar register sdks/python/examples/hello`).

## Write a candybar in Rust (SDK)

`sdks/rust` is the crate `candybar-sdk` (zero dependencies, no NATS, Rust >=
1.99.1). `fn main() -> ExitCode { candybar_sdk::run(job) }` runs your handler,
which reports usage with `ctx.add_tokens(...)`/`ctx.add_cost(...)` and returns
`()`, a message, an `Outcome`, or a `Result`. The SDK writes the AD-8 result
file; an `Err` or a panic becomes `{status: "fail"}` with its message. Build
the binary and register it with a normal `candybar.toml` whose `entrypoint`
points at it. See [`sdks/rust/README.md`](sdks/rust/README.md) and the
[`hello` example](sdks/rust/examples/hello) (`cargo build --release
--manifest-path sdks/rust/examples/hello/Cargo.toml`, then
`candybar register sdks/rust/examples/hello`).

## Run the daemon

```bash
export NATS_URL=nats://localhost:4222   # dev default; the deploy wires the real value
uv run candybar daemon                  # all implemented roles
uv run candybar daemon --roles heartbeat
```

The heartbeat role publishes one `bloodbank.cron.tick.fired` fact per UTC
minute boundary (`tick_id = tick:<unix_epoch_minute>`, JetStream `Nats-Msg-Id`
= event id for dedupe). It is the only component allowed to own a clock (AD-1).
Live-bus tests: `CANDYBAR_TEST_NATS=1 uv run pytest tests/test_heartbeat_integration.py`
(add `CANDYBAR_TICK_E2E=1` for the literal 5-minute subscriber window).

**Misfires (AD-6).** The dispatcher keeps its last fully dispatched tick
minute in the registry (`dispatch_cursor`). When the next tick it consumes
lands more than one minute past that cursor, it coalesces the gap. This
happens after daemon downtime, a bus outage or a dropped tick. For each
candybar:

- Only the latest missed fire runs, with `trigger=misfire` and the missed
  minute's `tick_id`. Its idempotency key matches the scheduled one, so a fire
  time runs at most once whatever its trigger.
- Each earlier missed fire inside the 24h window before the tick is recorded
  as `bloodbank.cron.candybar.skipped` with `reason=misfire_coalesced`.
- Fires from before a candybar was registered are never counted as missed.

If only the dispatcher role was down while the heartbeat kept publishing,
the queued ticks are acked without dispatch, and the newest one coalesces the
whole backlog. A fresh install, which has no cursor yet, never misfires.

**Skipping one occurrence (AD-13).** A row in the registry `skips` table
with `kind=occurrence` and a UTC `fire_time` suppresses that one fire of that
candybar. The dispatcher reads skips on every tick, before it emits any
command. A skipped fire gets no invoke command and no `invocations` row.
Instead it is recorded as `bloodbank.cron.candybar.skipped` with
`reason=occurrence`. Later fires dispatch normally.

- Inside a misfire gap, a skipped fire is never the one that runs; the latest
  missed fire that is not skipped runs instead.
- Skips only affect scheduled fires. `candybar run` is unaffected, and so is a
  fire whose tick was already dispatched before the skip was stored.
- The shared helpers in `dispatcher/schedule.py` (`next_fire`,
  `fires_between`, `previous_fire`) omit skipped fires, so the dispatcher
  and the Epic 5 schedule API compute the same fire times.

Write a skip through `registry.store.add_occurrence_skip`. It is idempotent
and rejects an unknown candybar. The HTTP endpoint lands with Story 5.6.

**Skipping the rest of a day (AD-13).** A `skips` row with
`kind=rest_of_day` and a UTC `day` suppresses every fire of that candybar on
that UTC date — no invoke commands and no `invocations` rows until the next
UTC midnight, then the schedule dispatches normally again. Each suppressed
fire is recorded as `bloodbank.cron.candybar.skipped` with
`reason=rest_of_day` at the tick it would have fired (a gap that spans a
skipped day records its fires when the gap closes). The same gap rules as
occurrence skips apply: a day-skipped latest missed fire hands the misfire
run to the latest fire that is not skipped, and the shared helpers omit the
whole date. Write one through `registry.store.add_rest_of_day_skip`; same
idempotence and validation contract as `add_occurrence_skip`.

## Metrics (Prometheus, AD-9)

The api role serves `GET /metrics` (same port as the API) for everything the
executor records in the same process. Series are labeled `{candybar_id, result}`
only (`result` is `success` or `fail`). Each invocation's id is attached as an
`invocation_id` **exemplar**, never as a label, so cardinality stays at two
series per candybar.

| metric | type | unit |
|---|---|---|
| `candybar_invocations_total` | counter | invocations |
| `candybar_invocation_runtime_milliseconds` | histogram (10ms–1h buckets) | wall ms |
| `candybar_invocation_memory_peak_bytes` | gauge (latest invocation's peak) | bytes |
| `candybar_invocation_memory_bytes` | histogram (1 MiB–32 GiB buckets) | bytes |
| `candybar_invocation_cpu_load_ratio` | histogram (0.01–32 buckets) | busy cores |
| `candybar_invocation_gpu_load_ratio` | histogram (0.01–8 buckets) | busy GPUs |
| `candybar_token_spend_total` | counter | self-reported LLM tokens (input + output) |

Memory and CPU are measured by the executor from each invocation's cgroup v2
scope (Story 3.1): memory is `memory.peak`, and CPU load is the `cpu.stat`
`usage_usec` delta divided by wall runtime, so 1.0 means one core was busy the
whole run. An unscoped run, or a host without the memory controller delegated,
reports 0. The same values are stored on the `invocations` row
(`memory_peak_bytes`, `cpu_load`) and carried in the `completed`/`failed` event
`data.metrics`.

GPU load (Story 3.2) comes from `nvidia-smi pmon`:
- While at least one invocation runs, one shared `pmon -d 1 -s u` process samples per-PID SM utilization. It stops when the last invocation finishes.
- A PID counts toward an invocation when its session id is the child's pid (every child starts its own session) or when it is in the invocation's cgroup scope.
- `gpu_load` is the per-sample sum of the member PIDs' SM% divided by 100, averaged over the samples taken during the run (1.0 = one GPU saturated).
- A host without `nvidia-smi` reads 0 silently. A failing `nvidia-smi` (no driver, unsupported device) reads 0 and logs one warning. `CANDYBAR_NVIDIA_SMI=""` turns GPU metering off, or names another binary.
- The value is stored in the row's `gpu_load` column and in `data.metrics.gpu_load`.

The container sees the GPU and the host PIDs that pmon reports through `gpus: all` and `pid: host` in `deploy/compose.yml`. `scripts/gpu-burn-candybar.py` is a dependency-free CUDA load job (driver API through ctypes) for proving GPU attribution end to end.

Token spend (Story 3.3) is self-reported: the result file's
`metrics.token_spend` (the SDKs' `ctx.add_tokens(...)`). The counter rises by
that amount once per execution, on the invocation's own `result` series, so
tokens a failed run reports still count, on `result="fail"`. Bare
executables, non-LLM jobs and unparsable result files add 0. `cost_usd` is
stored and returned by the API, but it has no Prometheus series.

Every observation carries the `invocation_id` exemplar on every family,
including a token increment of 0. OpenMetrics allows exemplars only on
counters and histogram buckets. The memory gauge therefore carries no
exemplar; its histogram twin does. The CPU
and GPU load families are histograms for the same reason: they give the same
`_sum`/`_count` a summary would, and can also carry the exemplar.

Exemplars are only emitted in the OpenMetrics format, which the endpoint
negotiates from the `Accept` header. Prometheus 3.x asks for it by default, but
it stores exemplars only with `--enable-feature=exemplar-storage`.

```promql
# success vs fail rate per candybar
sum by (candybar_id, result) (rate(candybar_invocations_total[5m]))
# failure ratio per candybar (0, not "no data", for a candybar that never failed)
sum by (candybar_id) (rate(candybar_invocations_total{result="fail"}[5m]))
  / sum by (candybar_id) (rate(candybar_invocations_total[5m]))
# p95 runtime per candybar (ms)
histogram_quantile(0.95, sum by (candybar_id, le) (rate(candybar_invocation_runtime_milliseconds_bucket[5m])))
# memory hogs: worst peak per candybar over a day (bytes)
topk(5, max by (candybar_id) (max_over_time(candybar_invocation_memory_peak_bytes[1d])))
# CPU burners: average busy cores per candybar
sum by (candybar_id) (rate(candybar_invocation_cpu_load_ratio_sum[1h]))
  / sum by (candybar_id) (rate(candybar_invocation_cpu_load_ratio_count[1h]))
# GPU burners: average busy GPUs per candybar
sum by (candybar_id) (rate(candybar_invocation_gpu_load_ratio_sum[1h]))
  / sum by (candybar_id) (rate(candybar_invocation_gpu_load_ratio_count[1h]))
# token burn: tokens per hour per candybar, and the top 5 burners over a day
sum by (candybar_id) (increase(candybar_token_spend_total[1h]))
topk(5, sum by (candybar_id) (increase(candybar_token_spend_total[1d])))
```

These queries are unit-tested by promtool in CI
(`deploy/prometheus/candybar-queries.test.yml`).

**From an exemplar to the full record.** An exemplar gives you an
`invocation_id`. The api role returns the stored `invocations` row for it:

```bash
curl https://candybar.delo.sh/invocations/<invocation_id>
curl 'https://candybar.delo.sh/candybars/<candybar_id>/invocations?limit=20'   # newest first, 1..500, default 50
```

Each record has `runtimeMs`, `memoryPeakBytes`, `cpuLoad`, `gpuLoad`,
`tokenSpend` and `costUsd` (null when not reported). It also has the
identity, trigger and outcome fields: `id`, `candybarId`, `trigger`,
`tickId`, `idempotencyKey`, `result`, `exitCode`, `errorMessage`,
`stderrTail`, `startedAt` and `finishedAt`. A row is written only when the
invocation finishes, so an invocation still running returns 404, as does an
unknown id. An unknown candybar returns 404, and a malformed id or a `limit`
out of range returns 422, all as problem+json.

## Deploy (AD-15)

One container composes all roles behind Traefik at
`https://candybar.delo.sh`, scraped by the homelab Prometheus, backed by the
host Postgres (`candybar` database) and Bloodbank NATS — zero new
infrastructure.

```bash
# 1. Resolve secrets (DATABASE_URL) from 1Password into the gitignored .env
~/docker/stacks/_lib/op-inject.sh deploy/.env.template deploy/.env

# 2. Build + run
docker compose -f deploy/compose.yml up -d --build

# 3. Smoke
curl -s https://candybar.delo.sh/healthz          # {"status":"ok"}
curl -s https://candybar.delo.sh/metrics | head   # Prometheus series
```

- The entrypoint applies `alembic upgrade head` before exec'ing the daemon
  (the daemon is the registry's sole writer, so boot-time migration cannot
  race).
- Registered entrypoints execute **inside** the container; put job scripts in
  `~/candybar/jobs` (bind-mounted at the same absolute path in the container,
  override with `CANDYBAR_JOBS_DIR`) and register them by that path.
- Each invocation runs in its own cgroup v2 scope inside the container, which
  is what memory/CPU metering reads. The container runs `privileged` with a
  private cgroup namespace. As root, the entrypoint moves itself into
  `/sys/fs/cgroup/daemon`, delegates `+memory +cpu` to
  `/sys/fs/cgroup/candybar`, and then drops to uid 10001 with `setpriv`.
  Without a writable cgroupfs it logs the fact and runs invocations unscoped,
  with memory and CPU reported as 0. `CANDYBAR_CGROUP_ROOT=""` turns scoping off.
- `docker stop` completes in ~1s when idle: both pull-consumer roles
  unsubscribe before draining. Restart/rejoin duplicates nothing —
  idempotency keys plus durable consumers make redelivery a republish from
  the registry row, never a re-execution.
- Secrets are `op://DeLoSecrets/...` references in `deploy/.env.template`;
  rotation = vault edit + re-inject + `up -d`. `scripts/check-deploy-hygiene.sh`
  (CI + `pytest`) fails any LAN IP or plaintext-secret shape on a deploy
  surface.
- DNS needs nothing: `candybar.delo.sh` is single-level, covered by the
  wildcard CNAME through the Cloudflare tunnel.

