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
- `hermes`  -> `{profile, job_id, timeout_s?, on_failure?}` (Story 7.4, AD-19):
  the executor runs `hermes -p <profile> cron run <job_id> --wait` and the
  Hermes job keeps its delivery, sessions, `context_from` and monitor gates.
  The job must be pinned `scheduler: external` in Hermes so its own ticker
  never fires it; this runner is its only trigger. The attempt's input +
  output tokens become the invocation's token spend. Hermes no-agent jobs do
  not use this runner (README, "Runner kinds").
- `n8n`     -> `{webhook | url, data{}?, timeout_s?, on_failure?}`: the
  executor POSTs the invocation context (plus the static `data`) to an n8n
  webhook workflow and waits for its response, which decides the verdict.
  `webhook` is a path under `$CANDYBAR_N8N_URL/webhook/`; `url` is a whole
  URL (another host, or `/webhook-test/` while building). The workflow must
  have no Schedule Trigger of its own: this runner is its only clock.

The bare `entrypoint` column stays the `exec` default for rows that carry no
runner. A kind this module does not know is rejected at registration, never stored.

The stored form is canonical: `canonical_runner` validates, applies defaults
and drops unset optionals, so a stored dict re-validates to itself and the
PATCH diff never reports a change that is not one.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .text import has_unsafe_text

# Env vars the AD-8 protocol owns: a literal must never shadow them.
RESERVED_ENV_PREFIX = "CANDYBAR_"
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# `systemctl start` takes the name verbatim: no leading dash (option injection),
# no slash (path), and only the characters a unit name can hold.
SYSTEMD_UNIT_RE = re.compile(r"^[A-Za-z0-9_.:@\\][A-Za-z0-9_.:@\\-]*\.service$")
# A hermes profile name or job ref (id or name): the CLI takes it as one argv
# item, so no leading dash (option injection), no whitespace, no path.
HERMES_REF_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:@-]*$")
HERMES_DEFAULT_TIMEOUT_S = 3600
# An n8n webhook path: one argv-safe relative URL path, no `..` segment, no query.
N8N_WEBHOOK_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:@/-]*")
N8N_URL_RE = re.compile(r"https?://[^\s/?#]+(/\S*)?")
N8N_DEFAULT_TIMEOUT_S = 3600
# `data` travels as one argv item to the webhook client: keep it small.
N8N_DATA_MAX_BYTES = 64 * 1024
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


class HermesRunner(_RunnerBase):
    kind: Literal["hermes"]
    profile: str
    job_id: str
    # Bounds the whole `cron run --wait`: Hermes gives up waiting at this many
    # seconds and the executor kills the CLI a grace period later.
    timeout_s: int = Field(default=HERMES_DEFAULT_TIMEOUT_S, ge=1, le=TIMEOUT_MAX_SECONDS)

    @field_validator("profile", "job_id")
    @classmethod
    def _ref_is_one_safe_argv_item(cls, value: str, info: object) -> str:
        if not HERMES_REF_RE.match(value):
            raise ValueError(
                f"{getattr(info, 'field_name', 'value')} must be a plain name or id "
                f"(letters, digits, _ . : @ -, no leading dash), got {value!r}"
            )
        return value


def _strings(value: Any) -> Iterator[str]:
    """Every str key and value inside a JSON document."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


class N8nRunner(_RunnerBase):
    kind: Literal["n8n"]
    # Exactly one: a webhook path (resolved against $CANDYBAR_N8N_URL at run
    # time, so moving n8n never touches a row) or a whole URL.
    webhook: str | None = None
    url: str | None = None
    # Static, non-secret JSON handed to the workflow as `data`: lets one
    # workflow serve several candybars.
    data: dict[str, Any] = Field(default_factory=dict)
    # Bounds the wait for the workflow's response; the executor kills the
    # webhook client a grace period later.
    timeout_s: int = Field(default=N8N_DEFAULT_TIMEOUT_S, ge=1, le=TIMEOUT_MAX_SECONDS)

    @field_validator("webhook")
    @classmethod
    def _webhook_is_a_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not N8N_WEBHOOK_RE.fullmatch(value) or ".." in value.split("/"):
            raise ValueError(
                f"webhook must be a path under /webhook/ (letters, digits, _ . : @ - /, "
                f"no leading dash or slash, no '..'), got {value!r}"
            )
        return value

    @field_validator("url")
    @classmethod
    def _url_is_http(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if has_unsafe_text(value) or not N8N_URL_RE.fullmatch(value):
            raise ValueError(f"url must be an http(s) URL, got {value!r}")
        try:
            parts = urlsplit(value)
            parts.port  # noqa: B018 - raises on a non-numeric or out-of-range port
        except ValueError as exc:
            raise ValueError(f"url must be an http(s) URL, got {value!r}: {exc}") from exc
        if not parts.hostname:
            raise ValueError(f"url must name a host, got {value!r}")
        return value

    @field_validator("data")
    @classmethod
    def _data_is_small_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"data must be plain JSON: {exc}") from exc
        if len(encoded.encode()) > N8N_DATA_MAX_BYTES:
            raise ValueError(f"data must be at most {N8N_DATA_MAX_BYTES} bytes of JSON")
        # json.dumps escapes NUL and lone surrogates, so check the raw strings:
        # Postgres jsonb rejects both, after the API has published its fact.
        if any(has_unsafe_text(text) for text in _strings(value)):
            raise ValueError("data must not contain NUL or lone surrogate characters")
        return value

    @model_validator(mode="after")
    def _one_target(self) -> N8nRunner:
        if (self.webhook is None) == (self.url is None):
            raise ValueError("an n8n runner needs exactly one of `webhook` or `url`")
        return self


RunnerModel = ExecRunner | SystemdRunner | HermesRunner | N8nRunner
Runner = Annotated[RunnerModel, Field(discriminator="kind")]
_RUNNER_ADAPTER: TypeAdapter[RunnerModel] = TypeAdapter(Runner)


def parse_runner(raw: object) -> RunnerModel:
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
    if runner.get("kind") == "hermes":
        return f"hermes:{runner['profile']}/{runner['job_id']}"
    if runner.get("kind") == "n8n":
        return f"n8n:{runner.get('webhook') or runner.get('url')}"
    argv = runner.get("argv") or ["?"]
    return str(argv[0])


__all__ = [
    "ARGV_MAX_ITEMS",
    "HERMES_DEFAULT_TIMEOUT_S",
    "HERMES_REF_RE",
    "N8N_DATA_MAX_BYTES",
    "N8N_DEFAULT_TIMEOUT_S",
    "N8N_WEBHOOK_RE",
    "RESERVED_ENV_PREFIX",
    "SYSTEMD_UNIT_RE",
    "ExecRunner",
    "HermesRunner",
    "N8nRunner",
    "Runner",
    "RunnerError",
    "SystemdRunner",
    "canonical_runner",
    "parse_runner",
    "runner_label",
]
