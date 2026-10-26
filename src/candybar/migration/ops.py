"""`candybar migrate {plan,cutover,verify,rollback,status}` (Story 7.5, AD-21).

The five verbs over the committed ledger. Ordering rules the wave stories
rely on:

- **cutover**: register the candybar DISABLED -> retire the native scheduler
  (the ledger's `disable` commands, then a probe that it is retired) -> enable
  the candybar. Any failure after the native scheduler was touched restores
  it with the `rollback` commands and leaves the candybar disabled, so a job
  never ends up on two schedulers or on none. Refused inside the guard
  window around a native or candybar fire (`refusal`).
- **rollback**: restore the native scheduler FIRST (and probe it active),
  then disable the candybar best effort. With the daemon down the native
  scheduler is still restored; the entry records `disable_pending` and a
  re-run finishes the disable. If the restore fails the candybar is left
  enabled: the job keeps one scheduler.
- **verify**: the first scheduled (or misfire) invocation after cutover is
  the proof. Never a manual run (AD-21). A job that already failed natively
  (`native_failing`) verifies on result parity.

Exit codes: 0 ok, 1 refused/failed, 2 invalid ledger or unknown id,
3 pending (verify: no fire yet; rollback: the candybar disable is pending).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .client import ApiError
from .ledger import (
    KIND_SYSTEMD_TIMER,
    STATUS_CUTOVER,
    STATUS_PLANNED,
    STATUS_ROLLED_BACK,
    STATUS_VERIFIED,
    Ledger,
    LedgerEntry,
    LedgerError,
    Verification,
    load_ledger,
    locked,
    save_ledger,
)
from .native import (
    NativeState,
    ReadFile,
    Shell,
    cron_fires,
    default_commands,
    fmt,
    probe,
    unit_file_of,
    unit_on_failure,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_PENDING = 3

# AD-21: cutover is refused within 2 minutes of a fire. For a job firing more
# often than every 8 minutes the window shrinks to a quarter of its period,
# or a `*/2` or `* * * * *` job could never be cut over at all.
GUARD_MAX = timedelta(minutes=2)
# A fire that just happened is still being dispatched (and a crontab line's
# run has no "in progress" signal): let it settle before retiring anything.
SETTLE = timedelta(seconds=15)
WAIT_POLL_S = 5.0
SCHEDULED_TRIGGERS = ("scheduled", "misfire")


class Api(Protocol):
    def list_candybars(self) -> list[dict[str, Any]]: ...
    def get_candybar(self, candybar_id: str) -> dict[str, Any] | None: ...
    def register(self, definition: dict[str, Any]) -> dict[str, Any]: ...
    def update(self, candybar_id: str, changes: dict[str, Any]) -> dict[str, Any]: ...
    def set_enabled(self, candybar_id: str, enabled: bool) -> dict[str, Any]: ...
    def invocations(
        self, candybar_id: str, limit: int = ...
    ) -> list[dict[str, Any]]: ...


@dataclass
class Host:
    """Everything a verb touches outside the ledger, injectable for tests."""

    api: Api
    shell: Shell
    read: ReadFile
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    sleep: Callable[[float], None] = field(default=lambda _s: None)
    echo: Callable[[str], None] = field(default=print)

    def probe(self, entry: LedgerEntry) -> NativeState:
        return probe(entry.native, shell=self.shell, read=self.read, now=self.now())


@dataclass(frozen=True)
class Fires:
    prev: datetime | None
    next: datetime | None


def candybar_fires(entry: LedgerEntry, now: datetime) -> Fires:
    manifest = entry.manifest
    prev, nxt = cron_fires(manifest.schedule, manifest.timezone, now)
    return Fires(prev=prev, next=nxt)


def guard_for(fires: Fires) -> timedelta:
    if fires.prev is not None and fires.next is not None:
        return min(GUARD_MAX, (fires.next - fires.prev) / 4)
    return GUARD_MAX


def refusal(entry: LedgerEntry, state: NativeState, now: datetime) -> str | None:
    """Why a cutover must not start right now, or None.

    A cutover takes seconds but must not straddle a fire: the native fire
    could land after the candybar is enabled (two runs) or the native
    scheduler could be retired just before its fire while the candybar is
    enabled just after its tick (no run).
    """
    tz = entry.manifest.timezone
    fires = candybar_fires(entry, now)
    guard = guard_for(fires)
    if state.running:
        return (
            f"a native run is in progress ({state.detail}); cut over after it finishes"
        )
    if state.active and state.next_fire is not None and state.next_fire - now < guard:
        return (
            f"the native fire at {fmt(state.next_fire, tz)} is within the "
            f"{int(guard.total_seconds())}s cutover guard"
        )
    if fires.next is not None and fires.next - now < guard:
        return (
            f"the candybar fire at {fmt(fires.next, tz)} is within the "
            f"{int(guard.total_seconds())}s cutover guard"
        )
    if fires.prev is not None and now - fires.prev < SETTLE:
        return f"the candybar fire at {fmt(fires.prev, tz)} just happened; let it settle"
    jitter = timedelta(seconds=entry.native.jitter_s)
    if (
        state.active
        and jitter
        and fires.prev is not None
        and (state.last_fire is None or state.last_fire < fires.prev)
        and now < fires.prev + jitter + guard
    ):
        return (
            f"the native run for {fmt(fires.prev, tz)} may still be pending "
            f"(jitter {entry.native.jitter_s}s, last native run {fmt(state.last_fire, tz)})"
        )
    return None


# --------------------------------------------------------------------------
# Helpers.


def _registration(entry: LedgerEntry) -> dict[str, Any]:
    """The full definition (canonical, defaults applied), runner xor entrypoint."""
    body = entry.manifest.model_dump()
    body.pop("enabled", None)
    return {key: value for key, value in body.items() if value is not None}


def _run(host: Host, commands: Iterable[str]) -> str | None:
    """Run commands in order; the first failure's description, or None."""
    for command in commands:
        host.echo(f"  $ {command}")
        result = host.shell(command)
        if not result.ok:
            tail = (result.err or result.out).strip()[-300:]
            return f"`{command}` exited {result.rc}: {tail}"
    return None


