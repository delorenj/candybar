"""Per-invocation average GPU load from `nvidia-smi pmon` (AD-9, Story 3.2).

The executor, never the child, measures GPU load. One process-wide sampler
runs `nvidia-smi pmon -d 1 -s u -o T` while at least one invocation is in
flight. Concurrent invocations share that single pmon process, which stops
when the last one finishes. Each pmon sample block lists the SM utilization
(%) of every process using a GPU.

A PID counts toward an invocation when it belongs to the invocation's process
tree: its session id is the child's pid (the runner spawns every child with
`start_new_session`, so the whole tree shares that sid), or it sits in the
invocation's cgroup scope (which also catches a descendant that called
setsid). Per sample, the member PIDs' SM% are summed and divided by 100, so
1.0 means one GPU's SMs are saturated; two GPUs at 100% read 2.0.

`gpu_load` is the mean over the samples taken while the invocation was
tracked. The block still open at finish counts too, so a 1–2 s job still gets
its burst. An invocation that is never sampled reads 0.

GPU-less is the common case and is silent. If nvidia-smi is absent (or
`CANDYBAR_NVIDIA_SMI=""`), pmon is never spawned and every invocation meters
0 with no log line. If pmon exists but fails (no driver, unsupported device,
non-NVIDIA), it warns once and disables the sampler for the process lifetime.
Nothing here raises into an invocation.

pmon's 1 s cadence belongs to the external sampler, not to the executor. The
executor owns no clock (AD-1): it only reads pmon's stdout.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

NVIDIA_SMI_ENV = "CANDYBAR_NVIDIA_SMI"
DEFAULT_NVIDIA_SMI = "nvidia-smi"
# 1 s samples (pmon's minimum), utilization only, with a time column used to
# split the scrolling output into sample blocks.
PMON_ARGS: tuple[str, ...] = ("pmon", "-d", "1", "-s", "u", "-o", "T")
GPU_LOAD_DECIMALS = 4
# Wall time sampling may add to one invocation's critical path (track +
# finish; pmon spawn/teardown run off it). Asserted in tests/test_gpu.py.
GPU_SAMPLING_BUDGET_MS = 50.0
PROC_ROOT = Path("/proc")

_AUTO = object()


@dataclass(eq=False)
class GpuSession:
    """One invocation's GPU accounting. `finish()` returns its average load."""

    pid: int  # the child: session leader of the invocation's process tree
    scope: Path | None
    sampler: GpuSampler | None  # None: GPU metering unavailable, reads 0
    total: float = 0.0  # summed load of the closed sample blocks
    samples: int = 0
    pending: float = 0.0  # contribution to the currently open block
    result: float | None = None

    def finish(self) -> float:
        """Stop tracking and return the average GPU load (0 when unmeasured)."""
        if self.result is not None:
            return self.result
        if self.sampler is None:
            self.result = 0.0
            return 0.0
        try:
            self.result = self.sampler._finish(self)
        except Exception:  # metering never fails an invocation
            log.debug("gpu session finish failed for pid %s", self.pid, exc_info=True)
            self.result = 0.0
        return self.result


@dataclass(eq=False)
class _PmonRun:
    """One pmon process lifetime: spawned for the first session and stopped
    when the last session finishes."""

    proc: asyncio.subprocess.Process | None = None
    task: asyncio.Task[None] | None = None
    stopping: bool = False
    data_lines: int = 0


@dataclass(eq=False)
class _Block:
    """The pmon sample block currently being read."""

    open: bool = False
    time: str | None = None
    keys: set[tuple[str, str]] = field(default_factory=set)
    sids: dict[int, int | None] = field(default_factory=dict)
    scopes: dict[int, frozenset[int]] = field(default_factory=dict)  # id(session) -> pids


def parse_sid(stat_text: str) -> int | None:
    """The session id from a /proc/<pid>/stat line (field 6; comm may hold ')')."""
    fields = stat_text.rpartition(")")[2].split()
    try:
        return int(fields[3])  # state ppid pgrp session ...
    except (IndexError, ValueError):
        return None


