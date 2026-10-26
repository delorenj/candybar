"""The `systemd` runner: start an existing user service and meter it (Story 7.3, AD-18).

A migrated systemd job keeps its `.service` exactly as it is: EnvironmentFile,
hardening, TimeoutStartSec and OnFailure= all stay in force, and only the
`.timer` retires. The executor starts it with `systemctl --user start --wait`
and reads the outcome back from the unit.

Three pieces of plumbing make that work, each reality-checked on systemd 257:

1. The AD-8 env reaches the unit through a once-installed drop-in
   `<unit>.d/50-candybar.conf` (`EnvironmentFile=-%t/candybar/inv/%n.env`). The
   runner rewrites that env file before each start, so there is no
   `daemon-reload` per run; a unit that never reads the env is unaffected.

2. A SUCCESSFUL oneshot is garbage-collected the moment it stops: `show`
   then reports `[not set]` for MemoryPeak/CPUUsageNSec and an empty
   InvocationID, so there is nothing to meter (a failed unit stays loaded; a
   successful one does not). systemd keeps a unit loaded while a unit that
   stays active references it, so the runner registers each unit as
   `After=` of `candybar-unit-holder.service` through a drop-in on that
   long-running unit. `After=` adds the reference and no behaviour: a held unit
   never starts because the holder is up.

3. Accounting is read from the unit after exit: `MemoryPeak` -> the memory
   gauge; `CPUUsageNSec` over the unit's own main-process span
   (`ExecMainExitTimestamp - ExecMainStartTimestamp`, monotonic) -> CPU load;
   `Result` / `ExecMainStatus` -> success or fail; the stderr tail from the
   journal for that `InvocationID`; GPU from pmon pids found in the unit's
   cgroup. Metering is best-effort and reads 0, like the exec runner; the
   verdict never depends on it.

The unit's journal output is stdout and stderr merged (the journal does not
tell them apart), so the "stderr tail" of a failed unit is its last journal
lines. It is fetched only for a failure: a successful unit's chatter is not an
error record.

Overlap: a unit that is already starting or running is refused, never joined
(`systemctl start --wait` on a running unit would wait for, and meter, someone
else's run). Story 7.11 (AD-20) makes the worker probe `unit_busy` BEFORE
anything is published or claimed and record `skipped(reason=overlap)`; the
refusal here stays as the backstop for the check-then-start race and still
records `failed` — loud and rare beats silent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from ..events.builders import STDERR_TAIL_MAX
from ..runners import SystemdRunner
from ..text import scrub_text
from .gpu import GPU_SAMPLER, GpuSampler
from .runner import ProcessRegistry, RunOutcome, _read_result_file, child_env

log = logging.getLogger(__name__)

SYSTEMCTL_ENV = "CANDYBAR_SYSTEMCTL"
JOURNALCTL_ENV = "CANDYBAR_JOURNALCTL"
UNIT_DIR_ENV = "CANDYBAR_SYSTEMD_USER_DIR"

DROPIN_NAME = "50-candybar.conf"
HOLDER_UNIT = "candybar-unit-holder.service"
HOLDER_DROPIN_NAME = "60-held-units.conf"
# `%t` is the runtime dir (/run/user/<uid>), `%n` the full unit name.
DROPIN_TEXT = (
    "# Managed by candybar (Story 7.3, AD-18). The AD-8 invocation env for a\n"
    "# candybar-started run; the runner rewrites this file before each start.\n"
    "[Service]\n"
    "EnvironmentFile=-%t/candybar/inv/%n.env\n"
)
HOLDER_HEADER = (
    "# Managed by candybar (Story 7.3, AD-18). Keeps each listed unit loaded after\n"
    "# it finishes so its accounting survives until the executor reads it.\n"
    "[Unit]\n"
)

WAIT_SLICE_SECONDS = 25.0
SHOW_TIMEOUT_SECONDS = 20.0
JOURNAL_TAIL_LINES = 40
UINT64_MAX = 2**64 - 1
BUSY_STATES = frozenset({"active", "activating", "reloading", "deactivating"})

_INSTALL_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def _install_lock() -> asyncio.Lock:
    """One lock per event loop: installs from concurrent runs must not interleave."""
    loop = asyncio.get_running_loop()
    lock = _INSTALL_LOCKS.get(loop)
    if lock is None:
        lock = _INSTALL_LOCKS[loop] = asyncio.Lock()
    return lock


@dataclass(frozen=True)
class SystemdHost:
    """Where this host keeps its user units and runtime files (all overridable for tests)."""

    systemctl: str = "systemctl"
    journalctl: str = "journalctl"
    unit_dir: Path = field(default_factory=lambda: Path.home() / ".config" / "systemd" / "user")
    runtime_dir: Path = field(default_factory=lambda: Path(f"/run/user/{os.getuid()}"))

    @classmethod
    def from_env(cls) -> SystemdHost:
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        return cls(
            systemctl=os.environ.get(SYSTEMCTL_ENV, "systemctl"),
            journalctl=os.environ.get(JOURNALCTL_ENV, "journalctl"),
            unit_dir=Path(
                os.environ.get(UNIT_DIR_ENV) or Path.home() / ".config" / "systemd" / "user"
            ),
            runtime_dir=Path(runtime),
        )

    @property
    def inv_dir(self) -> Path:
        return self.runtime_dir / "candybar" / "inv"

    def env_file(self, unit: str) -> Path:
        return self.inv_dir / f"{unit}.env"

    def result_file(self, invocation_id: UUID) -> Path:
        return self.inv_dir / f"{invocation_id}.result.json"

    def dropin(self, unit: str) -> Path:
        return self.unit_dir / f"{unit}.d" / DROPIN_NAME

    @property
    def holder_dropin(self) -> Path:
        return self.unit_dir / f"{HOLDER_UNIT}.d" / HOLDER_DROPIN_NAME


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


async def _command(host: SystemdHost, argv: list[str], timeout: float) -> CommandResult:
    """Run one short helper command (show, daemon-reload, journalctl) to completion."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_systemd_env(host),
        )
    except OSError as exc:
        return CommandResult(127, "", f"{type(exc).__name__}: {exc}")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return CommandResult(124, "", f"timed out after {timeout:g}s: {' '.join(argv[:4])}")
    return CommandResult(
        proc.returncode or 0,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


def _systemd_env(host: SystemdHost) -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", str(host.runtime_dir))
    return env


async def show(host: SystemdHost, unit: str, props: list[str]) -> dict[str, str]:
    """`systemctl --user show <unit> -p ...` as a dict ({} when the call fails)."""
    argv = [host.systemctl, "--user", "show", unit]
    for prop in props:
        argv += ["-p", prop]
    result = await _command(host, argv, SHOW_TIMEOUT_SECONDS)
    if result.returncode != 0:
        return {}
    parsed: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            parsed[key] = value
    return parsed


async def unit_busy(host: SystemdHost, unit: str) -> bool | None:
    """The AD-20 overlap probe: True while the unit is starting or running.

    Covers runs this executor did not start (a hand `systemctl start`, or a
    run still going from before an executor restart — the in-process live set
    forgets those). None when the user manager cannot be asked, so the caller
    naks for redelivery instead of skipping (or running) on a guess. A unit
    that is not loaded is NOT busy: the runner's own failure path reports
    that, loudly.
    """
    props = await show(host, unit, ["LoadState", "ActiveState"])
    if not props:
        return None
    if props.get("LoadState") != "loaded":
        return False
    return props.get("ActiveState") in BUSY_STATES


def _atomic_write(path: Path, text: str, mode: int = 0o644) -> bool:
    """Write `text` to `path` unless it already holds exactly that; True when written."""
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    return True


_HELD_RE = re.compile(r"^After=(\S+)$", re.MULTILINE)


def held_units_text(units: set[str]) -> str:
    return HOLDER_HEADER + "".join(f"After={unit}\n" for unit in sorted(units))


def read_held_units(path: Path) -> set[str]:
    try:
        return set(_HELD_RE.findall(path.read_text(encoding="utf-8")))
    except OSError:
        return set()


async def ensure_installed(host: SystemdHost, unit: str) -> bool:
    """Install the env drop-in and the holder reference for `unit`; reload once if either changed.

    Idempotent and cheap when nothing changed (two small reads). The reload is
    the only `daemon-reload` the runner ever issues, and only the first time a
    unit is seen: it does not touch running units. Returns True if it reloaded.
    """
    async with _install_lock():
        changed = _atomic_write(host.dropin(unit), DROPIN_TEXT)
        held = read_held_units(host.holder_dropin)
        if unit not in held:
            held.add(unit)
            changed = _atomic_write(host.holder_dropin, held_units_text(held)) or changed
        if not changed:
            return False
        result = await _command(host, [host.systemctl, "--user", "daemon-reload"], 60.0)
        if result.returncode != 0:
            log.warning("daemon-reload after installing the candybar drop-in failed: %s", result.stderr[:200])
        return True


def _env_file_text(env: dict[str, str]) -> str:
    """EnvironmentFile= lines for the AD-8 vars; values are double-quoted and escaped."""
    lines = []
    for key in sorted(env):
        value = env[key].replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}="{value}"\n')
    return "".join(lines)