def _restore_native(host: Host, entry: LedgerEntry) -> str | None:
    """Run the rollback commands and probe the native scheduler active again."""
    failure = _run(host, entry.rollback)
    if failure is not None:
        return failure
    after = host.probe(entry)
    if not after.active:
        return f"the native scheduler is not active after the rollback commands ({after.detail})"
    return None


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def _select(ledger: Ledger, ids: list[str], wave: str | None) -> list[LedgerEntry]:
    if ids:
        return [ledger.entry(job_id) for job_id in ids]
    return [
        entry
        for entry in ledger.jobs
        if entry.status in (STATUS_PLANNED, STATUS_ROLLED_BACK)
        and (wave is None or entry.wave == wave)
    ]


def _cutover_blockers(host: Host, entry: LedgerEntry) -> list[str]:
    """What must be fixed in the ledger (or the owning repo) before a cutover."""
    blockers: list[str] = []
    if not entry.source_of_truth.commit:
        blockers.append(
            "source_of_truth.commit is missing: commit and push the owning repo's change that "
            "stops a re-install from re-enabling the old scheduler, then record it"
        )
    if not entry.disable or not entry.rollback:
        blockers.append(
            "disable/rollback commands are missing: run `candybar migrate plan --write`"
        )
    runner = entry.manifest.runner or {}
    if runner.get("kind") == "systemd" and runner.get("on_failure"):
        on_failure = unit_on_failure(str(runner.get("unit")), host.shell)
        if on_failure:
            blockers.append(
                f"runner.on_failure is set but {runner.get('unit')} already has OnFailure="
                f"{on_failure}: drop the hook (AD-18), the unit keeps its own alerting"
            )
    return blockers


# --------------------------------------------------------------------------
# plan


