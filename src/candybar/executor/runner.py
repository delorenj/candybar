"""Spawn one invocation as a cgroup v2 scoped subprocess (AD-7, AD-8).

The child speaks the AD-8 process protocol: context in via CANDYBAR_* env
vars, result out as JSON at CANDYBAR_RESULT_FILE. The executor never owns a
clock (AD-1): the only waits here are bounded waits on the child's stdout/
stderr pipes, with an `on_wait` hook (the bus message's `in_progress()`)
after each timeout slice.

cgroup scoping is best-effort by design: when the cgroupfs root is missing,
read-only, or undelegated (dev boxes, CI, unprivileged containers), the run
proceeds unscoped with a warning — execution must not fail on missing cgroup
privilege. Set CANDYBAR_CGROUP_ROOT="" to disable scoping outright.

Story 3.1 meters the scope (metering.py): the cpu.stat baseline is taken right
after the child joins it, memory.peak and the cpu.stat delta right after the
child exits and before the scope is removed. Unscoped runs meter as zero.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ValidationError

from ..events.builders import STDERR_TAIL_MAX
from ..events.envelope import SelfReportedMetrics
from . import metering

log = logging.getLogger(__name__)

CGROUP_ROOT_ENV = "CANDYBAR_CGROUP_ROOT"
WORK_ROOT_ENV = "CANDYBAR_WORK_ROOT"
DEFAULT_CGROUP_ROOT = "/sys/fs/cgroup/candybar"
WAIT_SLICE_SECONDS = 25.0


class ResultFile(BaseModel):
    """The AD-8 result JSON. Unknown keys are ignored (reader generosity).

    `metrics` accepts only what a child may self-report (token_spend,
    cost_usd); memory/CPU are executor-measured and never taken from it.
    """

    status: Literal["success", "fail"]
    message: str | None = None
    metrics: SelfReportedMetrics = SelfReportedMetrics()


@dataclass(frozen=True)
class RunOutcome:
    """What one spawn attempt produced; result resolution happens in worker.py."""

    exit_code: int | None  # None when the child never spawned
    runtime_ms: int
    stderr_tail: str
    result: ResultFile | None
    result_error: str | None  # why a present result file was unusable
    spawn_error: str | None
    scoped: bool  # whether the child ran inside its cgroup v2 scope
    started_at: datetime
    finished_at: datetime
    # Executor-measured from the cgroup scope (Story 3.1); 0 when unmeasured.
    memory_peak_bytes: int = 0
    cpu_load: float = 0.0


@dataclass
class ProcessRegistry:
    """Live child processes, so the role can SIGTERM them all on shutdown."""

    procs: set[asyncio.subprocess.Process] = field(default_factory=set)
    stopping: bool = False

    async def terminate_all(self) -> None:
        self.stopping = True
        for proc in list(self.procs):
            if proc.returncode is None:
                try:
                    # Children spawn in their own session: kill the whole
                    # group so grandchildren can't hold the pipes open.
                    os.killpg(proc.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass  # exited between the check and the signal


def cgroup_root() -> str:
    return os.environ.get(CGROUP_ROOT_ENV, DEFAULT_CGROUP_ROOT)


def work_root() -> Path:
    return Path(os.environ.get(WORK_ROOT_ENV, Path(tempfile.gettempdir()) / "candybar"))


def child_env(
    *,
    invocation_id: UUID,
    candybar_id: UUID,
    trigger: str,
    tick_id: str | None,
    result_file: Path,
) -> dict[str, str]:
    """The AD-8 env contract, layered over the daemon's own environment."""
    env = dict(os.environ)
    env.update(
        {
            "CANDYBAR_INVOCATION_ID": str(invocation_id),
            "CANDYBAR_ID": str(candybar_id),
            "CANDYBAR_TRIGGER": trigger,
            # Manual invokes carry no tick; the var always exists (empty then).
            "CANDYBAR_TICK_ID": tick_id or "",
            "CANDYBAR_RESULT_FILE": str(result_file),
        }
    )
    return env