def _protocol_env(
    *,
    invocation_id: UUID,
    candybar_id: UUID,
    trigger: str,
    tick_id: str | None,
    result_file: Path,
) -> dict[str, str]:
    full = child_env(
        invocation_id=invocation_id,
        candybar_id=candybar_id,
        trigger=trigger,
        tick_id=tick_id,
        result_file=result_file,
    )
    return {k: v for k, v in full.items() if k in _PROTOCOL_KEYS}


_PROTOCOL_KEYS = frozenset(
    {
        "CANDYBAR_INVOCATION_ID",
        "CANDYBAR_ID",
        "CANDYBAR_TRIGGER",
        "CANDYBAR_TICK_ID",
        "CANDYBAR_RESULT_FILE",
    }
)


def _uint(raw: str | None) -> int | None:
    """A systemd unsigned property, or None when it is `[not set]` / unset / UINT64_MAX."""
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return None if value < 0 or value >= UINT64_MAX else value


def slice_path(slice_name: str) -> str:
    """The cgroup path of a slice: `a-b.slice` lives at `a.slice/a-b.slice`."""
    name = slice_name.removesuffix(".slice")
    if not name or name == "-":
        return ""
    parts = name.split("-")
    return "/".join(f"{'-'.join(parts[: i + 1])}.slice" for i in range(len(parts)))


