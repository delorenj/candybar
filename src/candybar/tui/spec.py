"""Answers to the create wizard and how they become a candybar spec.

Pure and import-light (no textual): `candybar create --no-tui` and the TUI
share it, so a flag-built file and a wizard-built file are the same file.
The spec it builds is the dict `project.render` / `project.plan_write` take
(manifest field names, `runner` a dict).
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from candybar.registry.manifest import SLUG_RE, CandybarManifest
from candybar.runners import RunnerError, canonical_runner
from candybar.timezones import DEFAULT_TIMEZONE, TimezoneError, resolve_timezone

KINDS = ("exec", "systemd", "hermes", "n8n", "entrypoint")
KIND_LABELS = {
    "exec": "exec: run a command",
    "systemd": "systemd: start a user service",
    "hermes": "hermes: trigger a Hermes job",
    "n8n": "n8n: call a webhook workflow",
    "entrypoint": "script: run an executable file",
}
# Kinds whose runner table takes `timeout_s`.
TIMEOUT_KINDS = frozenset({"exec", "hermes", "n8n"})

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SpecError(ValueError):
    """An answer that cannot become a candybar. `field` names the Answers field to blame."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass
class Answers:
    """Everything the wizard (or the flags) can say, in plain strings and flags."""

    name: str = ""
    description: str = ""
    tags: str = ""  # comma separated
    schedule: str = ""
    timezone: str = DEFAULT_TIMEZONE
    kind: str = ""  # one of KINDS, "" = not chosen yet
    command: str = ""  # exec: command line
    workdir: str = ""
    env: list[str] = field(default_factory=list)  # KEY=VAL
    unit: str = ""  # systemd
    profile: str = ""  # hermes
    job_id: str = ""
    target: str = ""  # n8n: webhook path or http(s) URL
    data: str = ""  # n8n: JSON object text
    entrypoint: str = ""  # script path
    timeout: str = ""  # seconds, exec/hermes/n8n
    on_failure: str = ""  # command line
    noise: bool = False
    enabled: bool = True

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> Answers:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in values.items() if k in known})


@dataclass(frozen=True)
class CreateResult:
    """What the TUI hands back: the spec, where to write it and what to do next."""

    spec: dict[str, Any]
    dest: Path
    register: bool = False
    overwrite: bool = False


# --- parsing helpers ------------------------------------------------------------


def split_command(text: str, *, field_name: str = "command") -> list[str]:
    try:
        argv = shlex.split(text)
    except ValueError as exc:
        raise SpecError(field_name, f"cannot parse the command line: {exc}") from exc
    if not argv:
        raise SpecError(field_name, "a command line is required")
    return argv


