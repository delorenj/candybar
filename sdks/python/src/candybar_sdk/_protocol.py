"""The AD-8 process protocol, Python side.

In via env (set by the candybar executor on every spawn):
``CANDYBAR_INVOCATION_ID``, ``CANDYBAR_ID``, ``CANDYBAR_TRIGGER``,
``CANDYBAR_TICK_ID`` (empty for manual triggers), ``CANDYBAR_RESULT_FILE``.

Out: UTF-8 JSON at ``CANDYBAR_RESULT_FILE``::

    {"status": "success" | "fail",
     "message": "...",                      # omitted when unset
     "metrics": {"token_spend": 0,          # int >= 0, always present
                 "cost_usd": 0.25}}         # omitted when unset

The executor's reader ignores unknown top-level keys but rejects unknown keys
inside ``metrics`` — this module never writes any. Exit codes: 0 success,
1 fail, 2 protocol error (not under the executor / result file unwritable).

Stdlib only. This module never imports a bus client: SDKs never open a NATS
connection (AD-8); the executor owns execution, metering and events (AD-7).
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import math
import os
import sys
import tempfile
import traceback
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Literal, NoReturn

ENV_INVOCATION_ID = "CANDYBAR_INVOCATION_ID"
ENV_CANDYBAR_ID = "CANDYBAR_ID"
ENV_TRIGGER = "CANDYBAR_TRIGGER"
ENV_TICK_ID = "CANDYBAR_TICK_ID"
ENV_RESULT_FILE = "CANDYBAR_RESULT_FILE"

#: Vars that must be non-empty. CANDYBAR_TICK_ID is legitimately empty
#: (manual triggers), so it is optional here.
REQUIRED_ENV = (ENV_INVOCATION_ID, ENV_CANDYBAR_ID, ENV_TRIGGER, ENV_RESULT_FILE)

#: Same cap the executor applies to failure messages in event data.
MESSAGE_MAX = 500
#: The registry stores token_spend as BIGINT; a larger total would make the
#: executor's terminal write fail on every redelivery.
TOKEN_SPEND_MAX = 2**63 - 1

EXIT_SUCCESS = 0
EXIT_FAIL = 1
EXIT_PROTOCOL = 2

Status = Literal["success", "fail"]


class ProtocolError(RuntimeError):
    """The process was not started under the candybar executor's contract."""


@dataclass
class _Meter:
    token_spend: int = 0
    cost_usd: float | None = None