def manager_cgroup() -> str | None:
    """The user manager's cgroup (`/user.slice/.../user@1000.service`) from our own, or None."""
    try:
        text = Path("/proc/self/cgroup").read_text(encoding="ascii")
    except OSError:
        return None
    match = re.search(r"^0::(/.*?/user@\d+\.service)", text, re.MULTILINE)
    return match.group(1) if match else None


def unit_cgroup_dir(slice_name: str, unit: str, *, fs_root: str = "/sys/fs/cgroup") -> Path | None:
    """Where the unit's cgroup appears while it runs, or None when we cannot tell."""
    manager = manager_cgroup()
    if manager is None or not slice_name:
        return None
    return Path(fs_root + manager) / slice_path(slice_name) / unit


def cpu_load(cpu_nsec: int | None, span_us: int) -> float:
    """Average busy cores over the unit's own main-process span; 0 when unmeasured."""
    if not cpu_nsec or span_us <= 0:
        return 0.0
    return round((cpu_nsec / 1000) / span_us, 4)


async def _journal_tail(host: SystemdHost, invocation_id: str) -> str:
    result = await _command(
        host,
        [
            host.journalctl,
            "--user",
            "--no-pager",
            "-o",
            "cat",
            "-n",
            str(JOURNAL_TAIL_LINES),
            f"_SYSTEMD_INVOCATION_ID={invocation_id}",
        ],
        SHOW_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return ""
    return scrub_text(result.stdout.strip()[-STDERR_TAIL_MAX:])


MAIN_PROPS = [
    "Result",
    "ExecMainCode",
    "ExecMainStatus",
    "ExecMainStartTimestampMonotonic",
    "ExecMainExitTimestampMonotonic",
    "CPUUsageNSec",
    "MemoryPeak",
    "InvocationID",
    "ActiveState",
]


def _early_failure(message: str, *, started_at: datetime, t0: float) -> RunOutcome:
    return RunOutcome(
        exit_code=None,
        runtime_ms=int((time.monotonic() - t0) * 1000),
        stderr_tail="",
        result=None,
        result_error=None,
        spawn_error=message[:STDERR_TAIL_MAX],
        scoped=False,
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )


async def run_systemd(
    *,
    runner: SystemdRunner,
    invocation_id: UUID,
    candybar_id: UUID,
    trigger: str,
    tick_id: str | None,
    registry: ProcessRegistry | None = None,
    on_wait: Callable[[], Awaitable[None]] | None = None,
    gpu: GpuSampler | None = None,
    host: SystemdHost | None = None,
) -> RunOutcome:
    """Start the unit, wait for it, and read its outcome and accounting back."""
    host = host or SystemdHost.from_env()
    unit = runner.unit
    started_at = datetime.now(UTC)
    t0 = time.monotonic()

    pre = await show(host, unit, ["LoadState", "ActiveState", "SubState", "Slice"])
    if not pre:
        return _early_failure(
            f"could not query systemd for {unit} (is the user manager reachable?)",
            started_at=started_at,
            t0=t0,
        )
    if pre.get("LoadState") != "loaded":
        return _early_failure(
            f"unit {unit} is not loaded (LoadState={pre.get('LoadState')})", started_at=started_at, t0=t0
        )
    if pre.get("ActiveState") in BUSY_STATES:
        return _early_failure(
            f"unit {unit} is still running (ActiveState={pre.get('ActiveState')}, "
            f"SubState={pre.get('SubState')}); overlapping start refused",
            started_at=started_at,
            t0=t0,
        )

    result_path = host.result_file(invocation_id)
    env_path = host.env_file(unit)
    try:
        await ensure_installed(host, unit)
        host.inv_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(host.inv_dir, 0o700)
        env_path.write_text(
            _env_file_text(
                _protocol_env(
                    invocation_id=invocation_id,
                    candybar_id=candybar_id,
                    trigger=trigger,
                    tick_id=tick_id,
                    result_file=result_path,
                )
            ),
            encoding="utf-8",
        )
        os.chmod(env_path, 0o600)
    except OSError as exc:
        return _early_failure(
            f"could not prepare the invocation env for {unit}: {exc}", started_at=started_at, t0=t0
        )

    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                host.systemctl,
                "--user",
                "start",
                "--wait",
                unit,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_systemd_env(host),
                start_new_session=True,
            )
        except OSError as exc:
            return _early_failure(f"{type(exc).__name__}: {exc}", started_at=started_at, t0=t0)

        scope = None
        with contextlib.suppress(Exception):
            scope = unit_cgroup_dir(pre.get("Slice", ""), unit)
        gpu_session = (GPU_SAMPLER if gpu is None else gpu).track(proc.pid, scope)
        if registry is not None:
            registry.add(proc)
        try:
            try:
                communicate = asyncio.create_task(proc.communicate())
                while not communicate.done():
                    await asyncio.wait({communicate}, timeout=WAIT_SLICE_SECONDS)
                    if not communicate.done() and on_wait is not None:
                        await on_wait()
                _out, err = communicate.result()
            finally:
                gpu_load = gpu_session.finish()
        finally:
            if registry is not None:
                registry.procs.discard(proc)
        runtime_ms = int((time.monotonic() - t0) * 1000)
        rc = proc.returncode if proc.returncode is not None else 1
        client_stderr = scrub_text(err.decode("utf-8", errors="replace").strip())

        props = await show(host, unit, MAIN_PROPS)
        result_file, result_error = _read_result_file(result_path)
        return await _outcome(
            host=host,
            unit=unit,
            rc=rc,
            client_stderr=client_stderr,
            props=props,
            result=result_file,
            result_error=result_error,
            runtime_ms=runtime_ms,
            gpu_load=gpu_load,
            started_at=started_at,
        )
    finally:
        with contextlib.suppress(OSError):
            env_path.unlink()
        with contextlib.suppress(OSError):
            result_path.unlink()