def _plan_entry(host: Host, entry: LedgerEntry, *, write: bool) -> int:
    """Print one entry's plan; returns the number of blocking problems."""
    now = host.now()
    tz = entry.manifest.timezone
    problems: list[str] = []
    warnings: list[str] = []
    native = entry.native
    if write and native.kind == KIND_SYSTEMD_TIMER and not native.unit_file:
        native.unit_file = unit_file_of(native.unit or "", host.shell)
        if not native.unit_file:
            warnings.append(
                f"no unit file found for {native.unit}; rollback enables it by name"
            )
    if write and not entry.disable and not entry.rollback:
        entry.disable, entry.rollback = default_commands(native)
        entry.record(
            now,
            "plan",
            "written",
            "default disable/rollback commands for " + native.kind,
        )

    state = host.probe(entry)
    fires = candybar_fires(entry, now)
    host.echo(f"== {entry.id} (wave {entry.wave}) status={entry.status}")
    host.echo(
        f"candybar: {entry.name}  {entry.manifest.schedule!r} {tz}  "
        + json.dumps(entry.manifest.runner or {"entrypoint": entry.manifest.entrypoint})
    )
    host.echo(
        f"native:   {native.kind} {native.schedule!r} (jitter {native.jitter_s}s)"
    )
    host.echo(
        f"  state: {'active' if state.active else 'retired' if state.retired else 'UNKNOWN'} "
        f"({state.detail}); next {fmt(state.next_fire, tz)}; last {fmt(state.last_fire, tz)}"
    )
    host.echo(
        f"  candybar fires: previous {fmt(fires.prev, tz)}; next {fmt(fires.next, tz)}"
    )
    for deviation in entry.deviations:
        host.echo(f"  deviation: {deviation}")
    sot = entry.source_of_truth
    host.echo(f"source of truth: {sot.repo} {sot.path} @ {sot.commit or 'MISSING'}")
    host.echo("disable:")
    for command in entry.disable:
        host.echo(f"  $ {command}")
    host.echo("rollback:")
    for command in entry.rollback:
        host.echo(f"  $ {command}")

    if not state.active and not state.retired:
        problems.append(f"the native scheduler is in an unknown state: {state.detail}")
    if state.retired and entry.status == STATUS_PLANNED:
        warnings.append(
            "the native scheduler is already retired: the job has no scheduler now"
        )
    if state.active and state.next_fire is not None and fires.next is not None:
        drift = abs((state.next_fire - fires.next).total_seconds())
        if drift > native.jitter_s + 60:
            warnings.append(
                f"next native fire {fmt(state.next_fire, tz)} vs candybar {fmt(fires.next, tz)}: "
                "schedules differ (expected only for interval timers, whose phase is not kept)"
            )
    if entry.status in (STATUS_PLANNED, STATUS_ROLLED_BACK):
        problems.extend(_cutover_blockers(host, entry))
        reason = refusal(entry, state, now) if state.active or state.retired else None
        host.echo(f"cutover now: {'refused: ' + reason if reason else 'allowed'}")
    for warning in warnings:
        host.echo(f"  warning: {warning}")
    for problem in problems:
        host.echo(f"  PROBLEM: {problem}")
    host.echo("")
    return len(problems)


def plan(
    host: Host,
    path: Path,
    ids: list[str],
    *,
    wave: str | None = None,
    write: bool = False,
) -> int:
    """Validate entries and print what a cutover would do. `--write` fills the defaults."""
    try:
        if write:
            with locked(path):
                ledger = load_ledger(path)
                entries = _select(ledger, ids, wave)
                problems = sum(
                    _plan_entry(host, entry, write=True) for entry in entries
                )
                save_ledger(path, ledger)
        else:
            ledger = load_ledger(path)
            entries = _select(ledger, ids, wave)
            problems = sum(_plan_entry(host, entry, write=False) for entry in entries)
    except LedgerError as exc:
        host.echo(f"error: {exc}")
        return EXIT_USAGE
    if not entries:
        host.echo("no entries to plan")
    return EXIT_FAILED if problems else EXIT_OK


# --------------------------------------------------------------------------
# cutover


def _ensure_registered_disabled(host: Host, entry: LedgerEntry) -> str:
    """The candybar's id, registered (or re-synced) and DISABLED. Raises ApiError/LedgerError."""
    definition = _registration(entry)
    if entry.candybar_id:
        existing = host.api.get_candybar(entry.candybar_id)
        if existing is not None:
            if existing.get("enabled", True):
                # E.g. a rollback whose disable is still pending: off before anything else.
                host.api.set_enabled(entry.candybar_id, False)
            host.api.update(entry.candybar_id, definition)
            host.echo(
                f"  re-synced candybar {entry.name} ({entry.candybar_id}), disabled"
            )
            return entry.candybar_id
    try:
        created = host.api.register({**definition, "enabled": False})
    except ApiError as exc:
        if exc.status == 409:
            raise LedgerError(
                f"candybar name {entry.name!r} is already registered but not recorded in this "
                "entry: set `candybar_id` in the ledger if it is this job's"
            ) from exc
        raise
    host.echo(f"  registered candybar {entry.name} ({created['id']}), disabled")
    return str(created["id"])


