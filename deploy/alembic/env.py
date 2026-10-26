"""Alembic env for the candybar registry.

DSN resolution: DATABASE_URL env var (ecosystem convention) via
candybar.registry.store, so migrations and the daemon never disagree about
where the registry lives. Migrations run sync over psycopg (v3).
Run as the `candybar` role so every created object is owner-only (AD-10:
the daemon is the sole writer; this env issues no GRANTs).
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from candybar.registry.models import Base
from candybar.registry.store import database_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with create_engine(database_url()).connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