@dataclass(frozen=True)
class Context:
    """Invocation context parsed from the five ``CANDYBAR_*`` env vars.

    Identity fields are read-only; token spend and cost accumulate through
    :meth:`add_tokens` / :meth:`add_cost` and are reported even when the
    handler fails (a failed LLM call still spent tokens).
    """

    invocation_id: str
    candybar_id: str
    trigger: str  # scheduled | manual | misfire (unknown values pass through)
    tick_id: str | None  # None for manual triggers
    result_file: Path
    _meter: _Meter = field(default_factory=_Meter, repr=False, compare=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Context:
        env = os.environ if environ is None else environ
        missing = [name for name in REQUIRED_ENV if not env.get(name)]
        if missing:
            raise ProtocolError(
                "not running under the candybar executor: missing " + ", ".join(missing)
            )
        return cls(
            invocation_id=env[ENV_INVOCATION_ID],
            candybar_id=env[ENV_CANDYBAR_ID],
            trigger=env[ENV_TRIGGER],
            tick_id=env.get(ENV_TICK_ID) or None,
            result_file=Path(env[ENV_RESULT_FILE]),
        )

    @property
    def token_spend(self) -> int:
        return self._meter.token_spend

    @property
    def cost_usd(self) -> float | None:
        return self._meter.cost_usd

    def add_tokens(self, tokens: int) -> None:
        """Add to the total tokens consumed (input + output) by this invocation."""
        if isinstance(tokens, bool) or not isinstance(tokens, int):
            raise TypeError(f"tokens must be an int, got {type(tokens).__name__}")
        if tokens < 0:
            raise ValueError(f"tokens must be >= 0, got {tokens}")
        total = self._meter.token_spend + tokens
        if total > TOKEN_SPEND_MAX:
            raise ValueError(f"token_spend total exceeds {TOKEN_SPEND_MAX}")
        self._meter.token_spend = total

    def add_cost(self, usd: float) -> None:
        """Add to this invocation's cost in US dollars."""
        if isinstance(usd, bool) or not isinstance(usd, int | float):
            raise TypeError(f"usd must be a number, got {type(usd).__name__}")
        if not math.isfinite(usd) or usd < 0:
            raise ValueError(f"usd must be a finite number >= 0, got {usd}")
        self._meter.cost_usd = (self._meter.cost_usd or 0.0) + float(usd)


@dataclass(frozen=True)
class Result:
    """An explicit outcome a handler may return instead of ``None``/``str``."""

    status: Status
    message: str | None = None

    def __post_init__(self) -> None:
        if self.status not in ("success", "fail"):
            raise ValueError(f"status must be 'success' or 'fail', got {self.status!r}")
        if self.message is not None and not isinstance(self.message, str):
            raise TypeError(f"message must be a str or None, got {type(self.message).__name__}")

    @classmethod
    def success(cls, message: str | None = None) -> Result:
        return cls("success", message)

    @classmethod
    def fail(cls, message: str | None = None) -> Result:
        return cls("fail", message)


def result_document(
    status: Status,
    message: str | None = None,
    *,
    token_spend: int = 0,
    cost_usd: float | None = None,
) -> dict[str, Any]:
    """The AD-8 result JSON object, in canonical key order."""
    doc: dict[str, Any] = {"status": status}
    if message is not None:
        doc["message"] = message[:MESSAGE_MAX]
    metrics: dict[str, Any] = {"token_spend": token_spend}
    if cost_usd is not None:
        metrics["cost_usd"] = cost_usd
    doc["metrics"] = metrics
    return doc


def write_result(path: Path, doc: Mapping[str, Any]) -> None:
    """Write the result atomically: the executor never reads a partial file."""
    path = Path(path)
    data = json.dumps(doc, ensure_ascii=False, allow_nan=False).encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".result-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _exception_message(exc: BaseException) -> str:
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


async def _await(awaitable: Awaitable[Any]) -> Any:
    return await awaitable


Handler = Callable[[Context], Any]


class Candybar:
    """A handler wrapped in the AD-8 protocol. Create with :func:`candybar`.

    Calling the object with a :class:`Context` calls the handler directly
    (handy for unit-testing your own code); :meth:`run` is the process entry.
    """

    def __init__(self, handler: Handler) -> None:
        if not callable(handler):
            raise TypeError("candybar() needs a callable handler")
        self.handler = handler
        functools.update_wrapper(self, handler)

    def __call__(self, ctx: Context) -> Any:
        return self.handler(ctx)

    def invoke(self, environ: Mapping[str, str] | None = None, *, stderr: IO[str] | None = None) -> int:
        """Run one invocation under the protocol and return the exit code."""
        err = sys.stderr if stderr is None else stderr
        env = os.environ if environ is None else environ
        try:
            ctx = Context.from_env(env)
        except ProtocolError as exc:
            print(f"candybar-sdk: {exc}", file=err)
            result_file = env.get(ENV_RESULT_FILE)
            if result_file:
                try:
                    write_result(Path(result_file), result_document("fail", str(exc)))
                except OSError as write_exc:
                    print(f"candybar-sdk: cannot write result file: {write_exc}", file=err)
            return EXIT_PROTOCOL

        status, message = self._call(ctx, err)
        doc = result_document(status, message, token_spend=ctx.token_spend, cost_usd=ctx.cost_usd)
        try:
            write_result(ctx.result_file, doc)
        except (OSError, ValueError) as exc:
            print(f"candybar-sdk: cannot write result file {ctx.result_file}: {exc}", file=err)
            return EXIT_PROTOCOL
        return EXIT_SUCCESS if status == "success" else EXIT_FAIL

    def run(self) -> NoReturn:
        """Process entry point: ``if __name__ == "__main__": main.run()``."""
        sys.exit(self.invoke())

    def _call(self, ctx: Context, err: IO[str]) -> tuple[Status, str | None]:
        try:
            returned = self.handler(ctx)
            if inspect.isawaitable(returned):
                returned = asyncio.run(_await(returned))
        except SystemExit as exc:
            if exc.code in (None, 0):
                return "success", None
            if isinstance(exc.code, int):
                return "fail", f"exit {exc.code}"
            return "fail", str(exc.code)
        except KeyboardInterrupt:
            return "fail", "interrupted"
        except Exception as exc:  # noqa: BLE001 — every handler error is a fail result
            traceback.print_exc(file=err)
            return "fail", _exception_message(exc)

        if returned is None:
            return "success", None
        if isinstance(returned, Result):
            return returned.status, returned.message
        if isinstance(returned, str):
            return "success", returned
        return "fail", (
            f"handler returned unsupported type {type(returned).__name__} "
            "(expected None, str or Result)"
        )


def candybar(handler: Handler) -> Candybar:
    """Decorate a ``(ctx) -> None | str | Result`` handler (sync or async).

    Return ``None`` or a message string for success, ``Result.fail(...)`` (or
    raise) for failure. Report usage with ``ctx.add_tokens`` / ``ctx.add_cost``.
    """
    return Candybar(handler)