def cutover(host: Host, path: Path, job_id: str, *, wait_s: float = 0) -> int:
    """register disabled -> retire native -> enable; reversed on any failure."""
    try:
        with locked(path):
            ledger = load_ledger(path)
            entry = ledger.entry(job_id)
            return _cutover(host, path, ledger, entry, wait_s)
    except LedgerError as exc:
        host.echo(f"error: {exc}")
        return EXIT_USAGE


def _cutover(
    host: Host, path: Path, ledger: Ledger, entry: LedgerEntry, wait_s: float
) -> int:
    def refuse(reason: str) -> int:
        entry.record(host.now(), "cutover", "refused", reason)
        save_ledger(path, ledger)
        host.echo(f"cutover of {entry.id} refused: {reason}")
        return EXIT_FAILED

    if entry.status in (STATUS_CUTOVER, STATUS_VERIFIED):
        host.echo(
            f"{entry.id} is already {entry.status} (candybar {entry.candybar_id})"
        )
        return EXIT_FAILED
    blockers = _cutover_blockers(host, entry)
    if blockers:
        return refuse("; ".join(blockers))

    deadline = host.now() + timedelta(seconds=max(wait_s, 0))
    while True:
        state = host.probe(entry)
        if not state.active and not state.retired:
            return refuse(
                f"the native scheduler is in an unknown state: {state.detail}"
            )
        reason = refusal(entry, state, host.now())
        if reason is None:
            break
        remaining = (deadline - host.now()).total_seconds()
        if remaining <= 0:
            return refuse(reason)
        host.echo(f"waiting: {reason}")
        host.sleep(min(WAIT_POLL_S, remaining))

    host.echo(f"cutover {entry.id} -> candybar {entry.name}")
    # 1. Register disabled. Nothing native is touched yet, so a failure here
    #    leaves the job exactly where it was.
    try:
        candybar_id = _ensure_registered_disabled(host, entry)
    except ApiError as exc:
        entry.record(host.now(), "cutover", "failed", f"register: {exc}")
        save_ledger(path, ledger)
        host.echo(f"cutover failed before the native scheduler was touched: {exc}")
        return EXIT_FAILED
    entry.candybar_id = candybar_id
    save_ledger(path, ledger)  # the id is on disk before anything native changes

    failure: str | None = None
    try:
        # 2. Retire the native scheduler (skipped when it already is).
        if state.active:
            failure = _run(host, entry.disable)
            if failure is None:
                after = host.probe(entry)
                if not after.retired:
                    failure = (
                        "the native scheduler is not retired after the disable commands "
                        f"({after.detail})"
                    )
        # 3. Enable the candybar.
        if failure is None:
            try:
                host.api.set_enabled(candybar_id, True)
            except ApiError as exc:
                failure = f"enable: {exc}"
    except Exception as exc:  # noqa: BLE001 - once native was touched, any surprise must restore it
        failure = f"unexpected {type(exc).__name__}: {exc}"
    if failure is not None:
        host.echo(f"cutover failed: {failure}; restoring the native scheduler")
        restored = _restore_native(host, entry)
        if restored is None:
            entry.record(host.now(), "cutover", "failed", f"{failure}; native restored")
            host.echo("native scheduler restored; the candybar stays disabled")
        else:
            entry.record(
                host.now(),
                "cutover",
                "failed",
                f"{failure}; RESTORE FAILED: {restored}",
            )
            host.echo(
                f"MANUAL ACTION NEEDED: the native scheduler was NOT restored ({restored}). "
                f"Run the ledger's rollback commands for {entry.id} by hand."
            )
        save_ledger(path, ledger)
        return EXIT_FAILED

    now = host.now()
    first = candybar_fires(entry, now).next
    entry.status = STATUS_CUTOVER
    entry.cutover_at = now
    entry.disable_pending = False
    entry.verification = Verification()
    entry.record(
        now, "cutover", "ok", f"candybar {candybar_id} enabled; first fire {fmt(first)}"
    )
    save_ledger(path, ledger)
    host.echo(
        f"cut over: {entry.id} now runs as candybar {entry.name} ({candybar_id}); "
        f"first fire {fmt(first, entry.manifest.timezone)}. Commit the ledger."
    )
    return EXIT_OK


