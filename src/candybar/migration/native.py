"""Native schedulers a job leaves: probe, retire, restore (Story 7.5, AD-21).

Three kinds, matching the cron inventory's sources:

- `systemd-timer`: a `systemd --user` `.timer`. Active = the timer unit is
  active; retired = not active and not enabled (no `timers.target.wants`
  link, so a reboot cannot bring it back). Next/last fire come from
  `systemctl --user list-timers --output=json`, which reports realtime
  microseconds for calendar and monotonic timers alike; a native run is "in
  progress" while the timer's service is active.
- `crontab`: one exact user-crontab line. Active = the line is present.
  Fires come from the line's cron read in the entry's timezone.
- `hermes`: a Hermes cron job. Active = enabled and not pinned
  `scheduler: external`; retired = pinned external (Story 7.4: the ticker
  never fires it, a `cron run` trigger keeps it external). Fires come from
  the job record's `next_run_at` / `last_run_at`.

Every shell command runs as `bash -euo pipefail -c '<cmd>'` through an
injected runner, so tests drive a fake host.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..dispatcher.schedule import ONE_MINUTE, next_fire, previous_fire
from ..timezones import resolve_timezone
from .ledger import KIND_CRONTAB, KIND_HERMES, KIND_SYSTEMD_TIMER, NativeScheduler

COMMAND_TIMEOUT_S = 300
HERMES_BIN = '"$HOME/.local/bin/hermes"'
# A service in one of these states means a native run is in progress.
RUNNING_STATES = frozenset(
    {"active", "activating", "deactivating", "reloading", "refreshing"}
)
ENABLED_STATES = frozenset({"enabled", "enabled-runtime"})


@dataclass(frozen=True)
class ShellResult:
    rc: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


Shell = Callable[[str], ShellResult]
ReadFile = Callable[[str], str]


def run_shell(command: str, *, timeout: float = COMMAND_TIMEOUT_S) -> ShellResult:
    """Run one ledger command exactly as committed: `bash -euo pipefail -c`."""
    try:
        done = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ShellResult(rc=124, err=f"timed out after {timeout:.0f}s")
    except OSError as exc:
        return ShellResult(rc=127, err=str(exc))
    return ShellResult(rc=done.returncode, out=done.stdout, err=done.stderr)


def read_file(path: str) -> str:
    return Path(path).expanduser().read_text(encoding="utf-8")


@dataclass(frozen=True)
class NativeState:
    """What the native scheduler is doing right now."""

    active: bool  # it would fire the job
    retired: bool  # it can never fire the job again on its own
    next_fire: datetime | None = None  # UTC
    last_fire: datetime | None = None  # UTC
    running: bool = False  # a native run is in progress
    detail: str = ""


def _q(value: str) -> str:
    return shlex.quote(value)


def _usec(value: object) -> datetime | None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1_000_000, UTC)


def _utc_minute(instant: datetime) -> datetime:
    return instant.astimezone(UTC).replace(second=0, microsecond=0)


def cron_fires(
    cron: str, timezone: str, now: datetime
) -> tuple[datetime | None, datetime | None]:
    """(previous fire at or before `now`, next fire strictly after `now`), UTC."""
    tz = resolve_timezone(timezone)
    floor = _utc_minute(now)
    prev = previous_fire(cron, floor + ONE_MINUTE, tz=tz)
    try:
        nxt = next_fire(cron, floor + ONE_MINUTE, tz=tz)
    except OverflowError:
        nxt = None
    return prev, nxt


# --------------------------------------------------------------------------
# Probes.


def _probe_systemd(native: NativeScheduler, shell: Shell) -> NativeState:
    unit = _q(native.unit or "")
    enabled = shell(f"systemctl --user is-enabled {unit} || true").out.strip()
    active = shell(f"systemctl --user is-active {unit} || true").out.strip()
    listed = shell(f"systemctl --user list-timers --all --output=json {unit}")
    entry: dict = {}
    if listed.ok:
        try:
            rows = json.loads(listed.out or "[]")
        except json.JSONDecodeError:
            rows = []
        entry = next(
            (r for r in rows if isinstance(r, dict) and r.get("unit") == native.unit),
            {},
        )
    running = False
    service = entry.get("activates")
    if isinstance(service, str) and service:
        state = shell(f"systemctl --user is-active {_q(service)} || true").out.strip()
        running = state in RUNNING_STATES
    is_active = active == "active"
    return NativeState(
        active=is_active,
        retired=not is_active and enabled not in ENABLED_STATES,
        next_fire=_usec(entry.get("next")) if is_active else None,
        last_fire=_usec(entry.get("last")),
        running=running,
        detail=f"{native.unit}: is-enabled={enabled or '?'} is-active={active or '?'}"
        + (f", {service} running" if running else ""),
    )


def _probe_crontab(native: NativeScheduler, shell: Shell, now: datetime) -> NativeState:
    listed = shell("crontab -l 2>/dev/null || true")
    lines = [line.rstrip() for line in listed.out.splitlines()]
    present = (native.line or "").rstrip() in lines
    prev, nxt = cron_fires(native.cron or "", native.timezone or "UTC", now)
    return NativeState(
        active=present,
        retired=not present,
        next_fire=nxt if present else None,
        last_fire=prev,
        detail=f"crontab line {'present' if present else 'absent'}",
    )


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _probe_hermes(
    native: NativeScheduler, read: ReadFile, now: datetime
) -> NativeState:
    try:
        raw = json.loads(read(native.jobs_file or ""))
    except (OSError, json.JSONDecodeError) as exc:
        return NativeState(
            active=False, retired=False, detail=f"jobs file unreadable: {exc}"
        )
    jobs = raw.get("jobs", []) if isinstance(raw, dict) else raw
    job = next(
        (j for j in jobs if isinstance(j, dict) and j.get("id") == native.job_id), None
    )
    if job is None:
        return NativeState(
            active=False,
            retired=False,
            detail=f"job {native.job_id} not in {native.jobs_file}",
        )
    external = job.get("scheduler") == "external"
    enabled = bool(job.get("enabled")) and not job.get("paused_at")
    next_fire = _parse_iso(job.get("next_run_at"))
    if next_fire is None and native.cron and native.timezone:
        next_fire = cron_fires(native.cron, native.timezone, now)[1]
    return NativeState(
        active=enabled and not external,
        retired=external,
        next_fire=next_fire if enabled and not external else None,
        last_fire=_parse_iso(job.get("last_run_at")),
        running=job.get("state") == "running",
        detail=f"hermes {native.profile}:{native.job_id} enabled={enabled} "
        f"scheduler={job.get('scheduler') or 'internal'}",
    )


def probe(
    native: NativeScheduler,
    *,
    shell: Shell = run_shell,
    read: ReadFile = read_file,
    now: datetime | None = None,
) -> NativeState:
    instant = now or datetime.now(UTC)
    if native.kind == KIND_SYSTEMD_TIMER:
        return _probe_systemd(native, shell)
    if native.kind == KIND_CRONTAB:
        return _probe_crontab(native, shell, instant)
    if native.kind == KIND_HERMES:
        return _probe_hermes(native, read, instant)
    raise ValueError(
        f"unknown native kind {native.kind!r}"
    )  # pragma: no cover - Literal


# --------------------------------------------------------------------------
# Default commands (written into the ledger by `plan --write`).


def unit_file_of(unit: str, shell: Shell = run_shell) -> str | None:
    """The resolved absolute path of a unit's file, or None when systemd has none."""
    shown = shell(f"systemctl --user show -p FragmentPath --value {_q(unit)}")
    path = shown.out.strip() if shown.ok else ""
    return os.path.realpath(path) if path else None