def _enter_scope(root: str, invocation_id: UUID, pid: int) -> Path | None:
    """Best-effort cgroup v2 scope: mkdir the scope, move the child in."""
    if not root:
        return None
    scope = Path(root) / str(invocation_id)
    try:
        scope.parent.mkdir(parents=True, exist_ok=True)
        metering.enable_controllers(scope.parent)
        scope.mkdir(exist_ok=True)
        (scope / "cgroup.procs").write_text(str(pid))
    except OSError as exc:
        log.warning("cgroup scope %s unavailable (%s); running unscoped", scope, exc)
        shutil.rmtree(scope, ignore_errors=True)
        return None
    return scope


def _leave_scope(scope: Path | None) -> None:
    if scope is None:
        return
    try:
        scope.rmdir()  # succeeds once the child has exited
    except OSError:
        # Plain-fs test double: the written cgroup.procs is a real file that
        # blocks rmdir (on cgroupfs it is virtual and never does).
        try:
            procs = scope / "cgroup.procs"
            if procs.is_file():
                procs.unlink()
            scope.rmdir()
        except OSError as exc:
            log.warning("could not remove cgroup scope %s: %s", scope, exc)


def _read_result_file(path: Path) -> tuple[ResultFile | None, str | None]:
    """Parse the AD-8 result file; (None, None) when the child wrote none."""
    if not path.exists():
        return None, None
    try:
        raw = path.read_text(encoding="utf-8")
        return ResultFile.model_validate(json.loads(raw)), None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        return None, f"invalid result file: {exc}"[:STDERR_TAIL_MAX]


async def run_invocation(
    *,
    entrypoint: str,
    invocation_id: UUID,
    candybar_id: UUID,
    trigger: str,
    tick_id: str | None,
    registry: ProcessRegistry | None = None,
    on_wait: Callable[[], Awaitable[None]] | None = None,
    root: str | None = None,
) -> RunOutcome:
    """Spawn the entrypoint, wait for exit, and gather the AD-8 outcome."""
    scope_root = cgroup_root() if root is None else root
    work_dir = Path(tempfile.mkdtemp(prefix=f"{invocation_id}-", dir=_ensure_work_root()))
    result_path = work_dir / "result.json"
    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    scope: Path | None = None
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                entrypoint,
                cwd=str(Path(entrypoint).parent),
                env=child_env(
                    invocation_id=invocation_id,
                    candybar_id=candybar_id,
                    trigger=trigger,
                    tick_id=tick_id,
                    result_file=result_path,
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group: SIGTERM reaches the tree
            )
        except (OSError, ValueError) as exc:
            return RunOutcome(
                exit_code=None,
                runtime_ms=int((time.monotonic() - t0) * 1000),
                stderr_tail="",
                result=None,
                result_error=None,
                spawn_error=f"{type(exc).__name__}: {exc}"[:STDERR_TAIL_MAX],
                scoped=False,
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        scope = _enter_scope(scope_root, invocation_id, proc.pid)
        cpu_baseline = metering.read_cpu_usage_usec(scope)
        if registry is not None:
            registry.procs.add(proc)
        try:
            communicate = asyncio.create_task(proc.communicate())
            while not communicate.done():
                # Bounded wait on the pipes — never a sleep-until (AD-1).
                await asyncio.wait({communicate}, timeout=WAIT_SLICE_SECONDS)
                if not communicate.done() and on_wait is not None:
                    await on_wait()
            stdout, stderr = communicate.result()
            # The child has exited; the scope (still present) holds its totals.
            memory_peak = metering.read_memory_peak(scope)
            cpu_used = metering.read_cpu_usage_usec(scope) - cpu_baseline
            if stdout:
                log.debug("invocation %s stdout tail: %s", invocation_id, stdout[-200:])
        finally:
            if registry is not None:
                registry.procs.discard(proc)

        runtime_ms = int((time.monotonic() - t0) * 1000)
        stderr_tail = stderr.decode("utf-8", errors="replace")[-STDERR_TAIL_MAX:]
        result, result_error = _read_result_file(result_path)
        return RunOutcome(
            exit_code=proc.returncode,
            runtime_ms=runtime_ms,
            stderr_tail=stderr_tail,
            result=result,
            result_error=result_error,
            spawn_error=None,
            scoped=scope is not None,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            memory_peak_bytes=memory_peak,
            cpu_load=metering.cpu_load(cpu_used, runtime_ms),
        )
    finally:
        _leave_scope(scope)
        shutil.rmtree(work_dir, ignore_errors=True)


def _ensure_work_root() -> str:
    root = work_root()
    root.mkdir(parents=True, exist_ok=True)
    return str(root)
