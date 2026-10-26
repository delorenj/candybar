"""The `hermes` runner: trigger a Hermes agent-mode job and wait (Story 7.4, AD-19).

Candybar owns WHEN the job fires; Hermes keeps everything else (delivery,
sessions, `context_from`, monitor gates). The executor runs

    hermes -p <profile> cron run <job_id> --wait --timeout <timeout_s>

through the same spawn path as an exec runner (own cgroup scope, so the CLI's
memory and CPU are metered, timeout kill, protocol env). The Hermes side
(delorenj/hermes-agent, `hermes_cli/cron_wait.py`) blocks until the durable
attempt is terminal, exits 0 only on `completed`, and ends stdout with one line

    cron-run-result: {"status": "completed"|"failed"|"unknown", "error": ...,
                      "input_tokens": N, "output_tokens": N, "execution_id": ...}

That line is the Hermes CLI's AD-8 result: `parse_result_line` turns it into a
`ResultFile`, with the attempt's input + output tokens as the invocation's
token spend (0 when Hermes had no usage record). The exit code stays primary in
`resolve_outcome`; the parsed result refines it (the failure message, the
tokens of a failed attempt).

The job must be pinned `scheduler: external` in Hermes, or its own ticker would
fire it as well (the double-fire AD-19 exists to prevent). A trigger that finds
the job already running joins that attempt rather than starting a second one.
"""

from __future__ import annotations

import json
import os

from ..events.envelope import TOKEN_SPEND_MAX, SelfReportedMetrics
from ..runners import HermesRunner
from ..text import scrub_text
from .runner import ResultFile

HERMES_BIN_ENV = "CANDYBAR_HERMES"
RESULT_PREFIX = "cron-run-result: "
# The executor kills the CLI this long after the time it asked Hermes to wait.
TIMEOUT_GRACE_SECONDS = 60
# Only the tail can hold the result line; Hermes may print a long run summary first.
_PARSE_TAIL_LINES = 200


def hermes_bin() -> str:
    return os.environ.get(HERMES_BIN_ENV) or "hermes"


def hermes_argv(runner: HermesRunner) -> list[str]:
    return [
        hermes_bin(),
        "-p",
        runner.profile,
        "cron",
        "run",
        runner.job_id,
        "--wait",
        "--timeout",
        str(runner.timeout_s),
    ]


def kill_after_seconds(runner: HermesRunner) -> int:
    return runner.timeout_s + TIMEOUT_GRACE_SECONDS


def parse_result_line(stdout: str) -> ResultFile | None:
    """The AD-8 result carried by the CLI's `cron-run-result:` line, or None."""
    for line in reversed(stdout.splitlines()[-_PARSE_TAIL_LINES:]):
        if not line.startswith(RESULT_PREFIX):
            continue
        try:
            doc = json.loads(line[len(RESULT_PREFIX) :])
        except ValueError:
            return None
        if not isinstance(doc, dict):
            return None
        ok = doc.get("status") == "completed"
        error = doc.get("error")
        message = None
        if not ok:
            message = scrub_text(str(error) if error else f"hermes attempt {doc.get('status') or 'failed'}")
        return ResultFile(
            status="success" if ok else "fail",
            message=message,
            metrics=SelfReportedMetrics(token_spend=_tokens(doc)),
        )
    return None


def _tokens(doc: dict) -> int:
    """Input + output tokens of the attempt; 0 when absent or unusable."""
    total = 0
    for key in ("input_tokens", "output_tokens"):
        value = doc.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        try:
            total += max(0, int(value))
        except (ValueError, OverflowError):  # NaN / Infinity are valid JSON floats
            continue
    return min(total, TOKEN_SPEND_MAX)


__all__ = [
    "HERMES_BIN_ENV",
    "RESULT_PREFIX",
    "TIMEOUT_GRACE_SECONDS",
    "hermes_argv",
    "hermes_bin",
    "kill_after_seconds",
    "parse_result_line",
]