def default_commands(native: NativeScheduler) -> tuple[list[str], list[str]]:
    """(disable, rollback) for a native kind: the exact commands to commit."""
    if native.kind == KIND_SYSTEMD_TIMER:
        unit = _q(native.unit or "")
        # Enable by path: `disable` of a linked unit removes its link, after
        # which `enable <name>` fails (reality-checked on systemd 257).
        target = _q(native.unit_file) if native.unit_file else unit
        return (
            [f"systemctl --user disable --now {unit}"],
            [f"systemctl --user enable --now {target}"],
        )
    if native.kind == KIND_CRONTAB:
        line = _q(native.line or "")
        return (
            [f"crontab -l | {{ grep -vxF -- {line} || true; }} | crontab -"],
            [
                (
                    f"crontab -l 2>/dev/null | grep -qxF -- {line} || "
                    f"{{ {{ crontab -l 2>/dev/null || true; printf '%s\\n' {line}; }} | crontab -; }}"
                )
            ],
        )
    if native.kind == KIND_HERMES:
        prefix = f"{HERMES_BIN} -p {_q(native.profile or '')} cron edit {_q(native.job_id or '')}"
        return ([f"{prefix} --scheduler external"], [f"{prefix} --scheduler internal"])
    raise ValueError(
        f"unknown native kind {native.kind!r}"
    )  # pragma: no cover - Literal


def unit_on_failure(service: str, shell: Shell = run_shell) -> str:
    """A service's `OnFailure=` list (empty when none): a candybar `on_failure`
    hook must not double its alerting (AD-18)."""
    shown = shell(f"systemctl --user show -p OnFailure --value {_q(service)}")
    return shown.out.strip() if shown.ok else ""


def fmt(instant: datetime | None, timezone: str | None = None) -> str:
    """`2026-10-27T09:23:00Z (05:23 America/New_York)`, or `-`."""
    if instant is None:
        return "-"
    utc = instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not timezone or timezone == "UTC":
        return utc
    local = instant.astimezone(resolve_timezone(timezone))
    return f"{utc} ({local:%a %H:%M} {timezone})"


def seconds(delta: timedelta) -> int:
    return int(delta.total_seconds())


__all__ = [
    "NativeState",
    "ShellResult",
    "cron_fires",
    "default_commands",
    "fmt",
    "probe",
    "read_file",
    "run_shell",
    "unit_file_of",
    "unit_on_failure",
]
