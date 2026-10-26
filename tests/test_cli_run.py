"""Unit tests for `candybar run <id>` (CLI → POST /candybars/{id}/invoke).

Network is stubbed at urllib.request.urlopen.
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any, Self
from unittest.mock import patch

from typer.testing import CliRunner

from candybar.cli.main import app

CANDYBAR_ID = "01900000-0000-7000-8000-0000000000aa"


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None


ACCEPTED = {
    "candybarId": CANDYBAR_ID,
    "name": "nightly-report",
    "invocationId": "0b5c6d1e-1111-4222-8333-444455556666",
    "commandId": "9f8e7d6c-1111-4222-8333-444455556666",
    "idempotencyKey": f"candybar:{CANDYBAR_ID}:manual:9f8e7d6c-1111-4222-8333-444455556666",
    "trigger": "manual",
}


def _problem(status: int, title: str, detail: str) -> urllib.error.HTTPError:
    body = json.dumps(
        {"type": "about:blank", "title": title, "status": status, "detail": detail}
    ).encode()
    return urllib.error.HTTPError(
        url="http://x", code=status, msg=title, hdrs=None, fp=io.BytesIO(body)
    )


def test_run_posts_invoke_and_prints_invocation(monkeypatch: Any) -> None:
    monkeypatch.setenv("CANDYBAR_API_URL", "http://candybar.example:8700/")
    with patch("urllib.request.urlopen", return_value=_FakeResponse(ACCEPTED)) as mock:
        result = CliRunner().invoke(app, ["run", CANDYBAR_ID])
    assert result.exit_code == 0, result.output
    assert "invoked nightly-report" in result.output
    assert ACCEPTED["invocationId"] in result.output
    assert ACCEPTED["idempotencyKey"] in result.output
    request = mock.call_args.args[0]
    assert request.get_method() == "POST"
    assert request.full_url == f"http://candybar.example:8700/candybars/{CANDYBAR_ID}/invoke"


def test_run_unknown_id_prints_problem_json_exits_1() -> None:
    err = _problem(404, "Candybar not found", f"no candybar registered with id {CANDYBAR_ID}")
    with patch("urllib.request.urlopen", side_effect=err):
        result = CliRunner().invoke(app, ["run", CANDYBAR_ID])
    assert result.exit_code == 1
    printed = json.loads(result.output.strip())
    assert printed["status"] == 404 and printed["title"] == "Candybar not found"


def test_run_malformed_id_prints_problem_json_exits_1() -> None:
    err = _problem(422, "Invalid request body", "uuid_parsing")
    with patch("urllib.request.urlopen", side_effect=err):
        result = CliRunner().invoke(app, ["run", "not-a-uuid"])
    assert result.exit_code == 1
    assert json.loads(result.output.strip())["status"] == 422


def test_run_bus_down_prints_problem_json_exits_1() -> None:
    err = _problem(503, "Event bus unavailable", "could not publish the invoke command")
    with patch("urllib.request.urlopen", side_effect=err):
        result = CliRunner().invoke(app, ["run", CANDYBAR_ID])
    assert result.exit_code == 1
    assert json.loads(result.output.strip())["status"] == 503


def test_run_api_unreachable_exits_1() -> None:
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
        result = CliRunner().invoke(app, ["run", CANDYBAR_ID])
    assert result.exit_code == 1
    assert "unreachable" in result.output


def test_run_quotes_the_id_segment() -> None:
    """A hostile id can never escape its path segment to reach another route."""
    with patch("urllib.request.urlopen", return_value=_FakeResponse(ACCEPTED)) as mock:
        CliRunner().invoke(app, ["run", "../../metrics?x=1"])
    url = mock.call_args.args[0].full_url
    assert url.endswith("/candybars/..%2F..%2Fmetrics%3Fx%3D1/invoke")


def test_run_requires_an_id() -> None:
    result = CliRunner().invoke(app, ["run"])
    assert result.exit_code == 2
