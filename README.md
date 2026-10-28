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
schedule = "*/5 * * * *"   # standard 5-field cron, read as wall time in `timezone`
timezone = "UTC"           # optional IANA zone, e.g. "America/New_York" (default "UTC")
entrypoint = "./run.sh"    # resolved against the manifest dir; must exist
noise = false              # optional
```

```bash
uv run candybar daemon --roles api   # serves POST /candybars on 127.0.0.1:8700
uv run candybar register ./my-job    # -> 201 + row + bloodbank.cron.candybar.created
```

Invalid cron/name/timezone → `application/problem+json` (422); duplicate name → 409;
bus down → 503 and no row. API bind: `CANDYBAR_API_HOST`/`CANDYBAR_API_PORT`;
CLI target: `CANDYBAR_API_URL`.

## Runner kinds (Stories 7.3 and 7.4, AD-18, AD-19)

A candybar defines how the executor starts it with **exactly one** of
`entrypoint` (above: a bare executable, the `exec` default) or a `[candybar.runner]`
table. A runner names things on the host the executor runs on, so neither the
CLI nor the API checks the files it points at.

```toml
# exec: an argv in its own cgroup scope, fully metered
[candybar.runner]
kind = "exec"
argv = ["/usr/bin/restic", "backup", "--quiet"]   # absolute path or a bare command (PATH)
workdir = "/srv/vault"                            # absolute; default: the user's home
env = { RESTIC_REPOSITORY = "/mnt/backups" }      # non-secret literals only
timeout_s = 6000                                  # SIGTERM, then SIGKILL after 10s
on_failure = ["ntfy-publish", "--topic", "alerts"]
```

```toml
# systemd: start an existing user service and meter it from the unit
[candybar.runner]
kind = "systemd"
unit = "cache-prune.service"
```

```toml
# hermes: trigger a Hermes agent-mode job and wait for its durable attempt
[candybar.runner]
kind = "hermes"
profile = "33god-pm"        # hermes -p <profile>
job_id = "a1b2c3d4e5f6"     # the Hermes job id (hex), not its name
timeout_s = 3600            # default; bounds the whole wait (the CLI is killed 60s later)
```

| | `exec` | `systemd` |
| --- | --- | --- |
| Starts | `argv` as a child of the executor | `systemctl --user start --wait <unit>` |
| Keeps | nothing but what you give it | the unit's `EnvironmentFile`, hardening, `TimeoutStartSec`, `OnFailure=`; only its `.timer` retires |
| Verdict | exit code, refined by the AD-8 result file | `Result` / `ExecMainStatus` of the unit |
| Memory | cgroup `memory.peak` of the scope | the unit's `MemoryPeak` |
| CPU load | scope `cpu.stat` delta / wall runtime | `CPUUsageNSec` / (`ExecMainExitTimestamp` - `ExecMainStartTimestamp`) |
| Stderr tail | the child's stderr | the journal for that run's `InvocationID` (failures only; stdout and stderr are merged there) |
| GPU | pmon pids in the process tree | pmon pids in the unit's cgroup |

- **Env for systemd jobs.** The AD-8 vars reach a unit through a drop-in the
  runner installs once, `~/.config/systemd/user/<unit>.d/50-candybar.conf`
  (`EnvironmentFile=-%t/candybar/inv/%n.env`), and an env file it rewrites before
  each start: no `daemon-reload` per run. A unit that never reads them is
  unaffected. A `CANDYBAR_RESULT_FILE` the unit writes is honoured like an
  exec job's; a unit under `ProtectSystem=strict` may not be able to, and then
  the exit code decides.
- **Why there is a holder.** systemd 257 garbage-collects a *successful*
  oneshot the instant it stops, taking `MemoryPeak`, `CPUUsageNSec` and the
  `InvocationID` with it (a failed one stays loaded). The runner therefore lists
  every unit it starts as `After=` of `candybar-unit-holder.service`
  (`deploy/host/`, a plain `sleep infinity`), because systemd keeps a unit loaded
  while an active unit references it. `After=` adds the reference and no
  behaviour. Without the holder everything still runs and passes or fails
  correctly; the unit just meters 0.
- **The `hermes` runner (AD-19).** It runs
  `hermes -p <profile> cron run <job_id> --wait --timeout <timeout_s>` through
  the exec spawn path, so the CLI is metered like any exec child (and, because
  Hermes runs the attempt inside that CLI, so is the agent) while Hermes keeps
  delivery, sessions, `context_from` and monitor gates. Exit 0 is success, 1 is
  failure; the CLI's final `cron-run-result: {...}` line supplies the failure
  message and the attempt's input + output tokens as the token spend (0 when
  Hermes recorded none). Two requirements on the Hermes side, both shipped in
  delorenj/hermes-agent:
  - the job must be pinned `scheduler: external`
    (`hermes cron edit <id> --scheduler external`), or Hermes' own ticker would
    fire it as well. The ticker honours the pin after the gateway restart that
    story 7.17 batches; until then the pin is inert, so **do not cut over a
    Hermes job before 7.17 deploys it**.
  - a trigger that finds the job already running **joins** that attempt instead
    of starting another: never two concurrent attempts of one job.
  The executor finds the CLI as `hermes` on its `PATH` (`~/.local/bin/hermes`
  in the unit), or as `CANDYBAR_HERMES`.
- **Hermes no-agent jobs do not use this runner.** A `no_agent` Hermes job is a
  script whose stdout is delivered verbatim; migrate it as an `exec` runner that
  runs the script directly, and reproduce its `failure_deliver` with an
  `on_failure` hook (an argv that delivers `$CANDYBAR_FAILURE_MESSAGE` to the
  same target, e.g. `ntfy-publish`). That keeps no agent machinery in the path
  and nothing to pin.
- **`on_failure`** (any kind) is an argv run once, best effort and unmetered,
  right after a recorded failure, with the AD-8 env plus
  `CANDYBAR_FAILURE_MESSAGE`; it is capped at 60s. It is skipped, with a
  warning, for a `systemd` runner whose unit already has `OnFailure=` (that unit
  pages for itself), for a redelivery, and for a failure the executor's own
  shutdown caused.
- **Overlap.** A unit that is already starting or running is refused, not
  joined. Until AD-20's `skipped(reason=overlap)` lands, that is a recorded
  failure.
- **Executor restart.** Stopping the executor terminates the `systemctl` client,
  not the unit: a long unit keeps running under systemd, and its invocation is
  recorded `failed` ("executor stopped").
- Read what a candybar runs with `GET /candybars/<id>/runner`
  (`{runner, entrypoint}`, exactly one non-null); it is outside the Holocene
  contract. `PATCH` with a `runner` replaces the entrypoint and the other way
  round. The hermes runner is Story 7.4's.

## Timezones and DST (Story 7.1, AD-16)

A candybar's cron is read as **local wall time** in its `timezone` (an IANA
name, default `UTC`, so every pre-Epic-7 candybar is unchanged). The cron string
is stored exactly as written; only which UTC instants it selects depends on the
zone. Ticks, `tick_id`, idempotency keys, events and every API timestamp stay
UTC.

One helper, `dispatcher/firetimes.fire_times(schedule, tz, start_utc, end_utc)`,
is the only source of fire instants. The dispatcher's tick match, the schedule
window and next-fire reads, misfire planning and the skip walks all call it, so
they cannot disagree. It applies the Vixie-cron DST rules (croniter alone fires
`40 1 * * *` twice on the fall-back night):

| Case | Rule | `America/New_York` example |
| --- | --- | --- |
| Repeated hour, fixed hour field | fires once, at the first occurrence | `40 1 * * *` on 2026-11-01 fires at 05:40Z only |
| Repeated hour, hour field `*` | fires at both occurrences (real-time cadence) | `*/30 * * * *`, `0 * * * *` run every real 30 min / hour |
| Gap, fixed hour field | fires once at the first existing minute after the gap | `30 2 * * *` on 2027-03-14 fires at 07:00Z (03:00 EDT) |
| Gap, hour field `*` | the skipped wall minutes are not caught up | `30 * * * *` has no 02:30 that day |

`skip-rest-of-day`'s `date` is a date in the candybar's zone. `GET /candybars`
and the `created` / `updated` facts carry `timezone`; `PATCH /candybars/{id}`
can change it.

## Definitions API (CAP-8, Story 5.1)

The shapes are `_bmad-output/specs/spec-cron-framework/holocene-view-contract.md`,
field for field (`tests/contract_shapes.py` parses them out of that file):

```bash
curl https://candybar.delo.sh/candybars          # [{id, name, description, tags, schedule, timezone, noise, enabled, createdAt}, ...] oldest first
curl https://candybar.delo.sh/candybars/<id>     # the same + {totalInvoked, totalSuccessful}
curl -X POST https://candybar.delo.sh/candybars -H 'content-type: application/json' \
  -d '{"name":"my-job","schedule":"*/5 * * * *","entrypoint":"/abs/run.sh"}'   # 201, single shape