# --------------------------------------------------------------------------
# verify


def verify(host: Host, path: Path, job_id: str) -> int:
    """The first scheduled fire after cutover is the proof (AD-21)."""
    try:
        with locked(path):
            ledger = load_ledger(path)
            entry = ledger.entry(job_id)
            return _verify(host, path, ledger, entry)
    except LedgerError as exc:
        host.echo(f"error: {exc}")
        return EXIT_USAGE


def _verify(host: Host, path: Path, ledger: Ledger, entry: LedgerEntry) -> int:
    if entry.status == STATUS_VERIFIED:
        host.echo(
            f"{entry.id} already verified by invocation {entry.verification.invocation_id}"
        )
        return EXIT_OK
    if (
        entry.status != STATUS_CUTOVER
        or not entry.candybar_id
        or entry.cutover_at is None
    ):
        host.echo(f"{entry.id} is {entry.status}, not cut over: nothing to verify")
        return EXIT_FAILED
    state = host.probe(entry)
    if state.active:
        entry.record(
            host.now(),
            "verify",
            "failed",
            f"native scheduler active again: {state.detail}",
        )
        save_ledger(path, ledger)
        host.echo(
            f"{entry.id}: the native scheduler is ACTIVE again ({state.detail}) — the job is on two "
            "schedulers. Re-run its disable commands and fix the source of truth that re-enabled it."
        )
        return EXIT_FAILED
    try:
        invocations = host.api.invocations(entry.candybar_id)
    except ApiError as exc:
        host.echo(f"cannot read invocations: {exc}")
        return EXIT_FAILED
    runs = [
        inv
        for inv in invocations
        if inv.get("trigger") in SCHEDULED_TRIGGERS
        and inv.get("result") is not None
        and (started := _parse_ts(inv.get("startedAt"))) is not None
        and started >= entry.cutover_at
    ]
    if not runs:
        nxt = candybar_fires(entry, host.now()).next
        host.echo(
            f"{entry.id}: no scheduled run since cutover yet; next fire "
            f"{fmt(nxt, entry.manifest.timezone)}"
        )
        return EXIT_PENDING
    first = min(runs, key=lambda inv: _parse_ts(inv["startedAt"]))
    result = first["result"]
    detail = (
        f"invocation {first['id']} ({first.get('trigger')}) {result} exit={first.get('exitCode')} "
        f"runtime={first.get('runtimeMs')}ms memory={first.get('memoryPeakBytes')}B "
        f"cpu={first.get('cpuLoad')}"
    )
    if result == "success" or entry.native_failing:
        entry.status = STATUS_VERIFIED
        entry.verification = Verification(
            invocation_id=str(first["id"]),
            result=str(result),
            at=_parse_ts(first.get("finishedAt")) or host.now(),
        )
        parity = (
            " (result parity: the job already failed natively)"
            if result != "success"
            else ""
        )
        entry.record(host.now(), "verify", "ok", detail + parity)
        save_ledger(path, ledger)
        host.echo(f"verified {entry.id}: {detail}{parity}. Commit the ledger.")
        if not first.get("memoryPeakBytes"):
            host.echo("  warning: the run reported no memory peak; check its metering")
        return EXIT_OK
    entry.record(host.now(), "verify", "failed", detail)
    save_ledger(path, ledger)
    host.echo(
        f"{entry.id}: the first scheduled run FAILED: {detail} ({first.get('errorMessage')}). "
        f"Investigate, or `candybar migrate rollback {entry.id}`."
    )
    return EXIT_FAILED


# --------------------------------------------------------------------------
# rollback


def rollback(host: Host, path: Path, job_id: str, *, reason: str = "") -> int:
    """Restore the native scheduler first, then disable the candybar best effort."""
    try:
        with locked(path):
            ledger = load_ledger(path)
            entry = ledger.entry(job_id)
            return _rollback(host, path, ledger, entry, reason)
    except LedgerError as exc:
        host.echo(f"error: {exc}")
        return EXIT_USAGE


