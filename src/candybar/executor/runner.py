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

Story 3.1 meters the scope (metering.py): memory.peak and the cpu.stat delta
are read right after the child exits and before the scope is removed.
Unscoped runs meter as zero.

The child joins its scope BEFORE exec (deferred-work: pre-exec cgroup join).
The parent opens the scope's cgroup.procs ahead of the spawn, and a
`preexec_fn` in the forked child writes "0" (meaning "me") to that fd, so
every page and CPU cycle of the exec'd image is charged to the scope from its
first instruction. The old post-spawn join (the parent writing the pid after
spawn returned) lost the race to any job shorter than the ~20 ms the
migration can block on `cgroup_threadgroup_rwsem`: the child was already
exiting, the kernel skipped it silently, and the run metered 0/0. The fork
already pays that wait now, inside the child, before exec.

`preexec_fn` is documented as unsafe in a threaded parent, because the forked
child can deadlock on a lock another thread held. The function here takes
no lock: it is a single `os.write` on an inherited fd with an `OSError`
guard. It allocates nothing that needs the GIL beyond what the forking thread
already holds, and it never logs or imports.

Story 3.2 adds GPU load (gpu.py): the child's process tree is tracked on the
shared pmon sampler from spawn to exit. That runs on GPU-less hosts too,
where tracking is a no-op that reads 0.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
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
from ..text import scrub_text
from . import metering
from .gpu import GPU_SAMPLER, GpuSampler

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
    # Executor-measured via nvidia-smi pmon (Story 3.2); 0 when GPU-less.
    gpu_load: float = 0.0
    # Why a nonzero exit failed, when the runner knows better than the code
    # (a timeout, a systemd unit's Result); used as the failure message.
    detail: str | None = None


@dataclass
class ProcessRegistry:
    """Live child processes, so the role can SIGTERM them all on shutdown."""

    procs: set[asyncio.subprocess.Process] = field(default_factory=set)
    stopping: bool = False

    async def terminate_all(self) -> None:
        self.stopping = True
        for proc in list(self.procs):
            self.terminate(proc)

    def add(self, proc: asyncio.subprocess.Process) -> None:
        """Track a freshly spawned child; SIGTERM it at once if stop already began.

        terminate_all sweeps only the children registered when it runs. A
        spawn that was in flight then (or started after) would otherwise run
        to completion during shutdown, past docker's stop grace, and be
        SIGKILLed unrecorded — so a late child is signalled on arrival.
        """
        self.procs.add(proc)
        if self.stopping:
            self.terminate(proc)

    @staticmethod
    def terminate(proc: asyncio.subprocess.Process, sig: int = signal.SIGTERM) -> None:
        if proc.returncode is not None:
            return
        try:
            # Children spawn in their own session: kill the whole
            # group so grandchildren can't hold the pipes open.
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.send_signal(sig)
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
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """The AD-8 env contract, layered over the daemon's own environment.

    `extra` is an exec runner's non-secret literals; they sit between the
    daemon's environment and the protocol vars, so the protocol always wins.
    """
    env = dict(os.environ)
    if extra:
        env.update(extra)
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


def _prepare_scope(root: str, invocation_id: UUID) -> tuple[Path, int] | None:
    """Best-effort cgroup v2 scope: mkdir it and open its cgroup.procs for the child.

    Returns the scope and a write fd on its cgroup.procs (close-on-exec, so it
    never leaks into the job), or None to run unscoped.
    """
    if not root:
        return None
    scope = Path(root) / str(invocation_id)
    try:
        scope.parent.mkdir(parents=True, exist_ok=True)
        metering.enable_controllers(scope.parent)
        scope.mkdir(exist_ok=True)
        # O_CREAT only matters for the plain-fs test double; cgroupfs has the file.
        fd = os.open(scope / "cgroup.procs", os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o644)
    except OSError as exc:
        log.warning("cgroup scope %s unavailable (%s); running unscoped", scope, exc)
        shutil.rmtree(scope, ignore_errors=True)
        return None
    return scope, fd


def _join_scope_preexec(fd: int) -> Callable[[], None]:
    """The forked child's last step before exec: move itself into the scope.

    Writing "0" to cgroup.procs migrates the writing process. A failed write
    leaves the child where it was (unscoped, metering 0) and never fails the
    spawn: execution must not depend on cgroup privilege.
    """

    def join() -> None:
        try:
            os.write(fd, b"0")
        except OSError:
            pass

    return join


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
        return None, scrub_text(f"invalid result file: {exc}"[:STDERR_TAIL_MAX])


