---
title: 'Story 7.2: Host executor'
type: 'feature'
created: '2026-10-26'
status: 'in-review'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '08eb38a'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-7-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The executor runs inside the container, which cannot reach what migrated jobs need (`systemctl --user`, `op`, the docker socket, `~/`, zellij, the hermes CLI).

**Approach (AD-17):** The executor role runs only on the host as the `systemd --user` unit `candybar-executor.service` (`candybar daemon --roles executor`, `Delegate=yes`, from the repo checkout's `.venv`, NATS and Postgres over host loopback). The container runs `--roles heartbeat,dispatcher,api`. One invoke subject, one `candybar-executor-invoke` durable, no routing field. The executor serves its own `/metrics` on 8701 and refuses to consume while the registry's alembic head differs from its own.

## Boundaries & Constraints

**Always:** the WorkQueue stream and durable are unchanged; the unit source is tracked in `deploy/host/` and linked (not copied) into the user manager; secrets only as an `op://` reference rendered to a mode-600 file outside the repo; the schema guard fails closed and is a guard, not a DB lock; a redeploy is a pair (container, then unit).

**Never:** a host/container routing field; per-candybar bus subscribers; runner kinds, drop-ins or the ledger (7.3-7.5); migrating any job.

## Acceptance Criteria

1. `deploy/host/candybar-executor.service` runs the executor role from `.venv` with `Delegate=yes`, on loopback NATS/Postgres, attaching the existing durable.
2. The container runs `--roles heartbeat,dispatcher,api`; no container executor remains.
3. The executor serves `/metrics` (+ `/healthz`) on 8701, scraped through `host.docker.internal`.
4. The executor consumes nothing while the registry alembic head differs from its own.
5. `deploy/README.md` documents the pair-deploy; a redeploy proves `deploy-smoke` still fires each minute.

</frozen-after-approval>

## Code Map

- `src/candybar/executor/metrics_server.py` -- stdlib asyncio `/metrics` + `/healthz` (no second uvicorn: it swaps process signal handlers)
- `src/candybar/registry/schema_guard.py` -- `own_head`, `database_head`, `SchemaGate` (in `registry/` per AD-10, the sole SQL/alembic module)
- `src/candybar/executor/role.py` -- starts/closes the metrics endpoint around the role; the consume loop idles while the gate is closed; fails closed before touching the bus when its own head is unknowable
- `deploy/host/` -- `candybar-executor.service`, `run-executor.sh` (resolves the delegated cgroup), `executor.env.template`, `install.sh`
- `deploy/compose.yml`, `Dockerfile` -- control-plane roles only; executor-only layers (node, SDK, privileged/pid/gpu) removed
- monitoring repo `prometheus/prometheus.yml` -- `candybar-executor` scrape job (3f4d09a)
- `tests/test_executor_host.py` -- 19 tests (+1 live-PG)

## Design Notes

- **`DelegateSubgroup=main`** keeps the daemon out of the unit cgroup itself, so the unit cgroup has no internal processes and can delegate memory/cpu to per-invocation scopes. The wrapper derives the scope root from `/proc/self/cgroup` (parent of `main`); `CANDYBAR_CGROUP_ROOT` in the environment still wins, and no delegation degrades to unscoped (never fails execution).
- **`KillMode=mixed`**: SIGTERM only to the executor so it records in-flight children `failed` itself; the rest is SIGKILLed after `TimeoutStopSec=60`.
- **Schema guard** is cached (5s while blocked, 30s while open), so a migration applied by a later container deploy also closes a running executor. Unreadable DB = closed (keeps retrying); unreadable scripts = refuse to start.
- **Jobs mount kept read-only in the container** because the API validates `entrypoint` existence against its own filesystem (422 otherwise); removing it would break registering existing-shape candybars. 7.3's runner `argv` is not file-validated by the API.
- **No `After=`/`Requires=`**: NATS, Postgres and docker are system/docker services a user unit cannot order against, and a container redeploy must not bounce the executor.

## Verification

- Unit suite 832 passed, 54 skipped, ruff clean; with throwaway postgres:17-alpine + nats:2.10.29: host/executor/claim/manual-invoke integration suites 28 passed.
- Live: unit installed and enabled (`Delegate=yes`, `DelegateSubgroup=main`, running in `<unit>/main`); container recreated with `--roles heartbeat,dispatcher,api` (healthz 200); the 14:55 and 14:58 UTC ticks fired `deploy-smoke` and `hello-node` from the host executor, with memory/CPU metered from per-invocation cgroup scopes; `restart` takes ~1s and the next tick still fires; Prometheus target `candybar-executor` is `up` via `host.docker.internal:8701`; a start with an unreadable migration dir exits with `SchemaGuardError` before connecting.

## Independent Review (inline layers, 2026-10-26)

No subagent tool was available; blind/edge/acceptance layers ran inline.

- **Patched:** the first cut of the schema guard lived in `executor/` and failed the AD-10 sole-writer test (`test_only_registry_touches_the_database`); moved to `registry/schema_guard.py`. The first cut loaded the repo `alembic.ini`, which mutates `sys.path`; replaced with a bare `Config` pointing at the script dir.
- **Rejected:** hard `Requires=` ordering (impossible for system services from a user unit); a uvicorn metrics server (signal-handler swap); removing the jobs mount (API validation).
- **Accepted:** the metrics endpoint binds `0.0.0.0` so the Prometheus container can reach it (single-tenant homelab).
- **Note for 7.3+:** an `exec` runner with `argv`/`workdir` should drop the API's filesystem existence check for runner-shaped registrations; the container cannot see host paths.
