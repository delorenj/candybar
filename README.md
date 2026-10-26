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

CI runs ruff + pytest on GitHub-hosted runners on every push to `main`.