async def run_invocation(
    *,
    invocation_id: UUID,
    candybar_id: UUID,
    trigger: str,
    tick_id: str | None,
    entrypoint: str | None = None,
    argv: list[str] | None = None,
    workdir: str | None = None,
    env: dict[str, str] | None = None,
    timeout_s: float | None = None,
    registry: ProcessRegistry | None = None,
    on_wait: Callable[[], Awaitable[None]] | None = None,
    root: str | None = None,
    gpu: GpuSampler | None = None,
) -> RunOutcome:
    """Spawn the job, wait for exit, and gather the AD-8 outcome.

    Two shapes share one spawn path. `entrypoint` is the bare-executable exec
    default: argv is just that path, run from its own directory. `argv` (+
    `workdir`, `env`, `timeout_s`) is the Story 7.3 exec runner: run from
    `workdir`, or the user's home when unset (the directory cron and
    `systemd --user` give their jobs), killed after `timeout_s`.
    """
    if (entrypoint is None) == (argv is None):
        raise ValueError("run_invocation needs exactly one of entrypoint and argv")
    if entrypoint is not None:
        argv = [entrypoint]
        workdir = str(Path(entrypoint).parent)
    elif workdir is None:
        workdir = str(Path.home())
    assert argv is not None
    scope_root = cgroup_root() if root is None else root
    work_dir = Path(tempfile.mkdtemp(prefix=f"{invocation_id}-", dir=_ensure_work_root()))
    result_path = work_dir / "result.json"
    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    scope: Path | None = None
    prepared = _prepare_scope(scope_root, invocation_id)
    procs_fd: int | None = None
    if prepared is not None:
        scope, procs_fd = prepared
    try:
        # The scope is fresh and empty: its usage before the child joins is
        # the baseline (0 on a real cgroup).
        cpu_baseline = metering.read_cpu_usage_usec(scope)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workdir,
                env=child_env(
                    invocation_id=invocation_id,
                    candybar_id=candybar_id,
                    trigger=trigger,
                    tick_id=tick_id,
                    result_file=result_path,
                    extra=env,
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group: SIGTERM reaches the tree
                preexec_fn=_join_scope_preexec(procs_fd) if procs_fd is not None else None,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
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
        finally:
            # The child holds its own copy until exec closes it (O_CLOEXEC).
            if procs_fd is not None:
                os.close(procs_fd)
                procs_fd = None
        gpu_session = (GPU_SAMPLER if gpu is None else gpu).track(proc.pid, scope)
        if registry is not None:
            registry.add(proc)
        timed_out = False
        try:
            try:
                communicate = asyncio.create_task(proc.communicate())
                deadline = None if timeout_s is None else t0 + timeout_s
                while not communicate.done():
                    # Bounded wait on the pipes — never a sleep-until (AD-1).
                    slice_s = WAIT_SLICE_SECONDS
                    if deadline is not None:
                        slice_s = max(min(slice_s, deadline - time.monotonic()), 0.0)
                    await asyncio.wait({communicate}, timeout=slice_s)
                    if communicate.done():
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        timed_out = True
                        await _stop_timed_out(proc, communicate)
                        break
                    if on_wait is not None:
                        await on_wait()
                if communicate.done():
                    stdout, stderr = communicate.result()
                else:
                    # Killed, but a grandchild in another session still holds
                    # the pipes: take what we have rather than wait on it.
                    communicate.cancel()
                    await asyncio.gather(communicate, return_exceptions=True)
                    stdout, stderr = b"", b""
                    await proc.wait()
            finally:
                # Always end the GPU session (even on cancellation), so the
                # shared pmon stops once nothing is in flight.
                gpu_load = gpu_session.finish()
            # The child has exited; the scope (still present) holds its totals.
            memory_peak = metering.read_memory_peak(scope)
            cpu_used = metering.read_cpu_usage_usec(scope) - cpu_baseline
            if stdout:
                log.debug("invocation %s stdout tail: %s", invocation_id, stdout[-200:])
        finally:
            if registry is not None:
                registry.procs.discard(proc)

        runtime_ms = int((time.monotonic() - t0) * 1000)
        # NUL survives errors="replace" and Postgres text rejects it (scrub_text).
        stderr_tail = scrub_text(stderr.decode("utf-8", errors="replace")[-STDERR_TAIL_MAX:])
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
            gpu_load=gpu_load,
            detail=f"timed out after {timeout_s:g}s and was killed" if timed_out else None,
        )
    finally:
        _leave_scope(scope)
        shutil.rmtree(work_dir, ignore_errors=True)


TIMEOUT_KILL_GRACE_SECONDS = 10.0


async def _stop_timed_out(proc: asyncio.subprocess.Process, communicate: asyncio.Task) -> None:
    """SIGTERM the job's process group, then SIGKILL it if it outlives the grace."""
    ProcessRegistry.terminate(proc)
    await asyncio.wait({communicate}, timeout=TIMEOUT_KILL_GRACE_SECONDS)
    if not communicate.done():
        ProcessRegistry.terminate(proc, signal.SIGKILL)
        await asyncio.wait({communicate}, timeout=TIMEOUT_KILL_GRACE_SECONDS)


def _ensure_work_root() -> str:
    root = work_root()
    root.mkdir(parents=True, exist_ok=True)
    return str(root)
