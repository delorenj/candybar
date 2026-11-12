# deploy/mac: Candybar on macOS (exec-runner-only profile)

The Mac peer install (Damian's Mac Studio, `aperture/mirror` PLAN.md W5) runs the
same Candybar as big-chungus with three named differences:

| piece | big-chungus | macOS profile |
|---|---|---|
| control plane (heartbeat, dispatcher, api) | `deploy/compose.yml`, Traefik edge, host Postgres | `deploy/compose.yml` + [`compose.mac.yml`](../compose.mac.yml) on Colima: no Traefik, no `proxy` network, no Linux mounts, loopback port `127.0.0.1:8700` |
| registry | host system Postgres | a dedicated PG17 container (`candybar-postgres`, loopback `127.0.0.1:5435`), never seeded from another host's registry |
| executor | `systemd --user` unit with cgroup v2 metering | `launchd` LaunchAgent `io.33god.candybar-executor`, `CANDYBAR_CGROUP_ROOT=""`, **exec runner only** |
| steward | `candybar-steward.service` | not installed |
| AD-22 watchdog | `candybar-watchdog.timer` (page-only) | **none, by decision** (below) |

## Runner kinds on the Mac

Only `exec` is supported in this profile.

- Register jobs in the table form with absolute `argv`:
  `[candybar.runner] kind = "exec"`, `argv = ["/abs/path", ...]`.
- A bare `entrypoint = "..."` manifest is refused by the API (422): the container
  validates the path against its own filesystem, and the `/home/delorenj/candybar/jobs`
  mount that made that work on Linux is removed here.
- `systemd` has no meaning on macOS. `hermes` and `n8n` need the Hermes CLI and an
  n8n webhook and are deferred to canaries; do not register them in W5.
- Jobs start with `HOME` as their working directory unless the manifest sets `workdir`
  (same as cron and systemd --user), and inherit the executor's explicit `PATH`.
- Invocations run unscoped: memory and CPU meter as 0 (`cost` reads "unmeasured"),
  GPU load is off (`CANDYBAR_NVIDIA_SMI=""`). Execution never depends on metering.

## Files

| file | role |
|---|---|
| `../compose.mac.yml` | overlay on `compose.yml`; every removal is an explicit `!reset` / `!override` and is asserted by `tests/test_compose_mac.py` |
| `io.33god.candybar-executor.plist.template` | LaunchAgent: `RunAtLoad` + `KeepAlive` only, explicit `PATH`, loopback metrics, no secret |
| `run-executor.sh` | the plist's program: forces `CANDYBAR_CGROUP_ROOT=""`, injects `PGPASSWORD` through `env-op-resolve`, `exec`s `candybar daemon --roles executor` |
| `install-executor.sh` | renders the plist for one domain (`--dry-run` prints it), lints it before replacing the old one, optionally (re)bootstraps it (`--load`) |
| `executor.env.op`, `compose.env.op` | `op://` references only, resolved from the interim 0600 store |

## Stopping the executor

`launchctl bootout` (and `kickstart -k`) send SIGTERM to the executor and wait
`ExitTimeOut` (60 s, the Linux unit's `TimeoutStopSec`) before SIGKILL. On SIGTERM the
executor SIGTERMs every in-flight child (each job runs in its own process group, so the
signal reaches the job's tree), waits for them and records them failed. Linux's
`KillMode=mixed` additionally sweeps the cgroup; macOS has none. So a job that ignores
SIGTERM for the whole 60 s survives the SIGKILLed executor as an untracked orphan. Before
an owner-window bootout, check `candybar ls` / `candybar history` for a long-running job
that traps SIGTERM.

## `CANDYBAR_CGROUP_ROOT=""`

The variable must be **set and empty**, not unset: unset falls back to
`/sys/fs/cgroup/candybar`. With it empty, `run_invocation` never creates a scope and
never opens `cgroup.procs`, `memory.peak` or `cpu.stat`, and nothing reads
`/proc/self/cgroup`. The only reader of `/proc/self/cgroup` in `src/` is the
`systemd` runner (`manager_cgroup`), a runner kind this profile never uses
(`tests/test_mac_profile.py` pins both facts, and that the Mac wrapper forces the
value even if the environment carries another). The Linux wrapper
`deploy/host/run-executor.sh` already skips its own `/proc/self/cgroup` read when the
variable is set empty; that is also tested.

## AD-22: no watchdog on the Mac

Operator decision (2026-11-12): **no watchdog is shipped for the Mac.** This is a
recorded decision, not an exception. `deploy/watchdog/` is a `systemd --user` timer
and is Linux-only; nothing in `deploy/mac/` replaces it, and nothing may be added
that polls Candybar on a schedule from outside it (that would be a second clock;
a launchd `StartInterval` is exactly what `doctor --live` flags). What the Mac has
instead is the supervision it already gets for free:

- `KeepAlive` restarts the executor if it dies; Colima's `restart: unless-stopped`
  restarts the control plane.
- `curl -s http://127.0.0.1:8700/healthz` (api) and `curl -s http://127.0.0.1:8701/healthz`
  (executor) answer on demand, and `candybar next` / `candybar history <name>` show
  whether fires are landing.
- `candybar doctor --repo . --live --json` (Darwin collector) lists any competing
  launchd, cron or Hermes clock. There is deliberately no scheduled run of it here;
  a Candybar-owned `scheduling-policy-drift` candybar can run it once W5-3 has the
  clock proven.

If the owner later wants Mac alerting, the owner records the exact schedule, owner,
scope and reason in `deploy/scheduling-policy.json` first (README "Exceptions");
until then the answer is none.

## Doctor on macOS

`uv run candybar doctor --repo . --live --json` on a Mac audits `launchd` instead
of systemd: plists in `~/Library/LaunchAgents`, `/Library/LaunchAgents` and
`/Library/LaunchDaemons`, the loaded services of `gui/<uid>`, `user/<uid>` and `system`
and their enable/disable overrides (`launchctl print`, `print-disabled`; read-only verbs
only; an `enabled` override beats a plist's `Disabled`), the user crontab
(`crontab -l`), every Hermes profile and the registry. `StartInterval` and
`StartCalendarInterval` on an enabled or loaded job are reported as bypass clocks
with the exact id `launchd:<label>`. One bad plist never aborts the inventory: a plist
with no `Label` (real Macs carry empty `<dict/>` stubs such as
`com.citrolabs.keystone.agent.plist`; launchd cannot load a job without one) becomes a
per-file row keyed `launchd:file:<path>`, which is a finding only if it carries a clock
key; a plist that cannot be parsed or read becomes a per-file `unreadable` row, which makes
the report `unknown` (exit 2, it cannot be certified clock-free) while every other job is
still audited and listed. `WatchPaths`, `KeepAlive`, `RunAtLoad` and
`ThrottleInterval` are supervision or events and are never flagged. Findings are
observations: other people's launchd jobs are listed, never disabled. Every loaded job
is probed with `launchctl print <domain>/<label>` so the live definition is audited, not just
the file on disk (an edited plist whose job was never booted out still fires); any probe
failure other than "service not found" makes the report `unknown`. One stated coverage
boundary, in the report's `not_audited`: loaded jobs with no plist in the scanned
directories whose label starts with `com.apple.`, `application.`, `0x`, `[` or
`UIKitApplication:` (OS-owned or runtime-generated) are not probed. That is a disclosure,
not an approval, and no exception is implied. Other users' domains are not audited either. Exit codes are unchanged: 0 clean, 1 bypass, 2 an inventory source
unavailable.

## W5-2 install recipe

Prerequisites (earlier waves): Colima up; project `33god-bus` up (network
`bloodbank-network`, NATS alias `nats`, `127.0.0.1:4222`, streams
`BLOODBANK_COMMANDS` present); `env-op-resolve` and `~/.config/33god/launchd-agent.env`
in place; mise python 3.14 and uv. No Homebrew libpq: the Darwin lock installs `psycopg-binary` (below).

All commands over ssh use `/usr/local/bin/docker --context colima` explicitly and
the PATH from `~/.config/33god/launchd-agent.env`.

1. **Checkout.** In `~/code/33GOD/candybar`: `git --no-optional-locks status --porcelain`
   must be empty, then `git fetch origin && git merge --ff-only origin/main`. Record the SHA.
2. **Locked env.** `uv sync --frozen`. The lock carries `psycopg-binary==3.3.6` for
   `sys_platform == 'darwin'` only, so psycopg uses the libpq bundled in the wheel and
   never dlopens a system library (Linux keeps the system libpq). Prove it, with no
   `DYLD_*` variable and no Homebrew libpq involved:
   `.venv/bin/python -c "import psycopg; print(psycopg.pq.__impl__, psycopg.pq.version())"`
   must print `binary` and a libpq version number; `python` means the binary wheel did not
   install (stop and report). Why not Homebrew's keg-only libpq: it is outside dyld's default
   search path and the only way to point at it is `DYLD_FALLBACK_LIBRARY_PATH`, which SIP strips
   across `/bin/sh` and `/usr/bin/env` (the resolver's shebang), so a launchd job never sees it.
   Before any `uv` command in the checkout run `mise trust --show` there. On this Mac the
   checkout's `mise.toml` was **untrusted** (2026-11-12), and then the mise shims (`uv`,
   `python3`) exit 1 inside that directory with "Config files ... are not trusted". Read
   `mise.toml`, then either `mise trust ~/code/33GOD/candybar/mise.toml` (record it; undo with
   `mise trust --untrust ~/code/33GOD/candybar/mise.toml`) or call uv by its absolute mise
   install path. The LaunchAgent does not need the trust: `run-executor.sh` puts `.venv/bin`
   first on PATH so the resolver's `python3` is the venv python, never a shim.
3. **Secret.** Generate a registry password on the Mac straight into the store, never
   printed. `env-op-resolve key op://DeLoSecrets/candybar-studio-registry/password` prints the
   store key (`OP_DELOSECRETS_CANDYBAR_STUDIO_REGISTRY_PASSWORD`); append `KEY=<value>` to
   `~/.config/33god/secrets.env` (0600) and add the matching `refmap.tsv` row, exactly as
   W3-1 did for `CANDYSTORE_DB_PASSWORD`. Back both files up first.
4. **Control plane + registry.**
   ```sh
   cd ~/code/33GOD/candybar
   env-op-resolve exec --env-file deploy/mac/compose.env.op -- \
     /usr/local/bin/docker --context colima compose -p candybar \
       -f deploy/compose.yml -f deploy/compose.mac.yml up -d --build candybar-postgres
   # watch `docker --context colima info` stay responsive, then:
   env-op-resolve exec --env-file deploy/mac/compose.env.op -- \
     /usr/local/bin/docker --context colima compose -p candybar \
       -f deploy/compose.yml -f deploy/compose.mac.yml up -d --build candybar
   ```
   The first `--build` compiles `candybar:local` for linux/arm64 (unproven on this Mac
   until you run it). Bring Postgres up first, the app second, one at a time.
5. **Migrations.** The container entrypoint runs `alembic -c deploy/alembic.ini upgrade head`
   on every boot, so step 4 migrates. To run it by hand (or to prove it from the host):
   ```sh
   env-op-resolve exec --env-file deploy/mac/executor.env.op -- \
     env DATABASE_URL=postgresql+psycopg://candybar@127.0.0.1:5435/candybar \
     uv run alembic -c deploy/alembic.ini upgrade head
   ```
   Proof: `docker --context colima exec candybar-postgres psql -U candybar -d candybar -tAc "select version_num from alembic_version"`
   prints the head of `deploy/alembic/versions` (see the handoff for the value at a given SHA).
6. **Executor LaunchAgent.**
   ```sh
   deploy/mac/install-executor.sh --dry-run        # read the plist first
   deploy/mac/install-executor.sh --load           # writes ~/Library/LaunchAgents, plutil -lint, bootstraps gui/<uid>
   ```
   Check both domains for the label before `print`/`kickstart`/`bootout`
   (`launchctl print gui/$(id -u)/io.33god.candybar-executor`, then `user/...`);
   `--domain user` bootstraps into `user/<uid>` instead; the installer renders exactly one
   `LimitLoadToSessionType` for the domain it is given (gui is `Aqua`, user is `Background`), so
   a later login can never load a second copy next to the first. Re-running it with `--load`
   lints the new plist before replacing the old one, boots out this one label if it is loaded
   in the target domain, and refuses (exit 73, nothing written) if it is loaded in the other.
   The executor sits at the schema guard until step 5 has run, then opens by itself.
7. **CLI on PATH.** `ln -s ~/code/33GOD/candybar/.venv/bin/candybar ~/.local/bin/candybar`
   (only if no `candybar` exists there already; check first). The CLI's default API is
   `http://127.0.0.1:8700`.
8. **Prove it.** `curl -s http://127.0.0.1:8700/healthz` (`{"status":"ok"}`),
   `curl -s http://127.0.0.1:8701/healthz` (`{"status":"ok"}`), the executor log line
   `schema guard open: registry head <head> matches this checkout`, `candybar ls`
   (empty registry, no error), `uv run candybar doctor --repo . --live --json` on the Mac.
   Reboot and logout persistence is an owner-window proof.

### Contracts at the W5-1 commit

- **API health:** `GET http://127.0.0.1:8700/healthz` returns `{"status":"ok"}`. It does not
  touch the database or the bus by design; it is liveness only. Container healthcheck
  `python -c urllib...` hits the same URL every 30 s.
- **Executor health:** `GET http://127.0.0.1:8701/healthz` returns `{"status":"ok"}`;
  `/metrics` on the same port. Bound by `CANDYBAR_EXECUTOR_METRICS_HOST|PORT` (an empty
  port disables it; a bind failure is logged and the executor runs on). **It does not
  report the schema revision.** The revision proof is the registry's `alembic_version`
  plus the executor's `schema guard open: registry head ...` log line.
- **Schema guard:** the executor consumes nothing while `alembic_version.version_num` differs
  from the single head of its own checkout's `deploy/alembic/versions`. It fails closed: if
  that directory is unreadable or has more than one head, the process refuses to start. It
  rechecks every 5 s while closed and every 30 s while open, and logs one warning on close
  (`schema guard CLOSED: registry head is X but this checkout expects Y`). The guard needs a
  checkout, which is why the executor runs from the repo `.venv`, not from the image.
  `CANDYBAR_SCHEMA_GUARD=off` skips it (tests only).
- **Ordering:** container first (it migrates), then the executor. A reversed order only waits.
- **Ports (loopback only):** `8700` api, `8701` executor metrics, `5435` registry Postgres,
  `4222` NATS (owned by `33god-bus`).
- **Durable:** the executor attaches `candybar-executor-invoke` on `BLOODBANK_COMMANDS`
  (filter `bloodbank.cmd.cron.candybar.invoke`); it is created by the executor.

## Rollback

Every step is additive and in its own namespace. Boot the executor out
(`launchctl bootout <domain>/<uid>/io.33god.candybar-executor`), delete the plist (the
installer keeps a copy of any file it replaced under `$CANDYBAR_BACKUP_DIR`), then
`docker --context colima compose -p candybar -f deploy/compose.yml -f deploy/compose.mac.yml stop`
(never `down -v`, never a prune). The registry volume `candybar_registry_pgdata` is the only
data; leave it unless the owner asks for it to go. Remove the store key and `refmap.tsv` row
only after restoring their backups.

## Not on the Mac

Steward, watchdog, Traefik route, Prometheus scrape (`proxy`), the `systemd` unit holder,
`host/install.sh`, `candybar-watch.service`, the shared jobs tree, cgroup metering, GPU
metering, Hermes and n8n runner canaries (deferred), and any copy of big-chungus's
registry (its rows carry `/home/delorenj` argv).
