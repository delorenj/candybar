"""The `n8n` runner: trigger an n8n webhook workflow and wait for its answer.

Candybar owns WHEN the workflow runs (its Schedule Trigger nodes retire);
n8n owns the steps. The executor runs the stdlib webhook client

    <executor python> -m candybar.n8n_webhook --url <url> --timeout <s> --data <json>

through the same spawn path as an exec runner, so timeouts, shutdown and the
AD-8 result file behave exactly as for any job. The client's verdict rules
are in `candybar.n8n_webhook`.

Metering caveat: the workflow runs inside n8n's own process, so memory and
CPU here are the client's (near zero); runtime is the real wall time of the
workflow, and tokens/cost are whatever the workflow reports back.
"""

from __future__ import annotations

import json
import os
import sys

from ..runners import N8nRunner

N8N_URL_ENV = "CANDYBAR_N8N_URL"
DEFAULT_N8N_URL = "https://n8n.delo.sh"
# The executor kills the client this long after the time it asked it to wait.
TIMEOUT_GRACE_SECONDS = 30


def n8n_base() -> str:
    return (os.environ.get(N8N_URL_ENV) or DEFAULT_N8N_URL).rstrip("/")


def webhook_url(runner: N8nRunner) -> str:
    if runner.url is not None:
        return runner.url
    return f"{n8n_base()}/webhook/{runner.webhook}"


def n8n_argv(runner: N8nRunner) -> list[str]:
    return [
        sys.executable,
        "-m",
        "candybar.n8n_webhook",
        "--url",
        webhook_url(runner),
        "--timeout",
        str(runner.timeout_s),
        "--data",
        json.dumps(runner.data, separators=(",", ":")),
    ]


def kill_after_seconds(runner: N8nRunner) -> int:
    return runner.timeout_s + TIMEOUT_GRACE_SECONDS


__all__ = [
    "DEFAULT_N8N_URL",
    "N8N_URL_ENV",
    "TIMEOUT_GRACE_SECONDS",
    "kill_after_seconds",
    "n8n_argv",
    "n8n_base",
    "webhook_url",
]
