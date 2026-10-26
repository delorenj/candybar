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
sdks/python/         # candybar-sdk (pip)     — Story 4.1
sdks/node/           # candybar-sdk (npm)     — Story 4.2
sdks/rust/           # candybar-sdk (crate)   — Story 4.3
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
```

These queries are unit-tested by promtool in CI
(`deploy/prometheus/candybar-queries.test.yml`).

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
- cgroup scoping is disabled in the container (`CANDYBAR_CGROUP_ROOT=""`) —
  the container itself is the isolation boundary.
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

