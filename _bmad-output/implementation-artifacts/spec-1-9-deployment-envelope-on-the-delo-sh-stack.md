---
title: 'Deployment envelope on the delo.sh stack'
type: 'feature'
created: '2026-10-26'
status: 'done'
baseline_commit: '5a8fdc4080da6b743d56298bb6830069b87997ea'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The full walking skeleton (1.1–1.8) runs only from a developer shell; AD-15 requires one container on the delo.sh stack, and Epic 1's success criterion is the skeleton running continuously on the homelab with zero new infrastructure. Two deferred items land here: live Prometheus lacks the candybar scrape job and exemplar storage, and the executor/dispatcher roles take ~30s to stop (nats-py pull-sub drain), which would be SIGKILLed by docker's 10s default grace.

**Approach:** Add a root Dockerfile + `deploy/compose.yml` envelope (Traefik `candybar.delo.sh`, `proxy` + `bloodbank-network`, secrets via `deploy/.env.template` op:// refs materialized by op-inject), a CI hygiene grep gate + image build, a fast-stop fix (unsubscribe pull subs before drain), and a cross-repo monitoring change (candybar scrape job + `--enable-feature=exemplar-storage`).

## Boundaries & Constraints

**Always:** one container composes all roles (default); networks are docker aliases (`nats:4222` on `bloodbank-network`, `host.docker.internal:5432` via `extra_hosts: host-gateway` for the host Postgres) — never LAN IPs; secrets only as `op://DeLoSecrets/...` refs in a gitignored `.env`; Traefik labels for `candybar.delo.sh` TLS websecure (prometheus-stack label pattern); image base `python:3.13-slim` (CI floor), deps from `uv sync --frozen --no-dev`; CI stays on `ubuntu-latest` (repo is public).

**Never:** no new infra (no registry push, no new Postgres/NATS, no per-role containers); no plaintext secrets or resolved `.env` committed; no `CANDYBAR_CGROUP_ROOT` scoping inside the container (docker is the isolation; set it to `""`); no OIDC middleware on the route (edge-only auth per spine; CLI needs plain HTTP).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| BUILD | `docker build` in CI + locally | image builds; `candybar --help` runs in-image | build fails loudly on lockfile drift |
| DEPLOY | `docker compose up -d` with materialized `.env` | all roles up; `/healthz` 200; Traefik serves `candybar.delo.sh` | missing `.env` fails at inject step, never boots half-configured |
| HYGIENE_GATE | repo scanned by `scripts/check-deploy-hygiene.sh` | pass; any `192.168.`/RFC1918 LAN IP or secret-shaped literal in `Dockerfile deploy/ src/ .env*` fails CI | grep hits exit 1 with the offending lines |
| FAST_STOP | SIGTERM with idle roles | executor + dispatcher stop in <10s (unsubscribe before drain) | in-flight children still SIGTERMed and recorded failed first |
| REJOIN | `docker restart candybar` mid-schedule | no duplicate invocations rows/events for the rejoin window | idempotency keys + durable consumers dedupe |
| SCRAPE | live Prometheus after config reload + recreate | `candybar` target up; exemplars stored (`query_exemplars` non-empty) | scrape config validated by promtool before apply |

</frozen-after-approval>

## Code Map

- `src/candybar/api/app.py` — add `GET /healthz` (200 `{"status":"ok"}`); reuse for docker healthcheck.
- `src/candybar/executor/role.py` + `src/candybar/dispatcher/role.py` — `await sub.unsubscribe()` before `nc.drain()` in `finally` (the deferred 30s-stop fix).
- `src/candybar/bus.py` — `NATS_URL` env (default `nats://localhost:4222`); compose sets `nats://nats:4222`.
- `src/candybar/registry/store.py` — `DATABASE_URL` env; compose sets host-postgres DSN via op ref.
- `src/candybar/api/role.py` — `CANDYBAR_API_HOST`/`CANDYBAR_API_PORT`; compose sets `0.0.0.0:8700`.
- `src/candybar/executor/runner.py` — `CANDYBAR_CGROUP_ROOT=""` disables cgroup scoping.
- `deploy/alembic.ini` — already `DATABASE_URL`-driven; container entrypoint runs `alembic upgrade head` then execs daemon.
- `/home/delorenj/code/33GOD/monitoring/prometheus/prometheus.yml` + `monitoring/compose.yml` — CROSS-REPO: add candybar job (`candybar:8700`) and `--enable-feature=exemplar-storage`.
- `~/docker/stacks/_lib/op-inject.sh` — the established template→`.env` materializer; reuse by hand/mise task, don't copy.
- Reference envelopes: `~/code/33GOD/candystore/{Dockerfile,compose.yml}` (local-build image, external networks, 127.0.0.1 port publish), `monitoring/compose.yml` (Traefik label set).
- Vault item: `op://DeLoSecrets/sxnfrbik4osfovqgofyuuszqda/password` (Candybar Postgres Role; UUID ref — never title).
- 1Password CLI `op` and live stack (nats/pg/traefik/prometheus) are present on this host.

## Tasks & Acceptance

**Execution:**
- [x] `Dockerfile` (root) — NEW: two-stage uv build on `python:3.13-slim`; `uv sync --frozen --no-dev`; copy `src/`, `pyproject.toml`, `uv.lock`, `deploy/alembic*`, `schemas/`; non-root user; entrypoint runs `alembic -c deploy/alembic.ini upgrade head` then `candybar daemon`; `.dockerignore` (`.git`, `_bmad*`, `tests`, `sdks`, `__pycache__`, `.env*`).
- [x] `deploy/compose.yml` + `deploy/.env.template` — NEW: single `candybar` service (build `context: ..`), networks `proxy` + `bloodbank-network` (external), `extra_hosts` host-gateway, `127.0.0.1:8700:8700` publish, Traefik labels for `candybar.delo.sh` (websecure+letsencrypt, port 8700), healthcheck on `/healthz`, `restart: unless-stopped`, env `CANDYBAR_CGROUP_ROOT=""`, `NATS_URL=nats://nats:4222`, `CANDYBAR_API_HOST=0.0.0.0`; template carries `DATABASE_URL=postgresql+psycopg://candybar:op://...sxnfrbik4osfovqgofyuuszqda/password@host.docker.internal:5432/candybar`; gitignore `deploy/.env`.
- [x] `src/candybar/api/app.py` + `tests/test_api_healthz.py` — `/healthz` route + test.
- [x] `src/candybar/executor/role.py` + `src/candybar/dispatcher/role.py` + tests — unsubscribe-before-drain; unit-test call order on fakes; live-NATS integration test asserts full role stop < 10s.
- [x] `scripts/check-deploy-hygiene.sh` — NEW: ripgrep LAN-IP + secret-shape patterns over `Dockerfile deploy/ src/ .env.template .dockerignore`, whitelisting `op://` refs and `${...}`; exits 1 with hits.
- [x] `.github/workflows/ci.yml` — add `docker build` step and hygiene-gate step.
- [x] `tests/test_deploy_hygiene.py` — pytest wrapper running the script against the repo (gate enforced locally, not only in CI) + positive-control fixtures.
- [ ] monitoring repo (cross-repo): `prometheus/prometheus.yml` candybar scrape job; `compose.yml` add `--enable-feature=exemplar-storage`; promtool-check config, reload, recreate, verify target up; commit+push that repo too.
- [x] `README.md` + `deploy/README.md` — deployment section: build, `op-inject` the template, `docker compose up -d`, smoke checks.
- [ ] Live deploy smoke: build image, materialize `.env`, `up -d`; assert `/healthz`, Traefik HTTPS, Prometheus target up, `candybar register`+`run` through the route; `docker restart` rejoin test (no duplicate invocations); scrape exemplar present; update `deferred-work.md` (close both 1.7 items).

**Acceptance Criteria:**
- Given the `deploy/` directory, when the container is built and deployed, then one docker container runs all roles on the existing `proxy` network with Traefik labels for `candybar.delo.sh`.
- Given the live Prometheus, when its config is applied, then it scrapes the container's `/metrics` and stores `invocation_id` exemplars.
- Given the CI workflow, when any tracked deploy surface contains a LAN IP or plaintext-secret shape, then CI fails (grep gate green on main).
- Given a running container, when it is restarted, then rejoining causes no missed-tick duplication (idempotent consumers reconnect; existing dedupe tests + live restart smoke).

## Implementation Notes

- Baseline 5a8fdc4. No subagent spawn capability in this session — implemented inline (same deviation as 1.1-1.8).
- Files: `Dockerfile` + `.dockerignore` (root), `deploy/{compose.yml,.env.template,container-entrypoint.sh,README.md}`, `scripts/check-deploy-hygiene.sh`, `src/candybar/api/app.py` (/healthz), `src/candybar/{executor,dispatcher}/role.py` (unsubscribe-before-drain), tests `test_api_healthz.py`, `test_deploy_hygiene.py` (12 cases), `test_fast_stop_integration.py`, shutdown-order pins in the two role tests, ci.yml (hygiene gate + docker build + fast-stop integration), README Deploy section, root `.gitignore` (`!**/.env.template`).
- CROSS-REPO: monitoring@80f6d2f — candybar scrape job (`candybar:8700`) + `--enable-feature=exemplar-storage`; promtool-checked, applied via `op run --env-file=.env.op -- docker compose up -d prometheus` + `/-/reload`; target up, 3 exemplars stored incl. the smoke manual invocation id.
- Surprises: (1) psycopg pure-python wheel dlopens libpq — runtime stage needed `apt-get install libpq5`, caught by first boot crash-loop. (2) `op inject` cannot parse a vault reference embedded inline in a URL (resolved 0 refs) AND scans comments (a prose "op://" in a comment broke injection) — fixed by adding a whole-value `dsn` field to vault item sxnfrbik4osfovqgofyuuszqda and rewording the comment. (3) .dockerignore `.env` patterns are root-relative — `deploy/.env` needed `**/.env` to stay out of the build context.
- Decisions where the spine is silent: image base python:3.13-slim (CI floor; host runs 3.14); local-build image `candybar:local`, no registry push (zero new infra); entrypoints execute inside the container so `~/candybar/jobs` is bind-mounted at the same absolute path (`CANDYBAR_JOBS_DIR` overrides); `CANDYBAR_CGROUP_ROOT=""` in-container; host Postgres reached via `host.docker.internal` (docker-standard alias; `postgres.delo.sh` is edge-only with no TCP route); no OIDC middleware on the route (edge-only posture; the CLI speaks plain HTTP); `127.0.0.1:8700` published for local CLI; hygiene gate scoped to deploy surfaces (tests/CI legitimately use throwaway loopback credentials).
- Prior-story dependency fixes: the 1.7-deferred 30s-drain fixed here (unsubscribe before drain in both pull-consumer roles); `docker stop candybar` now ~1s. Live PG connectivity verified from container on first boot (migrations ran, roles connected).
- Live smoke evidence: register + `candybar run` through https://candybar.delo.sh (202, invocation row `manual|success|0`); scheduled fire landed `scheduled|success|0` off tick:29883209/...210; `docker restart` mid-schedule produced exactly one row per tick (no duplication); Prometheus `candybar` target up, series + fail-zero present, `query_exemplars` returned 3 invocation ids.
- Left running intentionally: the `deploy-smoke` candybar (every-minute, noise=true) is the continuous-operation proof; its script lives at ~/candybar/jobs/deploy-smoke.sh.


- Review patches (step-04, all inline): unsubscribe-failure guard in both roles + test; healthcheck port env; Dockerfile two-phase dep caching; gate span-strip + root guard + mixed-line control; .dockerignore `**/` pycache globs; CI container boot smoke. Post-patch: 273 unit + 3 focused integration pass, ruff clean, gate clean, image rebuilt clean, container redeployed healthy with the new healthcheck.

## Spec Change Log

## Review Triage Log

Deviation: no subagent-spawn capability in this session — the three layers ran
inline (same as stories 1.1-1.8). Blind-hunter floor: kB=48.2, N=floor(sqrt(48.2)+1)=7.

Blind-hunter layer:
- low -> PATCHED: compose healthcheck hardcoded 127.0.0.1:8700 while the port is env-configurable; a CANDYBAR_API_PORT override would silently break it. Healthcheck now reads the env var.
- low -> PATCHED: Dockerfile copied src/ before `uv sync`, busting the dependency layer on every source edit (no uv cache mount). Now two-phase: `--no-install-project` sync of locked deps, then src + project sync.
- medium -> PATCHED: the gate's line-level op:// / ${} whitelist whitewashed a plaintext secret co-located on the same line. Now strips sanctioned SPANS and re-matches the remainder; mixed-line positive control added to the pytest matrix.
- low -> PATCHED: .dockerignore `__pycache__`/`*.pyc` were root-relative — host 3.14 bytecode shipped inside the 3.13 image (verified in the built image). Now `**/__pycache__` / `**/*.pyc`; rebuilt image clean.
- low -> REJECTED: mutable tag pins on base/tool images (uv:0.9.5, python:3.13-slim). Repo convention is tag pins throughout (nats 2.10.29 tarball, prom/prometheus:v3.5.0, postgres:17-alpine); digest-pinning one image makes the posture inconsistent, no named harm beyond the accepted convention.
- low -> REJECTED: fast-stop integration test attaches production durable/stream names, so against a live bus it could race the deployed container for real invoke commands. Requires deliberately aiming the env-gated test at the live bus mid-invoke; a stolen command still executes + acks through the identical code path (same rows/events); parameterizing stream names would add public surface for a test-only concern.
- low -> REJECTED (rule: no findings whose fix edits this build's spec): the Verification section's `docker run candybar:ci candybar --help` predated the migrate-then-exec entrypoint. Corrected as routine agent-owned maintenance to the `--entrypoint` form CI uses.

Edge-case-hunter layer:
- medium -> PATCHED: `sub.unsubscribe()` raises ConnectionClosedError/ConnectionDrainingError/BadSubscriptionError on a dead/draining connection (verified in nats-py 2.16 source) — in the finally block that skipped `nc.drain()` and turned a graceful stop into a role crash. Both roles now catch the precise tuple, warn, and drain; new unit test `test_unsubscribe_failure_does_not_mask_drain`.
- low -> PATCHED: hygiene script `cd "$ROOT"` failure (no arg outside a git repo) silently scanned the CWD — same vacuous-pass class as the missing-rg hole CI caught. Explicit root guard now exits 2.
- Claims check: every Intent/Task claim verified against the live deployment (register + manual run + scheduled fire through https://candybar.delo.sh; restart rejoin with one row per tick; Prometheus target up with stored exemplars). Nothing falsified. Deletion check: nothing meaningful removed.

Verification-gap layer:
- gap -> PATCHED: the container's full boot path (entrypoint migration, all four roles connecting, /healthz, fast stop) was verified only locally — a broken entrypoint would have shipped green CI. New CI step boots `candybar:ci` against the job's postgres+nats on the host network, polls /healthz up to 45s, and asserts `docker stop` < 9500ms.
- NOT A GAP (verified, no finding): durable survival across unsubscribe — nats-py `unsubscribe()` never deletes the consumer (source), and the live bus shows `candybar-executor-invoke` created 04:36 with cumulative delivered=22 across multiple stops using the new path.

## Design Notes

Compose networking: `bloodbank-network` (external) carries the `nats:4222` alias (candystore already consumes Bloodbank this way); the registry lives on the HOST's system Postgres (port 5432, `candybar` db confirmed), reached via `host.docker.internal` — a docker-standard alias, not a LAN IP, matching the spine's intent (monitoring stack precedents). `postgres.delo.sh` resolves only to the Cloudflare edge with no TCP route — unusable for the PG wire protocol.

## Verification

**Commands:**
- `uv run pytest -q` -- all tests pass incl. new healthz/drain/hygiene tests
- `uv run ruff check .` -- clean
- `docker build -t candybar:ci .` -- succeeds; `docker run --rm --entrypoint /app/.venv/bin/candybar candybar:ci --help` exits 0 (the entrypoint migrates first, so CLI smoke bypasses it)
- `bash scripts/check-deploy-hygiene.sh` -- exit 0
- `docker run --rm -v ~/code/33GOD/monitoring/prometheus:/etc/prometheus --entrypoint promtool prom/prometheus:v3.5.0 check config /etc/prometheus/prometheus.yml` -- SUCCESS

**Manual checks (if no CLI):**
- Live smoke: compose up; `curl https://candybar.delo.sh/healthz` → 200; prometheus UI/API shows candybar target up and `query_exemplars` returns ids; `docker restart candybar` → registry shows no duplicate invocation rows for the rejoin window.

## Independent Review (2026-10-26)

Fresh-context reviewer (not the implementer), bmad-code-review over the story's commits and the CURRENT tree (later epics reshaped shared modules). No subagent capability in this session, so the blind / edge-case / verification-gap / acceptance layers ran inline. Evidence classes: implemented + tested = unit suite 749 passed (`uv run pytest -q`), ruff clean; deployed = container rebuilt and redeployed per deploy/README.md after the patches, `/healthz` 200, next tick `tick:29883724` fired `deploy-smoke` (success, 19ms).

### Review Findings

- No `decision-needed`, `patch` or `defer` findings. One container runs all roles on `proxy` + `bloodbank-network` with Traefik labels for candybar.delo.sh; secrets only as `op://` refs; the hygiene gate fails loudly; live: rebuilt and redeployed this session, `/healthz` 200, next tick fired `deploy-smoke`. The 1.3 bus patch (68b878a) closes a fast-stop hole this story's FAST_STOP row assumed: a SIGTERM during a NATS outage now exits instead of hanging into docker's SIGKILL; the 1.6 patch keeps late-fetched commands from outliving the stop grace.
- Rejected: a non-SystemExit crash of the uvicorn serve task would leave the api role waiting on stop — low: uvicorn's bind failure exits via SystemExit (loud), and no other crash path was shown.
