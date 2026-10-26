"""Daemon role composition: one asyncio process, roles selected by flag (AD-2).

The daemon is the single deployable. Roles land with their owning stories:
heartbeat (1.3) today; dispatcher (1.5) and executor (1.6) register here as
they arrive; the api role is Epic 5. Roles share one stop event and run under
one TaskGroup: if any role crashes, the process exits rather than running
half-alive.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .heartbeat.role import run_heartbeat

RoleFn = Callable[[asyncio.Event], Awaitable[None]]

ROLES: dict[str, RoleFn] = {
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
    async with asyncio.TaskGroup() as tg:
        for role in roles:
            tg.create_task(ROLES[role](stop), name=f"candybar-role-{role}")
