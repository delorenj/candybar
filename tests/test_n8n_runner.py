"""The n8n runner: POST the invocation context to an n8n webhook and wait.

A throwaway local HTTP server plays n8n. The model, the argv, the client's
verdict rules and the full executor path (`run_target` -> a real
`python -m candybar.n8n_webhook` child, unscoped) are pinned against it.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from candybar import n8n_webhook
from candybar.executor import n8n_runner
from candybar.executor.targets import run_target
from candybar.executor.worker import resolve_outcome
from candybar.registry import CandybarManifest
from candybar.runners import (
    N8nRunner,
    RunnerError,
    canonical_runner,
    parse_runner,
    runner_label,
)

N8N = {"kind": "n8n", "webhook": "candybar/nightly"}
IDS = {"invocation_id": uuid4(), "candybar_id": uuid4(), "trigger": "scheduled", "tick_id": "tick:42"}


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# A fake n8n.


class FakeN8n:
    """Answers each POST with the next scripted (status, body, delay)."""

    def __init__(self) -> None:
        self.replies: list[tuple[int, str, float]] = [(200, "{}", 0.0)]
        self.requests: list[dict[str, Any]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                fake.requests.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": json.loads(self.rfile.read(length) or b"null"),
                    }
                )
                status, body, delay = fake.replies.pop(0) if len(fake.replies) > 1 else fake.replies[0]
                time.sleep(delay)
                payload = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reply(self, status: int, body: Any = "", delay: float = 0.0) -> FakeN8n:
        text = body if isinstance(body, str) else json.dumps(body)
        self.replies = [(status, text, delay)]
        return self


@pytest.fixture
def n8n(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeN8n]:
    fake = FakeN8n()
    monkeypatch.setenv(n8n_runner.N8N_URL_ENV, fake.base)
    yield fake
    fake.server.shutdown()


# ---------------------------------------------------------------------------
# The model.


def test_canonical_form_and_label() -> None:
    once = canonical_runner(N8N)
    assert once == {**N8N, "data": {}, "timeout_s": 3600}
    assert canonical_runner(once) == once
    assert runner_label(once) == "n8n:candybar/nightly"
    full = canonical_runner({"kind": "n8n", "url": "https://n8n.example/webhook-test/x"})
    assert runner_label(full) == "n8n:https://n8n.example/webhook-test/x"


def test_takes_data_timeout_and_a_failure_hook() -> None:
    raw = {**N8N, "data": {"lane": "ops", "n": [1, 2]}, "timeout_s": 900, "on_failure": ["ntfy-publish"]}
    parsed = parse_runner(raw)
    assert isinstance(parsed, N8nRunner)
    assert parsed.data == {"lane": "ops", "n": [1, 2]} and parsed.timeout_s == 900


@pytest.mark.parametrize(
    "raw",
    [
        {"kind": "n8n"},  # no target
        {**N8N, "url": "https://n8n.example/webhook/x"},  # both targets
        {"kind": "n8n", "webhook": "/leading/slash"},
        {"kind": "n8n", "webhook": "-x"},
        {"kind": "n8n", "webhook": "a/../b"},
        {"kind": "n8n", "webhook": "has space"},
        {"kind": "n8n", "webhook": "q?x=1"},
        {"kind": "n8n", "url": "ftp://n8n.example/x"},
        {"kind": "n8n", "url": "n8n.example/webhook/x"},
        {"kind": "n8n", "url": "https://n8n example/x"},
        {**N8N, "timeout_s": 0},
        {**N8N, "data": {"blob": "x" * (65 * 1024)}},
        {**N8N, "extra": 1},
    ],
)
def test_rejects_bad_definitions(raw: dict[str, Any]) -> None:
    with pytest.raises(RunnerError):
        parse_runner(raw)


def test_a_manifest_takes_the_runner_table() -> None:
    manifest = CandybarManifest.model_validate(
        {"name": "n8n-job", "schedule": "0 3 * * *", "runner": {**N8N, "data": {"lane": "ops"}}}
    )
    assert manifest.runner == {**N8N, "data": {"lane": "ops"}, "timeout_s": 3600}


def test_argv_resolves_the_webhook_against_the_configured_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(n8n_runner.N8N_URL_ENV, "https://n8n.example/")
    runner = parse_runner({**N8N, "data": {"a": 1}, "timeout_s": 120})
    assert isinstance(runner, N8nRunner)
    assert n8n_runner.n8n_argv(runner) == [
        sys.executable, "-m", "candybar.n8n_webhook",
        "--url", "https://n8n.example/webhook/candybar/nightly",
        "--timeout", "120",
        "--data", '{"a":1}',
    ]
    assert n8n_runner.kill_after_seconds(runner) == 150


def test_the_default_base_is_the_homelab_n8n(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(n8n_runner.N8N_URL_ENV, raising=False)
    runner = parse_runner(N8N)
    assert isinstance(runner, N8nRunner)
    assert n8n_runner.webhook_url(runner) == "https://n8n.delo.sh/webhook/candybar/nightly"


# ---------------------------------------------------------------------------
# The client's verdict rules (in-process).


def _call(n8n: FakeN8n, timeout: float = 5) -> tuple[bool, str | None, dict[str, Any]]:
    return n8n_webhook.call(f"{n8n.base}/webhook/candybar/nightly", {"lane": "ops"}, timeout)


def test_posts_the_invocation_context_and_data(n8n: FakeN8n, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANDYBAR_ID", "cb-1")
    monkeypatch.setenv("CANDYBAR_INVOCATION_ID", "inv-1")
    monkeypatch.setenv("CANDYBAR_TRIGGER", "manual")
    monkeypatch.setenv("CANDYBAR_TICK_ID", "")
    assert _call(n8n) == (True, None, {})
    [request] = n8n.requests
    assert request["path"] == "/webhook/candybar/nightly"
    assert request["body"] == {
        "candybar": {"id": "cb-1", "invocationId": "inv-1", "trigger": "manual", "tickId": None},
        "data": {"lane": "ops"},
    }
    assert request["headers"]["X-Candybar-Invocation-Id"] == "inv-1"


@pytest.mark.parametrize(
    "body",
    ["not json", {"status": "failed", "message": "a workflow's own field"}, [{"x": 1}], [], 42, {}],
)
def test_a_2xx_without_a_verdict_is_success(n8n: FakeN8n, body: Any) -> None:
    n8n.reply(200, body)
    ok, message, _ = _call(n8n)
    assert ok and message is None


@pytest.mark.parametrize("body", ["", "  \n"])
def test_an_empty_2xx_is_a_failure(n8n: FakeN8n, body: str) -> None:
    """n8n 2.18 answers 200 with no body when a Respond-node workflow errors first."""
    n8n.reply(200, body)
    assert _call(n8n) == (False, n8n_webhook.EMPTY_BODY_MESSAGE, {})


def test_the_verdict_object_decides_and_reports_usage(n8n: FakeN8n) -> None:
    n8n.reply(200, {"candybar": {"status": "fail", "message": "3 rows rejected", "tokenSpend": 1500, "costUsd": 0.02}})
    assert _call(n8n) == (False, "3 rows rejected", {"token_spend": 1500, "cost_usd": 0.02})
    n8n.reply(200, [{"candybar": {"status": "success", "message": "done", "token_spend": 7}}, {"other": 1}])
    assert _call(n8n) == (True, "done", {"token_spend": 7})


def test_an_unknown_verdict_status_is_a_failure(n8n: FakeN8n) -> None:
    n8n.reply(200, {"candybar": {"status": "maybe"}})
    assert _call(n8n) == (False, "workflow reported status 'maybe'", {})


@pytest.mark.parametrize(
    ("usage", "metrics"),
    [
        ({"tokenSpend": -5, "costUsd": -1}, {"token_spend": 0}),
        ({"tokenSpend": True, "costUsd": "0.1"}, {}),
        ({"tokenSpend": 12.9}, {"token_spend": 12}),
        ({"tokenSpend": 2**70}, {"token_spend": 2**63 - 1}),
    ],
)
def test_usage_is_sanitized(n8n: FakeN8n, usage: dict[str, Any], metrics: dict[str, Any]) -> None:
    n8n.reply(200, {"candybar": {"status": "success", **usage}})
    assert _call(n8n)[2] == metrics


def test_a_non_2xx_is_a_failure_with_n8ns_message(n8n: FakeN8n) -> None:
    n8n.reply(500, {"code": 0, "message": "Error in workflow"})
    assert _call(n8n) == (False, "n8n answered 500: Error in workflow", {})
    n8n.reply(404, {"code": 404, "message": 'The requested webhook "POST x" is not registered.', "hint": "Activate it"})
    ok, message, _ = _call(n8n)
    assert not ok and message == 'n8n answered 404: The requested webhook "POST x" is not registered. (Activate it)'


def test_no_answer_in_time_is_a_failure(n8n: FakeN8n) -> None:
    n8n.reply(200, "{}", delay=2)
    assert _call(n8n, timeout=0.3) == (False, "n8n did not answer within 0.3s", {})


def test_no_connection_is_a_failure() -> None:
    ok, message, _ = n8n_webhook.call("http://127.0.0.1:9/webhook/x", {}, 2)
    assert not ok and message is not None and message.startswith("n8n unreachable at http://127.0.0.1:9/webhook/x")


def test_main_writes_the_ad8_result_file(n8n: FakeN8n, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = tmp_path / "result.json"
    monkeypatch.setenv("CANDYBAR_RESULT_FILE", str(result))
    n8n.reply(200, {"candybar": {"status": "fail", "message": "nope", "tokenSpend": 3}})
    code = n8n_webhook.main(["--url", f"{n8n.base}/webhook/x", "--timeout", "5", "--data", '{"k": "v"}'])
    assert code == 1
    assert json.loads(result.read_text()) == {"status": "fail", "metrics": {"token_spend": 3}, "message": "nope"}
    assert n8n.requests[0]["body"]["data"] == {"k": "v"}


# ---------------------------------------------------------------------------
# The executor path: run_target spawns the real client.


def _run(runner: dict[str, Any]):
    return run(run_target(runner=runner, root="", **IDS))


def test_success_through_the_executor_carries_reported_usage(n8n: FakeN8n) -> None:
    n8n.reply(200, {"candybar": {"status": "success", "message": "42 items", "tokenSpend": 900, "costUsd": 0.01}})
    outcome = _run({**N8N, "data": {"lane": "ops"}})
    assert outcome.exit_code == 0 and outcome.spawn_error is None
    resolved = resolve_outcome(outcome)
    assert resolved.success
    assert resolved.metrics.token_spend == 900 and resolved.metrics.cost_usd == 0.01
    [request] = n8n.requests
    assert request["path"] == "/webhook/candybar/nightly"
    assert request["body"]["candybar"] == {
        "id": str(IDS["candybar_id"]),
        "invocationId": str(IDS["invocation_id"]),
        "trigger": "scheduled",
        "tickId": "tick:42",
    }
    assert request["body"]["data"] == {"lane": "ops"}


def test_a_workflow_error_fails_the_invocation_with_its_message(n8n: FakeN8n) -> None:
    n8n.reply(500, {"message": "Error in workflow"})
    outcome = _run(N8N)
    resolved = resolve_outcome(outcome)
    assert outcome.exit_code == 1 and not resolved.success
    assert resolved.message == "n8n answered 500: Error in workflow"
    assert "Error in workflow" in outcome.stderr_tail


def test_a_reported_failure_fails_the_invocation(n8n: FakeN8n) -> None:
    n8n.reply(200, {"candybar": {"status": "fail", "message": "upstream API down", "tokenSpend": 12}})
    resolved = resolve_outcome(_run(N8N))
    assert not resolved.success and resolved.message == "upstream API down"
    assert resolved.metrics.token_spend == 12


def test_the_timeout_bounds_the_wait(n8n: FakeN8n) -> None:
    n8n.reply(200, "{}", delay=4)
    started = time.monotonic()
    resolved = resolve_outcome(_run({**N8N, "timeout_s": 1}))
    assert time.monotonic() - started < 4
    assert not resolved.success and resolved.message == "n8n did not answer within 1s"


def test_a_whole_url_skips_the_base(n8n: FakeN8n, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(n8n_runner.N8N_URL_ENV, "http://127.0.0.1:9")  # would be unreachable
    assert resolve_outcome(_run({"kind": "n8n", "url": f"{n8n.base}/webhook-test/build"})).success
    assert n8n.requests[0]["path"] == "/webhook-test/build"
