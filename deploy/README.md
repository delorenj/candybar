# deploy

## Registry migrations (Story 1.4)

`alembic.ini` + `alembic/` (initial migration creates `candybars` /
`invocations` / `skips`, owner-only, DSN from `DATABASE_URL`). Run from the
repo root:

```bash
uv run alembic -c deploy/alembic.ini upgrade head
```

## Container envelope (Story 1.9)

One docker container composes all daemon roles (heartbeat + dispatcher +
executor + api) behind Traefik at `candybar.delo.sh`, on the existing `proxy`
and `bloodbank-network` networks, with the homelab Prometheus scraping
`/metrics` (scrape job + exemplar storage live in the monitoring repo).

| file | role |
|---|---|
| `compose.yml` | the whole envelope: build, networks, Traefik labels, healthcheck, jobs bind mount |
| `.env.template` | `DATABASE_URL` as an `op://DeLoSecrets/...` whole-field reference |
| `container-entrypoint.sh` | image entrypoint: `alembic upgrade head`, then `exec candybar daemon` |
| `prometheus/candybar-queries.test.yml` | promtool tests for the documented PromQL (Story 1.7) |

Bring-up:

```bash
~/docker/stacks/_lib/op-inject.sh deploy/.env.template deploy/.env   # gitignored, mode 600
docker compose -f deploy/compose.yml up -d --build
```

Notes:

- Entrypoints registered with the daemon run inside the container; job
  scripts live in `~/candybar/jobs` (bind-mounted at the same path inside;
  `CANDYBAR_JOBS_DIR` overrides).
- The container sets `CANDYBAR_CGROUP_ROOT=""` — docker is the isolation
  boundary, and the container cannot write the host cgroupfs anyway.
- `scripts/check-deploy-hygiene.sh` (repo root) gates every push: no LAN IPs,
  no plaintext-secret shapes on any deploy surface.
