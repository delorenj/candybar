"""`candybar.toml` manifest parsing and validation (AD-8).

The manifest is how any executable registers without an SDK. Validation is
pure (no DB, no bus) so both the CLI and the API reject bad input identically.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import croniter
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from ..runners import RunnerError, canonical_runner, runner_label
from ..text import has_unsafe_text
from ..timezones import DEFAULT_TIMEZONE, TimezoneError, resolve_timezone

MANIFEST_FILENAME = "candybar.toml"

# Spine naming convention: unique lowercase slugs.
SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

# croniter's non-standard hashed/random field tokens (`H`, `R`, `R(0-30)`,
# `R/2`, any case). `R` re-rolls a random value every time an expression is
# parsed, and the dispatcher parses per tick: such a schedule fires at
# random minutes and the read API's fire times never match the real ones.
_HASHED_FIELD_RE = re.compile(r"^[hr](\(\d+-\d+\))?(/\d+)?$", re.IGNORECASE)


class ManifestError(ValueError):
    """Raised when a candybar.toml is missing, unparseable, or invalid."""


class CandybarManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    tags: list[str] = []
    schedule: str
    # IANA zone the cron is read in as local wall time (AD-16); UTC by default.
    timezone: str = DEFAULT_TIMEZONE
    # Exactly one of `entrypoint` (the bare-executable exec default) and
    # `runner` (Story 7.3, AD-18: `{kind: exec|systemd, ...}`) defines how the
    # executor starts the job. `runner` is held in its canonical stored form.
    entrypoint: str | None = None
    runner: dict[str, Any] | None = None
    noise: bool = False

    @model_validator(mode="after")
    def _entrypoint_xor_runner(self) -> CandybarManifest:
        if (self.entrypoint is None) == (self.runner is None):
            raise ValueError("define exactly one of `entrypoint` and `runner`")
        return self

    @model_validator(mode="after")
    def _text_is_storable(self) -> CandybarManifest:
        # Postgres text rejects NUL and UTF-8 cannot encode a lone surrogate:
        # refuse here (422) instead of a 500 out of the insert or the event.
        for field in ("description", "schedule", "timezone"):
            if has_unsafe_text(getattr(self, field)):
                raise ValueError(f"{field} must not contain NUL or lone surrogate characters")
        if self.entrypoint is not None and has_unsafe_text(self.entrypoint):
            raise ValueError("entrypoint must not contain NUL or lone surrogate characters")
        if any(has_unsafe_text(tag) for tag in self.tags):
            raise ValueError("tags must not contain NUL or lone surrogate characters")
        return self

    @field_validator("name")
    @classmethod
    def _name_is_slug(cls, value: str) -> str:
        if not SLUG_RE.match(value):
            raise ValueError(
                f"invalid candybar name {value!r}: must match {SLUG_RE.pattern} "
                "(lowercase slug, digits and hyphens, no leading/trailing hyphen)"
            )
        return value

    @field_validator("tags")
    @classmethod
    def _tags_non_blank(cls, value: list[str]) -> list[str]:
        # The created/updated event payloads require non-blank tags; refuse
        # here so a blank tag is a 422, never a 500 out of the event builder.
        if any(not tag.strip() for tag in value):
            raise ValueError("tags must be non-blank strings")
        return value

    @field_validator("timezone")
    @classmethod
    def _timezone_is_iana(cls, value: str) -> str:
        try:
            resolve_timezone(value)
        except TimezoneError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("entrypoint")
    @classmethod
    def _entrypoint_non_empty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("entrypoint must not be empty")
        return value

    @field_validator("runner", mode="before")
    @classmethod
    def _runner_is_valid(cls, value: object) -> dict[str, Any] | None:
        if value is None:
            return None
        try:
            return canonical_runner(value)
        except RunnerError as exc:
            raise ValueError(f"invalid runner: {exc}") from exc

    @property
    def entrypoint_label(self) -> str:
        """What the NOT NULL `entrypoint` column holds: the path, or the runner's label."""
        if self.entrypoint is not None:
            return self.entrypoint
        return runner_label(self.runner or {})

    @field_validator("schedule")
    @classmethod
    def _schedule_is_standard_cron(cls, value: str) -> str:
        fields = value.split()
        if len(fields) != 5:
            raise ValueError(
                f"schedule must be a standard 5-field cron expression, got {len(fields)} fields: {value!r}"
            )
        if any(_HASHED_FIELD_RE.match(field) for field in fields):
            raise ValueError(
                f"invalid cron schedule {value!r}: hashed/random fields (H, R) are not "
                "standard cron and do not fire deterministically"
            )
        try:
            croniter.croniter(value)
        except (croniter.CroniterError, ValueError) as exc:
            raise ValueError(f"invalid cron schedule {value!r}: {exc}") from exc
        return value


def load_manifest(path: str | Path) -> CandybarManifest:
    """Load and validate the `candybar.toml` at `path` (a directory or the file itself).

    A relative `entrypoint` is resolved against the manifest's directory and
    must be an existing file (a directory or a missing path is rejected).
    """
    p = Path(path)
    manifest_path = p / MANIFEST_FILENAME if p.is_dir() else p
    if not manifest_path.is_file():
        raise ManifestError(f"no {MANIFEST_FILENAME} at {p}")
    try:
        raw = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{manifest_path}: invalid TOML: {exc}") from exc
    table = raw.get("candybar")
    if not isinstance(table, dict):
        raise ManifestError(f"{manifest_path}: missing [candybar] table")
    try:
        manifest = CandybarManifest.model_validate(table)
    except ValueError as exc:
        raise ManifestError(f"{manifest_path}: {exc}") from exc
    if manifest.entrypoint is None:
        # A runner definition names no file: argv, workdir and unit live on the
        # host the executor runs on, which is not necessarily this process's.
        return manifest
    entrypoint = Path(manifest.entrypoint)
    if not entrypoint.is_absolute():
        entrypoint = (manifest_path.parent / entrypoint).resolve()
    if not entrypoint.is_file():
        raise ManifestError(
            f"{manifest_path}: entrypoint is not an existing file: {manifest.entrypoint!r}"
        )
    return manifest.model_copy(update={"entrypoint": str(entrypoint)})
