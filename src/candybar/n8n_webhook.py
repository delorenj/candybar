"""The webhook client the `n8n` runner spawns: `python -m candybar.n8n_webhook`.

The executor runs this (not an in-process HTTP call) so an n8n run shares the
exec spawn path: protocol env, timeout kill, shutdown handling, stderr tail
and the AD-8 result file. stdlib only, so it starts fast.

It POSTs the invocation context to the workflow's webhook and waits for the
response (the Webhook node must respond "When Last Node Finishes" or "Using
'Respond to Webhook' Node"; "Immediately" only proves the trigger landed):

    {"candybar": {"id", "invocationId", "trigger", "tickId"}, "data": {...}}

The verdict:
- a non-2xx answer, no answer within `--timeout` (a wall-clock deadline for
  the whole exchange), or no connection: fail. Redirects are never followed:
  urllib would turn the POST into a body-less GET and judge whatever page
  the redirect lands on (an SSO portal, a moved URL) as the workflow's answer,
  so a 3xx is a failure that names its Location;
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
import http.client
import json
import math
import os
import signal
import sys
import urllib.error
import urllib.request
from typing import Any

from candybar import __version__
from candybar.text import scrub_text

VERDICT_KEY = "candybar"
EMPTY_BODY_MESSAGE = (
    "n8n answered with an empty body: the workflow most likely failed before its "
    "Respond to Webhook node (see the n8n execution)"
)
MESSAGE_MAX = 2000
TOKEN_SPEND_MAX = 2**63 - 1


class _Deadline(BaseException):  # BaseException: no library `except Exception` swallows it
    """The wall-clock `--timeout` passed (socket timeouts are per operation)."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None  # urllib then raises the 3xx as an HTTPError


_OPENER = urllib.request.build_opener(_NoRedirects)


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


def _number(value: Any) -> float | int | None:
    """A finite JSON number (ints of any size stay exact), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _metrics(verdict: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    tokens = _number(verdict.get("tokenSpend", verdict.get("token_spend")))
    if tokens is not None:
        metrics["token_spend"] = min(max(0, int(tokens)), TOKEN_SPEND_MAX)
    cost = _number(verdict.get("costUsd", verdict.get("cost_usd")))
    if cost is not None and cost >= 0:
        try:
            as_float = float(cost)
        except OverflowError:  # an int too big for a float: not a real cost
            as_float = math.inf
        if math.isfinite(as_float):
            metrics["cost_usd"] = as_float
    return metrics


def _clean(message: str | None) -> str | None:
    return scrub_text(message)[:MESSAGE_MAX] if message else message


def call(url: str, data: dict[str, Any], timeout_s: float) -> tuple[bool, str | None, dict[str, Any]]:
    """POST the context and judge the answer: (success, message, metrics)."""
    late = f"n8n did not answer within {timeout_s:g}s"
    try:
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
        with _OPENER.open(request, timeout=timeout_s) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", errors="replace")
        except (OSError, http.client.HTTPException):
            text = ""
        if 300 <= exc.code < 400:
            where = exc.headers.get("Location") if exc.headers else None
            return False, _clean(f"n8n answered {exc.code} redirect to {where or '?'} (not followed)"), {}
        return False, _clean(f"n8n answered {exc.code}: {_snippet(text) or exc.reason}"), {}
    except (_Deadline, TimeoutError):
        return False, late, {}
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError):
            return False, late, {}
        return False, _clean(f"n8n unreachable at {url}: {reason}"), {}
    except http.client.HTTPException as exc:  # IncompleteRead, InvalidURL, BadStatusLine...
        return False, _clean(f"n8n connection broke mid-response at {url}: {exc!r}"), {}
    except ValueError as exc:
        return False, _clean(f"invalid n8n URL {url!r}: {exc}"), {}

    if not body.strip():
        return False, EMPTY_BODY_MESSAGE, {}
    verdict = _verdict(body)
    status = verdict.get("status", "success")
    message = verdict.get("message")
    message = _clean(str(message)) if message is not None else None
    if status != "success":
        return False, message or _clean(f"workflow reported status {status!r}"), _metrics(verdict)
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

    def _expire(signum: int, frame: object) -> None:
        raise _Deadline

    # The socket timeout bounds each connect/recv; this bounds the whole
    # exchange, so a trickling peer cannot outlive `--timeout`.
    previous = signal.signal(signal.SIGALRM, _expire)
    signal.setitimer(signal.ITIMER_REAL, args.timeout)
    try:
        data = json.loads(args.data)
        ok, message, metrics = call(args.url, data if isinstance(data, dict) else {}, args.timeout)
    except _Deadline:
        ok, message, metrics = False, f"n8n did not answer within {args.timeout:g}s", {}
    except Exception as exc:  # noqa: BLE001 - any surprise still leaves a verdict, never a bare traceback
        ok, message, metrics = False, _clean(f"n8n webhook client crashed: {exc!r}"), {}
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    _write_result(ok, message, metrics)
    if message:
        stream = sys.stdout if ok else sys.stderr
        try:
            print(message, file=stream)
        except (UnicodeEncodeError, OSError):
            pass  # the result file already carries it; the exit code must still match
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
