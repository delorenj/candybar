# deploy

## Registry migrations (Story 1.4)

`alembic.ini` + `alembic/` (initial migration creates `candybars` /
`invocations` / `skips`, owner-only, DSN from `DATABASE_URL`). Run from the
repo root:

```bash
uv run alembic -c deploy/alembic.ini upgrade head
```

## Container envelope (Story 1.9, split by Story 7.2)

One docker container composes the **control-plane** roles (heartbeat +
dispatcher + api) behind Traefik at `candybar.delo.sh`, on the existing `proxy`
and `bloodbank-network` networks. The **executor** is not in it: it runs on the
host as a `systemd --user` unit (below, AD-17). The homelab Prometheus scrapes
two endpoints: the container's `/metrics` and the executor's own `:8701`.

| file | role |
|---|---|
| `compose.yml` | the container: build, networks, Traefik labels, healthcheck, `--roles heartbeat,dispatcher,api` |
| `.env.template` | `DATABASE_URL` as an `op://DeLoSecrets/...` whole-field reference |
| `container-entrypoint.sh` | image entrypoint: `alembic upgrade head`, then `exec candybar daemon "$@"` |
| `host/` | the host executor unit, wrapper, env template and installer (below) |
| `prometheus/candybar-queries.test.yml` | promtool tests for the documented PromQL (Story 1.7) |

Notes:

- The container runs nothing a job needs. It mounts `~/candybar/jobs` read-only
  at the same path only because the API validates that a registered
  `entrypoint` is an existing file (422 otherwise) against its own
  filesystem, while the host executor runs the same absolute path.
- The container sets `CANDYBAR_CGROUP_ROOT=""`: no invocations run in it, so
  there is nothing to delegate.
- `scripts/check-deploy-hygiene.sh` (repo root) gates every push: no LAN IPs,
  no plaintext-secret shapes on any deploy surface.

## Host executor (Story 7.2, AD-17)

`host/candybar-executor.service` runs `candybar daemon --roles executor` from
the repo checkout's `.venv` as the user, with `Delegate=yes` and
`DelegateSubgroup=main` (the daemon sits in `<unit>/main`; per-invocation
cgroup v2 scopes are created in the unit cgroup above it, so memory and CPU
meter exactly as before). It reaches NATS and Postgres on host loopback and
attaches the same `candybar-executor-invoke` durable the container used: the
WorkQueue stream is unchanged, and there is no host/container routing field.

| file | role |
|---|---|
| `host/candybar-executor.service` | the unit (linked into `~/.config/systemd/user`, so this file stays the source of truth) |
| `host/run-executor.sh` | `ExecStart`: resolves the delegated cgroup, then execs the executor from `.venv` |
| `host/executor.env.template` | `DATABASE_URL` as an `op://` reference |
| `host/candybar-unit-holder.service` | keeps started systemd units loaded so their accounting survives (Story 7.3, below) |
| `host/candybar-smoke.service` | a no-side-effect oneshot the `deploy-smoke-systemd` canary starts (Story 7.3) |
| `host/install.sh` | renders `~/.config/candybar/executor.env` (mode 600), links + enables the units, (re)starts the executor |

- **Metrics:** the executor serves its own `/metrics` + `/healthz` on `:8701`
  (`CANDYBAR_EXECUTOR_METRICS_HOST|PORT`; an empty port disables it). The
  monitoring repo's `candybar-executor` job scrapes it through
  `host.docker.internal`. A bind failure is logged and never stops the executor.
- **Schema guard:** the executor consumes nothing while the registry's
  `alembic_version` differs from the head of its own checkout's
  `deploy/alembic`. It is a guard, not a lock: it waits (logging a warning,
  rechecking every 5s) and opens by itself when the container has migrated.
  It fails closed: if it cannot read its own migration scripts it refuses to
  start. `CANDYBAR_SCHEMA_GUARD=off` skips it (tests, one-off dev runs).
- **Logs:** `journalctl --user -u candybar-executor -f`.
- **Jobs' environment:** the unit's `PATH` leads with the repo `.venv/bin`
  (python + `candybar_sdk`) and the mise shims (node, ...). Secrets reach jobs
  from the user manager's environment or `op` inside the job, never the repo.

### Runner kinds on the host (Story 7.3, AD-18)

The executor starts a candybar as an `exec` argv or a `systemd` unit (README,
"Runner kinds"). The systemd runner depends on two things this directory ships:

- `candybar-unit-holder.service`, enabled and running. systemd 257 collects a
  successful oneshot the moment it stops, accounting included; the runner
  maintains `~/.config/systemd/user/candybar-unit-holder.service.d/60-held-units.conf`
  (one `After=<unit>` per candybar-started unit) so each stays loaded long enough
  to read `MemoryPeak` and `CPUUsageNSec`. It never needs a restart.
- the per-unit drop-in `<unit>.d/50-candybar.conf`, written by the runner on the
  first start of a unit (one `daemon-reload`, which does not touch running units).

Neither is something to edit by hand; the runner restores both if they drift.
Retiring a migrated unit's timer leaves its drop-in and holder line in place
(harmless). The canaries `deploy-smoke-exec` and `deploy-smoke-systemd` are the
live proof of both kinds after a redeploy (`candybar run <id>`; no side effects; manifests in `deploy/smoke/`, registered with `candybar register`).

### The hermes runner on the host (Story 7.4, AD-19)

