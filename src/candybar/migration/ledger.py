"""The migration ledger: `deploy/migration/ledger.yaml` (Story 7.5, AD-21).

One entry per cron-inventory id (`_bmad-output/planning-artifacts/cron-inventory.md`):
what candybar the job becomes, which native scheduler it leaves, the accepted
deviations, where its source of truth lives (repo, path, commit), the exact
shell commands that retire and restore the native scheduler, the verifying
invocation, and a status `planned | cutover | verified | rolled-back`.

The file is committed: it is the record a human (or a wave story) reads to
undo a cutover even when the candybar daemon is down. `candybar migrate`
rewrites it atomically (temp file + rename) under an exclusive lock on its
directory, so two runs never interleave a write. YAML comments do not
survive a rewrite; the schema lives in `deploy/migration/README.md`.
"""

from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from ..registry.manifest import CandybarManifest
from ..timezones import TimezoneError, resolve_timezone

LEDGER_ENV = "CANDYBAR_LEDGER"
LEDGER_RELATIVE = Path("deploy") / "migration" / "ledger.yaml"

STATUS_PLANNED = "planned"
STATUS_CUTOVER = "cutover"
STATUS_VERIFIED = "verified"
STATUS_ROLLED_BACK = "rolled-back"
Status = Literal["planned", "cutover", "verified", "rolled-back"]

KIND_SYSTEMD_TIMER = "systemd-timer"
KIND_CRONTAB = "crontab"
KIND_HERMES = "hermes"
NativeKind = Literal["systemd-timer", "crontab", "hermes"]

HEADER = (
    "# Candybar migration ledger (Story 7.5, AD-21): one entry per cron-inventory id.\n"
    "# Driven by `candybar migrate {plan,cutover,verify,rollback,status}`; schema and\n"
    "# procedure in deploy/migration/README.md. Commit it after every change: it is\n"
    "# how a cutover is undone when the daemon is down.\n"
)


class LedgerError(ValueError):
    """The ledger is missing, unparseable, invalid, or locked by another run."""


def default_ledger_path() -> Path:
    """`$CANDYBAR_LEDGER`, else the repo checkout's ledger, else `./deploy/migration/ledger.yaml`.

    The CLI runs from the repo's `.venv` (an editable install), so the
    package's own checkout is the natural home of the committed ledger.
    """
    env = os.environ.get(LEDGER_ENV)
    if env:
        return Path(env).expanduser()
    checkout = Path(__file__).resolve().parents[3] / LEDGER_RELATIVE
    if checkout.is_file():
        return checkout
    return Path.cwd() / LEDGER_RELATIVE


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NativeScheduler(_Model):
    """The scheduler a job is leaving, and how to find its next fire."""

    kind: NativeKind
    # The native schedule exactly as found (e.g. `OnCalendar=*-*-* 05:23:00,
    # RandomizedDelaySec=10m`). Human-readable record, never parsed.
    schedule: str = Field(min_length=1)
    # RandomizedDelaySec (or any native start jitter), in seconds. Dropped by
    # the migration (an accepted deviation); the cutover guard still honours
    # a jittered native run that may be pending.
    jitter_s: int = Field(default=0, ge=0)
    # systemd-timer: the `.timer` unit, and its unit file's resolved absolute
    # path. On systemd 257 `disable` of a `systemctl link`-ed unit removes the
    # link, so a rollback must `enable` by path; `plan --write` fills it.
    unit: str | None = None
    unit_file: str | None = None
    # crontab: the exact user-crontab line (`cron` defaults to its first five
    # fields; `timezone` is the zone cron reads it in).
    line: str | None = None
    cron: str | None = None
    timezone: str | None = None
    # hermes: the profile, job id and the jobs.json holding the job record.
    profile: str | None = None
    job_id: str | None = None
    jobs_file: str | None = None

    @model_validator(mode="after")
    def _kind_fields(self) -> NativeScheduler:
        if self.kind == KIND_SYSTEMD_TIMER:
            if not self.unit or not self.unit.endswith(".timer"):
                raise ValueError("a systemd-timer native needs `unit` naming a .timer")
        elif self.kind == KIND_CRONTAB:
            if (
                not self.line
                or not self.line.strip()
                or self.line.lstrip().startswith("#")
            ):
                raise ValueError(
                    "a crontab native needs the exact, uncommented crontab `line`"
                )
            if "\n" in self.line:
                raise ValueError("a crontab `line` is one line")
            if self.cron is None:
                fields = self.line.split()
                if len(fields) < 6:
                    raise ValueError(
                        "a crontab `line` is five schedule fields and a command"
                    )
                self.cron = " ".join(fields[:5])
            if not self.timezone:
                raise ValueError(
                    "a crontab native needs `timezone` (cron reads it as local time)"
                )
        elif self.kind == KIND_HERMES and not (
            self.profile and self.job_id and self.jobs_file
        ):
            raise ValueError(
                "a hermes native needs `profile`, `job_id` and `jobs_file`"
            )
        if self.cron is not None and len(self.cron.split()) != 5:
            raise ValueError(f"native `cron` must be 5 fields, got {self.cron!r}")
        if self.timezone is not None:
            try:
                resolve_timezone(self.timezone)
            except TimezoneError as exc:
                raise ValueError(str(exc)) from exc
        return self