curl -X PATCH https://candybar.delo.sh/candybars/<id> -H 'content-type: application/json' \
  -d '{"schedule":"0 * * * *"}'                 # 200, single shape
```

`PATCH` takes any non-empty subset of `name`, `description`, `tags`,
`schedule`, `timezone`, `entrypoint`, `runner`, `noise`. The merged definition is validated like a
manifest. If anything actually changed, one `bloodbank.cron.candybar.updated`
fact goes out first, with `changes` = only the changed fields, and then the
row is written. Sending the stored values back is a 200 with no fact. Unknown
or read-only keys and `null` → 422; unknown id → 404; name taken → 409; bus
down → 503 with the row unchanged. `createdAt` is always UTC. The executor's
`entrypoint` and `runner` (Story 7.3) are accepted on writes but never echoed.

### Enabled flag (Story 7.5, AD-21)

```bash
curl -X POST https://candybar.delo.sh/candybars/<id>/enabled \
  -H 'content-type: application/json' -d '{"enabled":false}'   # 200, single shape
```

A disabled candybar stays registered and listed (`enabled: false`) but is
never dispatched: no tick or misfire command goes out for it, it has no
`current`/`upcoming` `/schedule` row (only past fires that actually ran), and
the host executor skips any of its queued scheduled/misfire commands with a
`skipped` fact (`reason=disabled`). A manual `candybar run` still runs it.
The body is the wanted state, not a toggle; the noise flow applies (one
`updated` fact with `changes: {"enabled": ...}` first, then the row; no fact
when nothing changes; 404 / 422 / 503 as on the noise routes). `POST
/candybars` accepts `"enabled": false`. `candybar migrate` (below) drives it.

### Noise collection (Story 5.5)

```bash
curl -X PUT    https://candybar.delo.sh/candybars/<id>/noise   # 200, single shape, noise: true
curl -X DELETE https://candybar.delo.sh/candybars/<id>/noise   # 200, single shape, noise: false
curl -X PUT    https://candybar.delo.sh/noise/batch -H 'content-type: application/json' \
  -d '{"ids":["<id>","<id>"]}'                                 # 200, {"ids": [...]} the collection