def resolve_nvidia_smi() -> str | None:
    """The nvidia-smi to sample with, or None when GPU metering is off/absent."""
    configured = os.environ.get(NVIDIA_SMI_ENV)
    if configured is not None and not configured.strip():
        return None
    return shutil.which(configured or DEFAULT_NVIDIA_SMI)


class GpuSampler:
    """A shared pmon sampler attributing per-pid SM% to tracked invocations."""

    def __init__(self, binary: str | None | object = _AUTO, *, proc_root: Path = PROC_ROOT) -> None:
        self._binary = binary
        self._proc_root = proc_root
        self._sessions: dict[GpuSession, None] = {}  # insertion-ordered set
        self._run: _PmonRun | None = None
        self._block = _Block()
        self._cols: dict[str, int] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self.disabled = False
        self._warned_exit = False
        self.pmon_starts = 0  # observability for tests: one pmon per busy period

    # -- public -----------------------------------------------------------

    def binary(self) -> str | None:
        """Resolve (once) which nvidia-smi to run; None means GPU-less."""
        if self._binary is _AUTO:
            self._binary = resolve_nvidia_smi()
        return self._binary  # type: ignore[return-value]

    def track(self, pid: int, scope: Path | None = None) -> GpuSession:
        """Start attributing GPU samples to the process tree led by `pid`.

        Synchronous and cheap. When pmon is not already running, its spawn is
        scheduled as a background task, so the invocation never waits for it.
        """
        try:
            binary = None if self.disabled else self.binary()
            if binary is None:
                return GpuSession(pid, scope, None)
            session = GpuSession(pid, scope, self)
            self._sessions[session] = None
            if self._run is None:
                self._start(binary)
            return session
        except Exception:
            log.debug("gpu tracking unavailable for pid %s", pid, exc_info=True)
            return GpuSession(pid, scope, None)

    async def aclose(self) -> None:
        """Stop pmon and wait for its reader (tests, shutdown)."""
        for session in list(self._sessions):
            session.finish()
        self._stop_run()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # -- pmon lifecycle ----------------------------------------------------

    def _start(self, binary: str) -> None:
        run = _PmonRun()
        self._run = run
        self._block = _Block()
        self.pmon_starts += 1
        run.task = asyncio.get_running_loop().create_task(self._pump(run, binary))
        self._tasks.add(run.task)
        run.task.add_done_callback(self._tasks.discard)

    def _stop_run(self) -> None:
        run = self._run
        self._run = None
        self._block = _Block()
        if run is None:
            return
        run.stopping = True
        if run.proc is not None and run.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                run.proc.terminate()

    async def _pump(self, run: _PmonRun, binary: str) -> None:
        if run.stopping:
            return  # every session finished before pmon got going: skip the spawn
        try:
            run.proc = await asyncio.create_subprocess_exec(
                binary,
                *PMON_ARGS,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, ValueError) as exc:
            self._disable(f"cannot start {binary} pmon: {exc}")
            if self._run is run:
                self._run = None
            return
        try:
            if run.stopping:  # every session finished while pmon was spawning
                return
            assert run.proc.stdout is not None
            async for raw in run.proc.stdout:
                if run is not self._run:
                    break  # stopped or superseded: discard late lines
                self._feed(run, raw.decode("utf-8", errors="replace"))
        except Exception:
            log.debug("gpu pmon reader failed", exc_info=True)
        finally:
            if run.proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    run.proc.terminate()
            rc = await run.proc.wait()
            if not run.stopping:
                if run.data_lines == 0:
                    self._disable(f"{binary} pmon exited ({rc}) without reporting a sample")
                else:
                    # At most one warning per process (spec): a pmon that keeps
                    # dying would otherwise log on every restart.
                    level = logging.DEBUG if self._warned_exit else logging.WARNING
                    self._warned_exit = True
                    log.log(level, "nvidia-smi pmon exited (%s); GPU sampling resumes on the next invocation", rc)
            if self._run is run:
                self._run = None
                self._block = _Block()

    def _disable(self, reason: str) -> None:
        if not self.disabled:
            self.disabled = True
            log.warning("GPU metering disabled for this process: %s; gpu_load meters as 0", reason)

    # -- sample attribution ------------------------------------------------

    def _feed(self, run: _PmonRun, line: str) -> None:
        """Consume one pmon output line."""
        if not line.strip():
            return
        if line.startswith("#"):
            tokens = line[1:].split()
            if "pid" in tokens and "sm" in tokens:  # the name row, not the units row
                self._cols = {name: i for i, name in enumerate(tokens)}
            return
        cols = self._cols
        if cols is None:
            return
        tokens = line.split()
        try:
            pid_tok = tokens[cols["pid"]]
            sm_tok = tokens[cols["sm"]]
            gpu_tok = tokens[cols["gpu"]] if "gpu" in cols else "0"
            time_tok = tokens[cols["Time"]] if "Time" in cols else None
        except IndexError:
            return
        run.data_lines += 1
        block = self._block
        key = (gpu_tok, pid_tok)
        # A new sample starts when the timestamp moves, or when a (gpu, pid)
        # repeats inside one second (pmon cadence drift).
        if block.open and (time_tok != block.time or key in block.keys):
            self._close_block()
            block = self._block
        block.open = True
        block.time = time_tok
        block.keys.add(key)
        if pid_tok == "-" or sm_tok == "-":
            return  # empty sample or no SM activity: counts as a 0 sample
        try:
            pid = int(pid_tok)
            sm = min(max(float(sm_tok), 0.0), 100.0)
        except ValueError:
            return
        if sm <= 0:
            return
        for session in self._sessions:
            if self._member(session, pid):
                session.pending += sm / 100.0

    def _close_block(self) -> None:
        for session in self._sessions:
            session.total += session.pending
            session.samples += 1
            session.pending = 0.0
        self._block = _Block()

    def _member(self, session: GpuSession, pid: int) -> bool:
        if self._sid(pid) == session.pid:
            return True
        if session.scope is not None:
            return pid in self._scope_pids(session)
        return False

    def _sid(self, pid: int) -> int | None:
        sids = self._block.sids
        if pid not in sids:
            try:
                text = (self._proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
                sids[pid] = parse_sid(text)
            except OSError:
                sids[pid] = None
        return sids[pid]

    def _scope_pids(self, session: GpuSession) -> frozenset[int]:
        scopes = self._block.scopes
        key = id(session)
        if key not in scopes:
            pids: frozenset[int] = frozenset()
            if session.scope is not None:
                try:
                    raw = (session.scope / "cgroup.procs").read_text(encoding="ascii")
                    pids = frozenset(int(tok) for tok in raw.split() if tok.isdigit())
                except (OSError, ValueError):
                    pass
            scopes[key] = pids
        return scopes[key]

    def _finish(self, session: GpuSession) -> float:
        self._sessions.pop(session, None)
        total, samples = session.total, session.samples
        if self._block.open:  # the block in flight counts as one sample
            total += session.pending
            samples += 1
        if not self._sessions:
            self._stop_run()
        if samples == 0:
            return 0.0
        return round(total / samples, GPU_LOAD_DECIMALS)


# Process-default sampler: every invocation in this executor process shares it.
GPU_SAMPLER = GpuSampler()

__all__ = [
    "DEFAULT_NVIDIA_SMI",
    "GPU_LOAD_DECIMALS",
    "GPU_SAMPLER",
    "GPU_SAMPLING_BUDGET_MS",
    "NVIDIA_SMI_ENV",
    "PMON_ARGS",
    "GpuSampler",
    "GpuSession",
    "parse_sid",
    "resolve_nvidia_smi",
]