async def _outcome(
    *,
    host: SystemdHost,
    unit: str,
    rc: int,
    client_stderr: str,
    props: dict[str, str],
    result: object,
    result_error: str | None,
    runtime_ms: int,
    gpu_load: float,
    started_at: datetime,
) -> RunOutcome:
    verdict = props.get("Result", "")
    invocation = props.get("InvocationID", "")
    start_us = _uint(props.get("ExecMainStartTimestampMonotonic")) or 0
    exit_us = _uint(props.get("ExecMainExitTimestampMonotonic")) or 0
    ran = bool(invocation) or start_us > 0
    memory_peak = _uint(props.get("MemoryPeak")) or 0
    span_us = exit_us - start_us if exit_us > start_us > 0 else runtime_ms * 1000
    load = cpu_load(_uint(props.get("CPUUsageNSec")), span_us)

    exit_code: int | None
    detail: str | None = None
    spawn_error: str | None = None
    failed_run = rc != 0 or (verdict not in ("", "success") and ran)
    if not failed_run:
        exit_code = 0
    elif ran:
        status = _uint(props.get("ExecMainStatus"))
        exit_code = status if verdict == "exit-code" and status else (rc or 1)
        detail = f"unit {unit} failed: Result={verdict or 'unknown'}"
        if verdict == "exit-code" and status is not None:
            detail += f", exit status {status}"
    else:
        exit_code = None
        spawn_error = scrub_text(
            (client_stderr or f"systemctl start --wait exited {rc}").replace("\n", " ")[:300]
        )

    stderr_tail = ""
    if failed_run:
        if invocation:
            stderr_tail = await _journal_tail(host, invocation)
        stderr_tail = stderr_tail or client_stderr[-STDERR_TAIL_MAX:]

    return RunOutcome(
        exit_code=exit_code,
        runtime_ms=runtime_ms,
        stderr_tail=scrub_text(stderr_tail[-STDERR_TAIL_MAX:]),
        result=result,  # type: ignore[arg-type]
        result_error=result_error,
        spawn_error=spawn_error,
        scoped=ran and memory_peak > 0,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        memory_peak_bytes=memory_peak,
        cpu_load=load,
        gpu_load=gpu_load,
        detail=detail,
    )


__all__ = [
    "DROPIN_NAME",
    "HOLDER_UNIT",
    "SystemdHost",
    "cpu_load",
    "ensure_installed",
    "run_systemd",
    "show",
    "slice_path",
    "unit_cgroup_dir",
]