```

`PUT /noise/batch` replaces the collection. Afterwards exactly the given ids
are noise, and every other candybar is not. So send the whole set you want:
`[]` clears it, and duplicate ids count once. The answer lists the resulting
collection, oldest candybar first.

Each candybar whose flag actually changes gets one
`bloodbank.cron.candybar.updated` fact with `changes: {"noise": ...}`. The
fact goes out first, then the row is written. A call that changes nothing (the
flag already has that value) publishes nothing. `/schedule` rows,
`GET /candybars` and the stream's `definition.changed` all show the new flag.

The batch is all or nothing. Any unknown id → 422 naming it, and nothing
changes. A bus failure → 503, and no flag is written. Facts already sent for
earlier candybars stand until you retry, and a retry of the same batch
converges. A database failure rolls the whole batch back. A bad body (missing
or null `ids`, a malformed id, extra keys, more than 10,000 ids) → 422. On the
single routes: unknown id → 404, bus down → 503 with the row unchanged.

candybar stores only the explicit collection. Marking crons that repeat more
often than every 10 minutes is the view's job, computed from the cron string;
a minutely job stays `noise: false` until someone adds it.

### Skip operations (Story 5.6)

```bash
curl -X POST https://candybar.delo.sh/candybars/<id>/skip \
  -H 'content-type: application/json' -d '{"fireTime":"2026-10-26T10:10:00Z"}'
