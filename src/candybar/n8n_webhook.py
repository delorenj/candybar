"""The webhook client the `n8n` runner spawns: `python -m candybar.n8n_webhook`.

The executor runs this (not an in-process HTTP call) so an n8n run shares the
exec spawn path: protocol env, timeout kill, shutdown handling, stderr tail
and the AD-8 result file. stdlib only, so it starts fast.

It POSTs the invocation context to the workflow's webhook and waits for the
response (the Webhook node must respond "When Last Node Finishes" or "Using
'Respond to Webhook' Node"; "Immediately" only proves the trigger landed):

    {"candybar": {"id", "invocationId", "trigger", "tickId"}, "data": {...}}

The verdict:
- a non-2xx answer, no answer within `--timeout`, or no connection: fail;
- a 2xx answer with an EMPTY body: fail. That is what n8n (2.18) sends when a
  "Using 'Respond to Webhook' Node" workflow dies before its Respond node;
  in "When Last Node Finishes" mode the same error is a 500;
- any other 2xx answer: success, unless its JSON (an object, or the first
  item of a list) carries a `candybar` object that says otherwise:
  `{"candybar": {"status": "success"|"fail", "message", "tokenSpend", "costUsd"}}`.
  The namespaced key keeps a workflow's own `status` fields from deciding.

The result file carries the message and any self-reported tokens/cost; the
exit code (0/1) matches it.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from candybar import __version__

VERDICT_KEY = "candybar"
EMPTY_BODY_MESSAGE = (
    "n8n answered with an empty body: the workflow most likely failed before its "
    "Respond to Webhook node (see the n8n execution)"
)
MESSAGE_MAX = 2000
TOKEN_SPEND_MAX = 2**63 - 1


def _payload(data: dict[str, Any]) -> dict[str, Any]:
    env = os.environ
    return {
        "candybar": {
            "id": env.get("CANDYBAR_ID", ""),
            "invocationId": env.get("CANDYBAR_INVOCATION_ID", ""),
            "trigger": env.get("CANDYBAR_TRIGGER", ""),
            "tickId": env.get("CANDYBAR_TICK_ID") or None,
        },
        "data": data,
    }


def _snippet(body: str) -> str:
    """The useful part of an n8n error body (`{"message": ...}`), else its start."""
    try:
        doc = json.loads(body)
    except ValueError:
        doc = None
    if isinstance(doc, dict) and isinstance(doc.get("message"), str):
        text = doc["message"]
        if isinstance(doc.get("hint"), str):
            text += f" ({doc['hint']})"
        return text[:MESSAGE_MAX]
    return " ".join(body.split())[:MESSAGE_MAX]


def _verdict(body: str) -> dict[str, Any]:
    """The `candybar` verdict object of a 2xx body, or {} when there is none."""
    try:
        doc = json.loads(body) if body.strip() else None
    except ValueError:
        return {}
    if isinstance(doc, list) and doc and isinstance(doc[0], dict):
        doc = doc[0]
    if isinstance(doc, dict) and isinstance(doc.get(VERDICT_KEY), dict):
        return doc[VERDICT_KEY]
    return {}


def _metrics(verdict: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    tokens = verdict.get("tokenSpend", verdict.get("token_spend"))
    if isinstance(tokens, int | float) and not isinstance(tokens, bool) and math.isfinite(tokens):
        metrics["token_spend"] = min(max(0, int(tokens)), TOKEN_SPEND_MAX)
    cost = verdict.get("costUsd", verdict.get("cost_usd"))
    if isinstance(cost, int | float) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0:
        metrics["cost_usd"] = float(cost)
    return metrics


def call(url: str, data: dict[str, Any], timeout_s: float) -> tuple[bool, str | None, dict[str, Any]]:
    """POST the context and judge the answer: (success, message, metrics)."""
    request = urllib.request.Request(
        url,
        data=json.dumps(_payload(data)).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"candybar/{__version__}",
            "X-Candybar-Invocation-Id": os.environ.get("CANDYBAR_INVOCATION_ID", ""),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return False, f"n8n answered {exc.code}: {_snippet(text) or exc.reason}", {}
    except TimeoutError:
        return False, f"n8n did not answer within {timeout_s:g}s", {}
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError):
            return False, f"n8n did not answer within {timeout_s:g}s", {}
        return False, f"n8n unreachable at {url}: {reason}", {}

    if not body.strip():
        return False, EMPTY_BODY_MESSAGE, {}
    verdict = _verdict(body)
    status = verdict.get("status", "success")
    message = verdict.get("message")
    message = str(message)[:MESSAGE_MAX] if message is not None else None
    if status != "success":
        return False, message or f"workflow reported status {status!r}", _metrics(verdict)
    return True, message, _metrics(verdict)


def _write_result(ok: bool, message: str | None, metrics: dict[str, Any]) -> None:
    path = os.environ.get("CANDYBAR_RESULT_FILE")
    if not path:
        return
    doc: dict[str, Any] = {"status": "success" if ok else "fail", "metrics": metrics}
    if message:
        doc["message"] = message
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="candybar.n8n_webhook", description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--data", default="{}", help="Static JSON object passed as `data`.")
    args = parser.parse_args(argv)
    data = json.loads(args.data)
    ok, message, metrics = call(args.url, data if isinstance(data, dict) else {}, args.timeout)
    _write_result(ok, message, metrics)
    if message:
        print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
