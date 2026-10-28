"""Pick the runner for a run target and fire the on_failure hook (Story 7.3, AD-18).

`run_target` is the executor's default `run`: the worker hands it either the
bare `entrypoint` path of a pre-Epic-7 candybar, or the stored runner dict
(`exec`, `systemd`, the Story 7.4 `hermes` runner, or the `n8n` runner).
Everything below it returns a `RunOutcome`, so a bad stored runner or a unit
that will not start fails through the same record path as any failed run.

`run_on_failure` is the hook: an argv run once, best-effort and unmetered,
after a terminal `failed` has been recorded, with the AD-8 protocol env plus
`CANDYBAR_FAILURE_MESSAGE`. It never raises, never blocks the executor longer
than `HOOK_TIMEOUT_SECONDS`, and is skipped (with a warning) for a systemd
runner whose unit already has `OnFailure=`: that unit alerts for itself, and a
second alert would double-page.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from ..events.builders import STDERR_TAIL_MAX
from ..runners import (
    ExecRunner,
    HermesRunner,
    N8nRunner,
    RunnerError,
    SystemdRunner,
    parse_runner,
)
from ..text import scrub_text
from . import n8n_runner
from .gpu import GpuSampler
from .hermes_runner import hermes_argv, kill_after_seconds, parse_result_line
from .runner import ProcessRegistry, RunOutcome, child_env, run_invocation
from .systemd_runner import SystemdHost, run_systemd, show

log = logging.getLogger(__name__)

HOOK_TIMEOUT_SECONDS = 60.0
FAILURE_MESSAGE_ENV = "CANDYBAR_FAILURE_MESSAGE"


async def run_target(
    *,
    invocation_id: UUID,
    candybar_id: UUID,
    trigger: str,
    tick_id: str | None,
    entrypoint: str | None = None,
    runner: dict | None = None,
    registry: ProcessRegistry | None = None,
    on_wait: Callable[[], Awaitable[None]] | None = None,
    root: str | None = None,
    gpu: GpuSampler | None = None,
    host: SystemdHost | None = None,
) -> RunOutcome:
    """Run a candybar's work as its runner (or its bare entrypoint) says."""
    common = {
        "invocation_id": invocation_id,
        "candybar_id": candybar_id,
        "trigger": trigger,
        "tick_id": tick_id,
        "registry": registry,
        "on_wait": on_wait,
        "gpu": gpu,
    }
    if runner is None:
        if entrypoint is None:
            raise ValueError("run_target needs an entrypoint or a runner")
        return await run_invocation(entrypoint=entrypoint, root=root, **common)  # type: ignore[arg-type]

    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    try:
        parsed = parse_runner(runner)
    except RunnerError as exc:
        # Only reachable by editing the row by hand: registration validates.
        return RunOutcome(
            exit_code=None,
            runtime_ms=int((time.monotonic() - t0) * 1000),
            stderr_tail="",
            result=None,
            result_error=None,
            spawn_error=f"invalid runner: {exc}"[:STDERR_TAIL_MAX],
            scoped=False,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
    if isinstance(parsed, SystemdRunner):
        return await run_systemd(runner=parsed, host=host, **common)  # type: ignore[arg-type]
    if isinstance(parsed, HermesRunner):
        return await run_invocation(
            argv=hermes_argv(parsed),
            timeout_s=kill_after_seconds(parsed),
            stdout_result=parse_result_line,
            root=root,
            **common,  # type: ignore[arg-type]
        )
    if isinstance(parsed, N8nRunner):
        return await run_invocation(
            argv=n8n_runner.n8n_argv(parsed),
            timeout_s=n8n_runner.kill_after_seconds(parsed),
            root=root,
            **common,  # type: ignore[arg-type]
        )
    assert isinstance(parsed, ExecRunner)
    return await run_invocation(
        argv=parsed.argv,
        workdir=parsed.workdir,
        env=parsed.env,
        timeout_s=parsed.timeout_s,
        root=root,
        **common,  # type: ignore[arg-type]
    )


async def run_on_failure(
    runner: dict,
    *,
    invocation_id: UUID,
    candybar_id: UUID,
    trigger: str,
    tick_id: str | None,
    message: str | None,
    host: SystemdHost | None = None,
) -> None:
    """Run the runner's `on_failure` argv once, best-effort. Never raises."""
    argv = runner.get("on_failure")
    if not argv:
        return
    try:
        if runner.get("kind") == "systemd":
            props = await show(host or SystemdHost.from_env(), str(runner.get("unit")), ["OnFailure"])
            if props.get("OnFailure", "").strip():
                log.warning(
                    "on_failure hook skipped for %s: the unit already has OnFailure=%s",
                    runner.get("unit"),
                    props["OnFailure"].strip(),
                )
                return
        env = child_env(
            invocation_id=invocation_id,
            candybar_id=candybar_id,
            trigger=trigger,
            tick_id=tick_id,
            result_file=Path(os.devnull),
            extra=runner.get("env") if isinstance(runner.get("env"), dict) else None,
        )
        env[FAILURE_MESSAGE_ENV] = scrub_text(message or "")
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=runner.get("workdir") or str(Path.home()),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            _out, err = await asyncio.wait_for(proc.communicate(), HOOK_TIMEOUT_SECONDS)
        except TimeoutError:
            ProcessRegistry.terminate(proc)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5.0)
            ProcessRegistry.terminate(proc, 9)
            log.warning("on_failure hook for invocation %s timed out and was killed", invocation_id)
            return
        if proc.returncode != 0:
            log.warning(
                "on_failure hook for invocation %s exited %s: %s",
                invocation_id,
                proc.returncode,
                err.decode("utf-8", errors="replace")[-200:],
            )
    except Exception:  # best-effort by definition: the failure is already recorded
        log.exception("on_failure hook for invocation %s crashed", invocation_id)


__all__ = ["FAILURE_MESSAGE_ENV", "HOOK_TIMEOUT_SECONDS", "run_on_failure", "run_target"]