# 200 {"skipped":true,"fireTime":"2026-10-26T10:10:00+00:00"}

curl -X POST https://candybar.delo.sh/candybars/<id>/skip-rest-of-day \
  -H 'content-type: application/json' -d '{"date":"2026-10-26"}'
# 200 {"skipped":true,"date":"2026-10-26"}
```

`POST /candybars/{id}/skip` skips the single fire `fireTime` (a UTC minute;
any offset is converted). `POST /candybars/{id}/skip-rest-of-day` suppresses
every fire of that date in the candybar's timezone (UTC unless it has one) —
`date` covers the whole local date, and the next local day dispatches normally. The dispatcher honors both on its next tick:
`/schedule` rows for the suppressed fires read `skipped: true` with the
upcoming times recomputed, and the stream's `skip.applied` event arrives with
the write itself (the route publishes the `bloodbank.cron.candybar.skipped`
fact first, then stores the row).

Both routes are idempotent: repeating a call answers 200 and keeps the first
row. A `fireTime` the schedule never produces, or a date the schedule never
fires on, is a 404 (`{"title":"No such fire", ...}`) — as is an unknown id. A
naive or off-minute `fireTime`, a non-date `date`, or extra body keys are a
422. A bus failure answers 503 with nothing stored.

## Schedule window (CAP-8, Story 5.2)

```bash
curl 'https://candybar.delo.sh/schedule?from=2026-10-26T00:00:00Z&to=2026-10-27T00:00:00Z'
# [{candybarId, name, schedule, fireTime, state, lastResult?, skipped, noise}, ...]
```

You get one row per fire of every registered candybar in `from <= fireTime < to`,
ordered by `(fireTime, name)`. Fire times come from the dispatcher's own
`fire_times` helper and skip helpers (each cron read in its candybar's
timezone). `fireTime` is always UTC, and only fires after the candybar's
registration are listed. `from` and `to` need an offset; use `Z`, because a raw
`+` in a query string decodes as a space.

- `state`: `past` means the fire time has been reached. `current` is the single
  next fire across all candybars that is not skipped; the window contains it only
  when that fire falls inside the window. `upcoming` is everything later.
- `skipped`: an `occurrence` or `rest_of_day` skip covers the fire, and no run was
  recorded for it.
- `lastResult`: present only on past rows whose fire has a recorded run (scheduled
  or misfire), and never null. Manual runs are not schedule rows.

You get a 422 problem when `from`/`to` is missing or has no offset, when
`to <= from`, when the window is longer than 31 days, or when it holds more than
20,000 fires.

### Next fire (poll fallback, Story 5.3)

```bash
curl https://candybar.delo.sh/schedule/next
# {candybarId, name, schedule, fireTime, state: "current", skipped: false, noise}
```

This returns the single `current` row, the same row `/schedule` marks current
in any window that contains it. Poll it when the stream is unavailable. It is
cheap: it reads the definitions and the skips stored at or after the next
tick, never the history, and does one `next_fire` lookup per candybar. A
skipped day costs one step, not one per fire. Budget: under 50 ms (in-memory)
and under 100 ms (real Postgres) for 200 candybars. You get a 404 problem when
nothing will fire: the registry is empty, or no schedule fires again.

### Live updates (SSE, Story 5.4)

```bash
curl -N 'https://candybar.delo.sh/schedule/stream?from=2026-10-26T00:00:00Z&to=2026-10-27T00:00:00Z'
# retry: 3000
#
# id: 3f9c1a7e02b4-0
# event: ready
# data: {"type":"ready","seq":0,"key":"ready","at":"...","cut":"...","current":{...row...}}
#
# id: 3f9c1a7e02b4-1
# event: current.advanced
# data: {"type":"current.advanced","seq":1,"key":"current","at":"...","cut":"...","current":{...row...}}
```

One SSE channel, fed by the bus. The api role reads every `bloodbank.evt.cron.*`
fact through an ephemeral, ordered JetStream consumer, with no durable and no
acks. It dedupes ticks on `tick_id`, and turns each fact into events that carry
`/schedule` rows:

| Event | When | Payload (besides `type`, `seq`, `key`, `at`) |
|---|---|---|
| `invocation.started` | `invoked` | `candybarId`, `invocationId`, `trigger`, `row` |
| `invocation.completed` | `completed` / `failed` | the same, plus `result`; `row.lastResult` |
| `skip.applied` | `skipped` | `candybarId`, `reason`, `row` (`skipped` only for operator skips) |
| `definition.changed` | `created` / `updated` | `change`, `definition`, `changes`; `rows` with a window |
| `current.advanced` | a tick or definition change moves the current row | `cut`, `current` (a row, or null) |

A manual run has no schedule row, so its invocation events carry `row: null`
and the key `invocation:<id>`.

Patch rule for a consumer:
- On `current.advanced`, rows with `fireTime < cut` become `past`, the row
  matching `current` becomes `current`, and the rest become `upcoming`.
- A row event replaces the row at its key, `<candybarId>@<fireTime>`.
- `definition.changed` sets `name`, `schedule` and `noise` on that candybar's
  rows. With a window, it replaces those rows with `rows` instead.

The `from`/`to` window is optional: give both or neither, with the same limits
as `/schedule`. Row events outside the window arrive as id-only frames, which
move the client's cursor and dispatch nothing.

Every payload is absolute, so applying one twice changes nothing.

Ids are `<boot>-<n>`, and the last 1,024 events are kept. A reconnect whose
`Last-Event-ID` is still in that buffer replays exactly the missed events. Any
other id gets a `reset`: refetch `/schedule`. So does an api restart, which
changes the boot. `seq` counts within one boot, so on a `reset` also forget the
seqs you applied.

Each tick sends a `: keepalive` comment, so there is no timer of its own. Stop
closes every stream, so the role still stops inside docker's grace.

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

## Migrate a cron job onto candybar (Story 7.5, AD-21)

```bash
uv run candybar migrate plan <inventory-id> --write   # validate, probe the native scheduler, fill default commands
uv run candybar migrate cutover <inventory-id>        # register disabled -> retire native -> enable
uv run candybar migrate verify <inventory-id>         # first scheduled fire is the proof (exit 3 until then)
uv run candybar migrate rollback <inventory-id>       # restore native FIRST, then disable (works with the daemon down)
uv run candybar migrate decommission <inventory-id> --reason '...'  # retire for good (rollback then refuses)
uv run candybar migrate status [--json] [--probe]
```

Drives the committed ledger `deploy/migration/ledger.yaml` as a REST client of
the api role (never a registry writer). Schema, guard window, per-kind
commands and the by-hand rollback: [`deploy/migration/README.md`](deploy/migration/README.md).

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
`kind=rest_of_day` and a `day` (a date in the candybar's timezone) suppresses
every fire of that candybar on that local date — no invoke commands and no
`invocations` rows until the next local midnight, then the schedule dispatches normally again. Each suppressed
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