class SourceOfTruth(_Model):
    """Where the job's definition lives, and the commit that stops it re-enabling its timer."""

    repo: str = Field(min_length=1)
    path: str = Field(min_length=1)
    # The owning repo's commit that retires the old scheduler in the source
    # of truth (installer, unit file, mise task). Required before cutover.
    commit: str | None = None


class Verification(_Model):
    invocation_id: str | None = None
    result: str | None = None
    at: AwareDatetime | None = None


class HistoryEvent(_Model):
    at: AwareDatetime
    action: str
    outcome: str
    detail: str = ""


class LedgerEntry(_Model):
    """One migrated (or to-be-migrated) job."""

    id: str = Field(min_length=1)  # the cron-inventory id, verbatim
    wave: str = Field(min_length=1)  # the Epic 7 wave story, e.g. "7.7"
    # The candybar definition registered at cutover: the `candybar.toml`
    # fields (name, description, tags, schedule, timezone, runner or
    # entrypoint, noise). `enabled` is the CLI's to set, never the ledger's.
    candybar: dict[str, Any]
    native: NativeScheduler
    deviations: list[str] = []
    # The job already failed under its native scheduler: verify on result
    # parity (its first candybar fire may fail too), per AD-21.
    native_failing: bool = False
    source_of_truth: SourceOfTruth
    # Exact shell commands, run as `bash -euo pipefail -c '<cmd>'` in order.
    disable: list[str] = []
    rollback: list[str] = []
    status: Status = STATUS_PLANNED
    candybar_id: str | None = None
    cutover_at: AwareDatetime | None = None
    verification: Verification = Verification()
    # A rollback restored the native scheduler but could not reach the API to
    # disable the candybar; re-running `rollback` finishes it.
    disable_pending: bool = False
    history: list[HistoryEvent] = []

    @field_validator("candybar")
    @classmethod
    def _candybar_is_a_manifest(cls, value: dict[str, Any]) -> dict[str, Any]:
        if "enabled" in value:
            raise ValueError(
                "`candybar.enabled` is set by `candybar migrate`, not the ledger"
            )
        manifest = CandybarManifest.model_validate(value)
        # Keep what was written, with the runner in its canonical stored form
        # so a re-sync PATCH of an unchanged definition is a no-op.
        if manifest.runner is not None:
            value = {**value, "runner": manifest.runner}
        return value

    @property
    def manifest(self) -> CandybarManifest:
        return CandybarManifest.model_validate(self.candybar)

    @property
    def name(self) -> str:
        return str(self.candybar["name"])

    def record(self, at: datetime, action: str, outcome: str, detail: str = "") -> None:
        self.history.append(
            HistoryEvent(at=at, action=action, outcome=outcome, detail=detail)
        )

    def to_yaml_data(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["native"] = self.native.model_dump(mode="json", exclude_none=True)
        return data


class Ledger(_Model):
    version: Literal[1] = 1
    jobs: list[LedgerEntry] = []

    @model_validator(mode="after")
    def _unique(self) -> Ledger:
        ids = [entry.id for entry in self.jobs]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate inventory ids: {dupes}")
        names = [entry.name for entry in self.jobs]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate candybar names: {dupes}")
        return self

    def entry(self, job_id: str) -> LedgerEntry:
        for entry in self.jobs:
            if entry.id == job_id:
                return entry
        raise LedgerError(f"no ledger entry for {job_id!r}")


def load_ledger(path: Path) -> Ledger:
    if not path.is_file():
        raise LedgerError(f"no migration ledger at {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise LedgerError(f"{path}: invalid YAML: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise LedgerError(f"{path}: the ledger is a mapping with `version` and `jobs`")
    try:
        return Ledger.model_validate(raw)
    except ValueError as exc:
        raise LedgerError(f"{path}: {exc}") from exc


def dump_ledger(ledger: Ledger) -> str:
    data = {
        "version": ledger.version,
        "jobs": [entry.to_yaml_data() for entry in ledger.jobs],
    }
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=4096)
    return HEADER + body


def save_ledger(path: Path, ledger: Ledger) -> None:
    """Atomic replace: a reader (or a crash) sees the old file or the new one, never half."""
    text = dump_ledger(ledger)
    # Round-trip before replacing: never commit a file the next load refuses.
    Ledger.model_validate(yaml.safe_load(text) or {})
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Exclusive lock for a read-modify-write of the ledger.

    Taken on the ledger's DIRECTORY (the file itself is replaced by rename,
    so a lock on its inode would not exclude the next writer). Never waits:
    a second concurrent `candybar migrate` is refused.
    """
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LedgerError(
                f"another `candybar migrate` holds {path.parent}; retry"
            ) from exc
        yield
    finally:
        os.close(fd)


__all__ = [
    "KIND_CRONTAB",
    "KIND_HERMES",
    "KIND_SYSTEMD_TIMER",
    "LEDGER_ENV",
    "STATUS_CUTOVER",
    "STATUS_PLANNED",
    "STATUS_ROLLED_BACK",
    "STATUS_VERIFIED",
    "HistoryEvent",
    "Ledger",
    "LedgerEntry",
    "LedgerError",
    "NativeScheduler",
    "SourceOfTruth",
    "Verification",
    "default_ledger_path",
    "dump_ledger",
    "load_ledger",
    "locked",
    "save_ledger",
]