def _rollback(
    host: Host, path: Path, ledger: Ledger, entry: LedgerEntry, reason: str
) -> int:
    if entry.status == STATUS_PLANNED:
        host.echo(f"{entry.id} was never cut over: nothing to roll back")
        return EXIT_FAILED
    if entry.status == STATUS_ROLLED_BACK and not entry.disable_pending:
        host.echo(f"{entry.id} is already rolled back")
        return EXIT_OK
    if entry.status in (STATUS_CUTOVER, STATUS_VERIFIED):
        host.echo(f"rollback {entry.id}: restoring the native scheduler")
        failure = _restore_native(host, entry)
        if failure is not None:
            entry.record(
                host.now(), "rollback", "failed", f"native not restored: {failure}"
            )
            save_ledger(path, ledger)
            host.echo(
                f"rollback FAILED: {failure}. The candybar stays enabled so the job keeps one "
                "scheduler; fix the restore and re-run."
            )
            return EXIT_FAILED
        entry.status = STATUS_ROLLED_BACK
        entry.disable_pending = bool(entry.candybar_id)
        entry.record(
            host.now(),
            "rollback",
            "ok",
            "native scheduler restored" + (f": {reason}" if reason else ""),
        )
        save_ledger(path, ledger)  # the restore is on disk before the API is tried
    if entry.candybar_id and entry.disable_pending:
        try:
            host.api.set_enabled(entry.candybar_id, False)
            entry.disable_pending = False
            entry.record(
                host.now(), "rollback", "ok", f"candybar {entry.candybar_id} disabled"
            )
        except ApiError as exc:
            if exc.status == 404:
                entry.disable_pending = False
                entry.record(
                    host.now(), "rollback", "ok", "candybar no longer registered"
                )
            else:
                entry.record(
                    host.now(), "rollback", "pending", f"candybar disable: {exc}"
                )
        save_ledger(path, ledger)
    if entry.disable_pending:
        host.echo(
            f"native scheduler restored, but candybar {entry.candybar_id} could NOT be disabled "
            "(API unreachable?). Re-run `candybar migrate rollback "
            f"{entry.id}` once the API is back. Commit the ledger."
        )
        return EXIT_PENDING
    host.echo(
        f"rolled back {entry.id}: native scheduler restored, candybar disabled. Commit the ledger."
    )
    return EXIT_OK


# --------------------------------------------------------------------------
# status


def status(
    host: Host,
    path: Path,
    *,
    as_json: bool = False,
    probe_native: bool = False,
    wave: str | None = None,
) -> int:
    try:
        ledger = load_ledger(path)
    except LedgerError as exc:
        host.echo(f"error: {exc}")
        return EXIT_USAGE
    try:
        registered: dict[str, dict[str, Any]] | None = {
            str(c["id"]): c for c in host.api.list_candybars()
        }
    except ApiError:
        registered = None
    rows: list[dict[str, Any]] = []
    for entry in ledger.jobs:
        if wave is not None and entry.wave != wave:
            continue
        enabled: bool | None = None
        if registered is not None and entry.candybar_id in registered:
            enabled = bool(registered[entry.candybar_id].get("enabled", True))
        row: dict[str, Any] = {
            "id": entry.id,
            "wave": entry.wave,
            "status": entry.status,
            "candybar": entry.name,
            "candybarId": entry.candybar_id,
            "enabled": enabled,
            "disablePending": entry.disable_pending,
            "verifiedBy": entry.verification.invocation_id,
        }
        if probe_native:
            state = host.probe(entry)
            row["native"] = (
                "active" if state.active else "retired" if state.retired else "unknown"
            )
        rows.append(row)
    if as_json:
        host.echo(json.dumps(rows, indent=2))
        return EXIT_OK
    if registered is None:
        host.echo("(candybar API unreachable: enabled column unknown)")
    if not rows:
        host.echo(f"ledger {path}: no entries")
        return EXIT_OK
    for row in rows:
        flag = {True: "enabled", False: "disabled", None: "-"}[row["enabled"]]
        extra = " DISABLE-PENDING" if row["disablePending"] else ""
        native = f" native={row['native']}" if "native" in row else ""
        host.echo(
            f"{row['status']:<12} {row['wave']:<5} {row['id']:<48} {row['candybar']:<36} "
            f"{flag}{native}{extra}"
        )
    return EXIT_OK


__all__ = [
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_PENDING",
    "EXIT_USAGE",
    "GUARD_MAX",
    "Host",
    "candybar_fires",
    "cutover",
    "guard_for",
    "plan",
    "refusal",
    "rollback",
    "status",
    "verify",
]