The `hermes` runner shells out to `hermes -p <profile> cron run <job_id> --wait`,
found as `hermes` on the executor unit's `PATH` (`~/.local/bin/hermes`) or as
`CANDYBAR_HERMES`. Nothing else is installed for it. The Hermes job must be
pinned `scheduler: external`, and that pin only takes effect in the gateway after
the batched `hermes-gateway-restart` of wave 7.17, so no Hermes job is cut over
before then. `deploy/smoke/hermes/` is the live canary (README there).

## Host steward (steward-contract.md section 4)

`host/candybar-steward.service` runs `candybar daemon --roles steward` from the
repo checkout's `.venv`: an event-driven worker, not a clock. It attaches the
`candybar-steward` durable on `BLOODBANK_COMMANDS` (filter
`bloodbank.cmd.cron.candybar.steward`, disjoint from the executor's), runs at
most two tasks at a time, and is schema-gated like the executor (it consumes
nothing until the registry's alembic head matches its checkout).

| file | role |
|---|---|
| `host/candybar-steward.service` | the unit (linked into `~/.config/systemd/user`) |
| `host/run-steward.sh` | `ExecStart`: execs the steward role from `.venv` |
| `host/steward.env.template` | `DATABASE_URL` and `CANDYBAR_JEV_KEY` as `op://` references (by item UUID) |
| `steward/templates/{enrich,fix,evaluate,refactor}.md` | the versioned prompts (front matter `template_version`) |

- **OpenCode:** the steward drives the user's OpenCode background service
  over its v2 HTTP API, reading the URL and Basic-auth secret from
  `~/.local/state/opencode/service.json` (the file the `opencode` client
  maintains; the URL moves when the service restarts) on every session.
  Models: `automaticai/auto` for evaluate, `automaticai/auto/fast` for the
  rest. Budgets: enrich 15 min, evaluate 20 min, fix and refactor 30 min,
  then the session is interrupted and the task fails. Enrich and evaluate
  sessions deny the edit/write/patch tools (OpenCode `permissions`).
- **Jev:** enrich's project classification uses the steward's own LiteLLM
  virtual key on the jev-litellm ingress (alias `candybar-steward`, $1/day,
  120 rpm; vault item `icxowfdsbslgo2yyqfxuxhqaym`). The job's own checkout
  (the ledger repo's, or the runner's home when the ledger entry lives in this
  repo) that is a pjangler project wins without a Jev call.
- **Reports:** evaluate writes
  `~/code/DeLoDocs/Reports/<date>/candybar-evaluate-<name>-<HHMM>-<task8>.{md,json}`
  and `mc cp`s both to `delo/daily-reports/<date>/` (the unit's PATH has
  `/usr/local/bin/mc`).
- **Failures** never wedge the consumer: any exception fails the task with an
  `error`; the command is acked once that is recorded (nak'd and retried when
  the write fails). A redelivered command whose task is `running` (the steward
  died mid-task) is failed as abandoned, never re-run. On startup the steward
  fails every task left `running` and every `queued` one older than the
  command stream's max_age (24 h), and logs it
  (`steward: startup reaper failed N task(s)`).
- **Cancel:** `candybar steward cancel <task-id>` fails a queued or running
  task ("cancelled by operator") and interrupts its OpenCode session.
- **Logs:** `journalctl --user -u candybar-steward -f`; the task list is
  `candybar steward tasks --active`.

`host/install.sh` renders `~/.config/candybar/steward.env` beside
`executor.env`, links and enables the unit, and restarts it with the executor.
A pair deploy therefore restarts all three: the container (migrations), then
`systemctl --user restart candybar-executor candybar-steward`.

## Off-candybar watchdog (Story 7.6, AD-22)

`watchdog/` is a `systemd --user` timer (every 2 min) that watches candybar
from OUTSIDE candybar — never registered as a candybar (CI gate:
`scripts/check-watchdog-not-a-candybar.sh`). It checks `/healthz`, that the
every-minute `watchdog-canary` candybar (`deploy/smoke/watchdog-canary/`)
finished within 3 minutes, and that `candybar-executor.service` is active;
pages ntfy once per outage and once on recovery; touches an ok-stamp that
infra-doctor's `candybar-watchdog-alive` file_age probe watches. Install with
`deploy/watchdog/install.sh`; details in `deploy/watchdog/README.md`.

### Deploying (a pair)

Any change that touches the daemon redeploys both halves, container first, so
a new migration is applied before the executor's guard looks:

```bash
~/docker/stacks/_lib/op-inject.sh deploy/.env.template deploy/.env   # gitignored, mode 600
docker compose -f deploy/compose.yml up -d --build                   # control plane (+ alembic upgrade head)
systemctl --user restart candybar-executor candybar-steward          # host roles, same git SHA
```

The first time on a host, `deploy/host/install.sh` does the unit installs and
the restart. A change to the registry (a new migration, such as 0006's `runner`
column, 0007's `enabled` flag or 0008's archive flag, profiles and steward
tasks) is why the order is container first. Then prove it: `curl -s https://candybar.delo.sh/healthz`, and
the next minute tick must show a `deploy-smoke` invocation
(`journalctl --user -u candybar-executor` or
`GET /candybars/<id>/invocations`).

Rollback to the single-container shape: `systemctl --user disable --now
candybar-executor`, then restore the pre-7.2 `deploy/compose.yml` (the
executor role starts again with no `--roles` flag).
