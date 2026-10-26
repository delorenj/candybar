# deploy

Alembic migration env for the registry landed in Story 1.4: `alembic.ini` +
`alembic/` (initial migration creates `candybars` / `invocations` / `skips`,
owner-only, DSN from `DATABASE_URL`). Run from the repo root:

```bash
uv run alembic -c deploy/alembic.ini upgrade head
```

The container envelope is implemented by Story 1.9: one docker container
composing all roles behind Traefik at `candybar.delo.sh`, on the existing
`proxy` network, with Prometheus scraping `/metrics`. Secrets only as env vars
holding `op://DeLoSecrets/...` references; service references are `*.delo.sh`
hostnames (AD-15).
