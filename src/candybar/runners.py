"""Runner definitions: how the executor starts a candybar's work (Story 7.3, AD-18).

`candybars.runner` (jsonb) selects the runner kind. Pure models and rules,
no I/O, so the manifest, the API and the executor validate the same shape:

- `exec`    -> `{argv[], workdir?, env{}?, timeout_s?, on_failure?}`: an argv
  spawned in its own cgroup scope under the executor unit. `env` holds
  non-secret literals only; secrets come from the user manager's environment
  or `op` inside the job.
- `systemd` -> `{unit, on_failure?}`: the executor runs
  `systemctl --user start --wait <unit>`; the `.service` keeps every directive
  (EnvironmentFile, hardening, TimeoutStartSec, OnFailure=) and only its
  `.timer` retires.

The bare `entrypoint` column stays the `exec` default for rows that carry no
runner. A kind this module does not know (the `hermes` runner is Story 7.4's)
is rejected at registration, never stored.

The stored form is canonical: `canonical_runner` validates, applies defaults
and drops unset optionals, so a stored dict re-validates to itself and the
PATCH diff never reports a change that is not one.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from .text import has_unsafe_text

# Env vars the AD-8 protocol owns: a literal must never shadow them.
RESERVED_ENV_PREFIX = "CANDYBAR_"
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# `systemctl start` takes the name verbatim: no leading dash (option injection),
# no slash (path), and only the characters a unit name can hold.
SYSTEMD_UNIT_RE = re.compile(r"^[A-Za-z0-9_.:@\\][A-Za-z0-9_.:@\\-]*\.service$")
TIMEOUT_MAX_SECONDS = 7 * 24 * 3600
ARGV_MAX_ITEMS = 256
ENV_MAX_ITEMS = 256


class RunnerError(ValueError):
    """Raised when a runner definition is invalid."""


def _check_argv(value: list[str], *, what: str) -> list[str]:
    if not value:
        raise ValueError(f"{what} must not be empty")
    if len(value) > ARGV_MAX_ITEMS:
        raise ValueError(f"{what} has more than {ARGV_MAX_ITEMS} items")
    if any(has_unsafe_text(item) for item in value):
        raise ValueError(f"{what} must not contain NUL or lone surrogate characters")
    program = value[0]
    if not program.strip():
        raise ValueError(f"{what}[0] must not be blank")
    if "/" in program and not program.startswith("/"):
        raise ValueError(
            f"{what}[0] must be an absolute path or a bare command name "
            f"(looked up on PATH), got {program!r}"
        )
    return value


class _RunnerBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # An argv run once, best-effort and unmetered, after a terminal `failed`,
    # with the CANDYBAR_* protocol env plus CANDYBAR_FAILURE_MESSAGE.
    on_failure: list[str] | None = None

    @field_validator("on_failure")
    @classmethod
    def _on_failure_is_argv(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _check_argv(value, what="on_failure")


class ExecRunner(_RunnerBase):
    kind: Literal["exec"]
    argv: list[str]
    # Absolute directory the job starts in; unset means the user's home, the
    # working directory cron and `systemd --user` services give their jobs.
    workdir: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    timeout_s: int | None = Field(default=None, ge=1, le=TIMEOUT_MAX_SECONDS)

    @field_validator("argv")
    @classmethod
    def _argv_is_argv(cls, value: list[str]) -> list[str]:
        return _check_argv(value, what="argv")

    @field_validator("workdir")
    @classmethod
    def _workdir_is_absolute(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if has_unsafe_text(value) or not value.startswith("/"):
            raise ValueError(f"workdir must be an absolute path, got {value!r}")
        return value

    @field_validator("env")
    @classmethod
    def _env_is_literals(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > ENV_MAX_ITEMS:
            raise ValueError(f"env has more than {ENV_MAX_ITEMS} entries")
        for key, val in value.items():
            if not ENV_KEY_RE.match(key):
                raise ValueError(f"env key {key!r} is not a valid variable name")
            if key.startswith(RESERVED_ENV_PREFIX):
                raise ValueError(f"env key {key!r} is reserved for the candybar protocol (AD-8)")
            if has_unsafe_text(val):
                raise ValueError(f"env[{key}] must not contain NUL or lone surrogate characters")
        return value


class SystemdRunner(_RunnerBase):
    kind: Literal["systemd"]
    unit: str

    @field_validator("unit")
    @classmethod
    def _unit_is_service(cls, value: str) -> str:
        if not SYSTEMD_UNIT_RE.match(value):
            raise ValueError(f"unit must be a <name>.service systemd unit name, got {value!r}")
        return value


Runner = Annotated[ExecRunner | SystemdRunner, Field(discriminator="kind")]
_RUNNER_ADAPTER: TypeAdapter[ExecRunner | SystemdRunner] = TypeAdapter(Runner)


def parse_runner(raw: object) -> ExecRunner | SystemdRunner:
    """Validate a runner definition (a dict), raising `RunnerError` on any problem."""
    if not isinstance(raw, dict):
        raise RunnerError(f"runner must be an object, got {type(raw).__name__}")
    try:
        return _RUNNER_ADAPTER.validate_python(raw)
    except ValueError as exc:  # pydantic.ValidationError is a ValueError
        raise RunnerError(_readable(exc)) from exc


def _readable(exc: ValueError) -> str:
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    parts = []
    for err in errors():
        where = ".".join(str(p) for p in err.get("loc", ()) if p != "function-after[...]")
        parts.append(f"{where}: {err.get('msg', 'invalid')}" if where else str(err.get("msg")))
    return "; ".join(parts)


def canonical_runner(raw: object) -> dict[str, Any]:
    """The validated runner in its stored form: defaults applied, unset optionals dropped."""
    return parse_runner(raw).model_dump(exclude_none=True)


def runner_label(runner: dict[str, Any]) -> str:
    """The short human label kept in the NOT NULL `entrypoint` column for a runner row."""
    if runner.get("kind") == "systemd":
        return f"systemd:{runner['unit']}"
    argv = runner.get("argv") or ["?"]
    return str(argv[0])


__all__ = [
    "ARGV_MAX_ITEMS",
    "RESERVED_ENV_PREFIX",
    "SYSTEMD_UNIT_RE",
    "ExecRunner",
    "Runner",
    "RunnerError",
    "SystemdRunner",
    "canonical_runner",
    "parse_runner",
    "runner_label",
]
