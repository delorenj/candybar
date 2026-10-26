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
| `host/install.sh` | renders `~/.config/candybar/executor.env` (mode 600), links + enables the unit, (re)starts it |

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

### Deploying (a pair)

Any change that touches the daemon redeploys both halves, container first, so
a new migration is applied before the executor's guard looks:

```bash
~/docker/stacks/_lib/op-inject.sh deploy/.env.template deploy/.env   # gitignored, mode 600
docker compose -f deploy/compose.yml up -d --build                   # control plane (+ alembic upgrade head)
systemctl --user restart candybar-executor                           # executor, same git SHA
```

The first time on a host, `deploy/host/install.sh` does the unit install and
the restart. Then prove it: `curl -s https://candybar.delo.sh/healthz`, and
the next minute tick must show a `deploy-smoke` invocation
(`journalctl --user -u candybar-executor` or
`GET /candybars/<id>/invocations`).

Rollback to the single-container shape: `systemctl --user disable --now
candybar-executor`, then restore the pre-7.2 `deploy/compose.yml` (the
executor role starts again with no `--roles` flag).
