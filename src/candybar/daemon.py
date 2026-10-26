"""Daemon role composition: one asyncio process, roles selected by flag (AD-2).

The daemon is the single deployable. Roles land with their owning stories:
heartbeat (1.3), api (1.4), dispatcher (1.5), and executor (1.6) today;
the api role grows its CAP-8 surface in Epic 5.
Roles share one stop event and run under one TaskGroup: if any role crashes,
the process exits rather than running half-alive.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .api.role import run_api
from .dispatcher.role import run_dispatcher
from .executor.role import run_executor
from .heartbeat.role import run_heartbeat

log = logging.getLogger(__name__)

RoleFn = Callable[[asyncio.Event], Awaitable[None]]

ROLES: dict[str, RoleFn] = {
    "api": run_api,
    "dispatcher": run_dispatcher,
    "executor": run_executor,
    "heartbeat": run_heartbeat,
}


def available_roles() -> list[str]:
    return sorted(ROLES)


def resolve_roles(requested: str) -> list[str]:
    """Parse a --roles value into role names; raise ValueError on unknown ones."""
    if requested == "all":
        return available_roles()
    selected = list(dict.fromkeys(r.strip() for r in requested.split(",") if r.strip()))
    if not selected:
        raise ValueError("no roles selected (use 'all' or a comma-separated role list)")
    unknown = [r for r in selected if r not in ROLES]
    if unknown:
        raise ValueError(
            f"unknown role(s): {', '.join(unknown)} (available: {', '.join(available_roles())})"
        )
    return selected


async def run_daemon(roles: list[str], stop: asyncio.Event) -> None:
    """Run the selected roles concurrently until `stop` is set or one crashes."""
    if "executor" in roles and "api" not in roles:
        # /metrics rides the api role's server (Story 1.7): an executor-only
        # process records series nobody can scrape.
        log.warning("executor role running without api role: /metrics is not exposed")
    async with asyncio.TaskGroup() as tg:
        for role in roles:
            tg.create_task(ROLES[role](stop), name=f"candybar-role-{role}")
