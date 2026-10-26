# Candybar daemon image (AD-15 deployment envelope).
#
# One image, all roles: the entrypoint applies the registry migrations and
# execs `candybar daemon` (default role composition: heartbeat + dispatcher +
# executor + api). Built locally by deploy/compose.yml (`candybar:local`) —
# the homelab pulls nothing from a registry. Base is python:3.13-slim to
# match the CI floor (the spine pins >=3.13; the host toolchain is 3.14).

FROM python:3.13-slim AS build

# uv resolves from the committed lockfile — the image gets exactly the
# spine-pinned stack, no re-resolution.
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /bin/

WORKDIR /app
# Deps first, project second: source edits must not bust the dependency
# layer (no uv cache mount, so a busted layer re-downloads everything).
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-editable --no-install-project
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable
# Story 4.1: candybar-sdk (zero deps, stdlib only) goes into the same venv,
# whose bin/ leads PATH — so a job's `#!/usr/bin/env python3` can
# `import candybar_sdk`. The daemon itself never imports it.
COPY sdks/python/ ./sdks/python/
RUN uv pip install --python /app/.venv/bin/python --no-deps --no-cache ./sdks/python

FROM python:3.13-slim

# psycopg (pure-python wheel, per the spine pin) dlopens libpq at runtime;
# the slim base does not ship it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 candybar
WORKDIR /app

COPY --from=build /app/.venv /app/.venv
# Alembic migrations live outside the wheel; the entrypoint needs them.
COPY deploy/alembic.ini ./deploy/alembic.ini
COPY deploy/alembic/ ./deploy/alembic/
COPY deploy/container-entrypoint.sh /usr/local/bin/container-entrypoint

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    # Compose injects the real values; these keep bare `docker run` sane.
    CANDYBAR_API_HOST=0.0.0.0 \
    CANDYBAR_API_PORT=8700

USER candybar
EXPOSE 8700

# Migrate-then-serve: the daemon is the registry's sole writer (AD-10), so
# boot-time `upgrade head` can never race another writer. `exec` in the
# entrypoint makes the daemon PID 1's child directly, so SIGTERM from
# `docker stop` reaches the asyncio roles.
ENTRYPOINT ["container-entrypoint"]
CMD []