def parse_tags(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


def parse_env(lines: Iterable[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _ENV_KEY_RE.match(key):
            raise SpecError("env", f"env line {line!r} must look like KEY=value")
        env[key] = value
    return env


def parse_timeout(text: str) -> int | None:
    text = str(text).strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError as exc:
        raise SpecError("timeout", f"timeout must be a whole number of seconds, got {text!r}") from exc
    if value < 1:
        raise SpecError("timeout", "timeout must be at least 1 second")
    return value


def parse_hermes_ref(text: str) -> tuple[str, str]:
    """`PROFILE:JOB_ID` -> (profile, job_id)."""
    profile, sep, job_id = text.partition(":")
    if not sep or not profile.strip() or not job_id.strip():
        raise SpecError("profile", f"expected PROFILE:JOB_ID, got {text!r}")
    return profile.strip(), job_id.strip()


def is_url(target: str) -> bool:
    return target.startswith(("http://", "https://"))


# --- field validators (messages come from the manifest's own rules) -----------


def _manifest_error(field_name: str, **values: Any) -> str | None:
    probe = {"name": "probe", "schedule": "* * * * *", "entrypoint": "probe", **values}
    try:
        CandybarManifest.model_validate(probe)
    except ValueError as exc:
        for err in getattr(exc, "errors", list)():
            if err.get("loc") == (field_name,):
                return str(err.get("msg", "invalid")).removeprefix("Value error, ")
        return None
    return None


def name_error(name: str) -> str | None:
    if not name:
        return "a name is required"
    if not SLUG_RE.match(name):
        return (
            f"invalid name {name!r}: lowercase letters, digits and hyphens, "
            "no leading or trailing hyphen"
        )
    return None


def schedule_error(cron: str) -> str | None:
    """The manifest's complaint about a cron expression, or None when it is fine."""
    if not cron.strip():
        return "a schedule is required"
    return _manifest_error("schedule", schedule=cron)


def timezone_error(tz: str) -> str | None:
    try:
        resolve_timezone(tz)
    except TimezoneError as exc:
        return str(exc)
    return None


def host_timezone(
    *,
    localtime: Path = Path("/etc/localtime"),
    env: Mapping[str, str] | None = None,
) -> str:
    """The host's IANA zone: `$TZ`, else the `/etc/localtime` symlink target, else UTC."""
    env = os.environ if env is None else env
    candidates: list[str] = []
    tz_env = env.get("TZ", "").strip().lstrip(":")
    if tz_env:
        candidates.append(tz_env)
    try:
        target = os.readlink(localtime)
    except OSError:
        target = ""
    marker = "zoneinfo/"
    if marker in target:
        candidates.append(target.split(marker, 1)[1])
    for candidate in candidates:
        if timezone_error(candidate) is None:
            return candidate
    return DEFAULT_TIMEZONE


# --- building the spec -------------------------------------------------------


def build_runner(a: Answers) -> dict[str, Any] | None:
    """The runner table for `a`, or None for the script-entrypoint kind. Raises SpecError."""
    kind = a.kind
    runner: dict[str, Any]
    if kind == "exec":
        runner = {"kind": "exec", "argv": split_command(a.command)}
        if a.workdir.strip():
            runner["workdir"] = a.workdir.strip()
        env = parse_env(a.env)
        if env:
            runner["env"] = env
    elif kind == "systemd":
        if not a.unit.strip():
            raise SpecError("unit", "a unit name is required")
        runner = {"kind": "systemd", "unit": a.unit.strip()}
    elif kind == "hermes":
        if not a.profile.strip():
            raise SpecError("profile", "a Hermes profile is required")
        if not a.job_id.strip():
            raise SpecError("job_id", "a Hermes job id is required")
        runner = {"kind": "hermes", "profile": a.profile.strip(), "job_id": a.job_id.strip()}
    elif kind == "n8n":
        target = a.target.strip()
        if not target:
            raise SpecError("target", "a webhook path or URL is required")
        runner = {"kind": "n8n", "url" if is_url(target) else "webhook": target}
        if a.data.strip():
            try:
                data = json.loads(a.data)
            except ValueError as exc:
                raise SpecError("data", f"data is not valid JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise SpecError("data", "data must be a JSON object")
            runner["data"] = data
    elif kind == "entrypoint":
        if not a.entrypoint.strip():
            raise SpecError("entrypoint", "a script path is required")
        if a.timeout.strip():
            raise SpecError("timeout", "a timeout needs a runner; use an exec runner instead of a script entrypoint")
        if a.on_failure.strip():
            raise SpecError("on_failure", "on-failure needs a runner; use an exec runner instead of a script entrypoint")
        return None
    else:
        raise SpecError("kind", "choose a runner: exec, systemd, hermes, n8n or a script entrypoint")
    timeout = parse_timeout(a.timeout)
    if timeout is not None:
        if kind not in TIMEOUT_KINDS:
            raise SpecError("timeout", f"a {kind} runner has no timeout")
        runner["timeout_s"] = timeout
    if a.on_failure.strip():
        runner["on_failure"] = split_command(a.on_failure, field_name="on_failure")
    try:
        canonical_runner(_probe(runner))
    except RunnerError as exc:
        raise SpecError(_blame(kind, str(exc)), str(exc)) from exc
    return runner


def _probe(runner: dict[str, Any]) -> dict[str, Any]:
    """The runner as the project loader will see it: relative paths made absolute.

    Project files resolve a relative `argv[0]` / `workdir` against the project
    root after this point, so validating them as typed would refuse `./run.sh`.
    """
    probe = dict(runner)
    for key in ("argv", "on_failure"):
        argv = probe.get(key)
        if argv and "/" in argv[0] and not argv[0].startswith("/"):
            probe[key] = ["/" + argv[0].lstrip("./"), *argv[1:]]
    workdir = probe.get("workdir")
    if isinstance(workdir, str) and workdir.strip() and not workdir.startswith("/"):
        probe["workdir"] = "/" + workdir
    return probe


def _blame(kind: str, message: str) -> str:
    """Pick the Answers field a runner validation message is about."""
    lowered = message.lower()
    for word, field_name in (
        ("workdir", "workdir"), ("env", "env"), ("timeout", "timeout"), ("on_failure", "on_failure"),
        ("webhook", "target"), ("url", "target"), ("data", "data"), ("unit", "unit"),
        ("profile", "profile"), ("job_id", "job_id"), ("argv", "command"),
    ):
        if word in lowered:
            return field_name
    return {"exec": "command", "systemd": "unit", "hermes": "profile", "n8n": "target"}.get(kind, "kind")


def build_spec(a: Answers) -> dict[str, Any]:
    """The definition dict `project.plan_write` takes. Raises SpecError (no name/schedule checks)."""
    spec: dict[str, Any] = {
        "name": a.name,
        "description": a.description.strip(),
        "tags": parse_tags(a.tags),
        "schedule": " ".join(a.schedule.split()),
        "timezone": a.timezone.strip() or DEFAULT_TIMEZONE,
        "noise": bool(a.noise),
        "enabled": bool(a.enabled),
    }
    runner = build_runner(a)
    if runner is None:
        spec["entrypoint"] = a.entrypoint.strip()
    else:
        spec["runner"] = runner
    return spec


# --- systemd unit suggestions ------------------------------------------------


def list_user_units(
    run: Callable[..., Any] | None = None, *, timeout: float = 3.0
) -> list[str]:
    """Best-effort `.service` names of the user manager; [] on any failure."""
    import subprocess

    run = run or subprocess.run
    try:
        out = run(
            ["systemctl", "--user", "list-unit-files", "--type=service", "--no-legend", "--plain"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if getattr(out, "returncode", 1) != 0:
        return []
    units = []
    for line in str(out.stdout).splitlines():
        parts = line.split()
        if parts and parts[0].endswith(".service") and not parts[0].endswith("@.service"):
            units.append(parts[0])
    return sorted(set(units))


def format_fire(instant: datetime, tz: ZoneInfo) -> str:
    return instant.astimezone(tz).strftime("%a %Y-%m-%d %H:%M %Z")
