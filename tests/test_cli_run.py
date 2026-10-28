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


def test_run_by_name_resolves_then_invokes() -> None:
    listing = [{"id": CANDYBAR_ID, "name": "nightly-report"}, {"id": "x", "name": "other"}]
    responses = [_FakeResponse(listing), _FakeResponse(ACCEPTED)]  # type: ignore[arg-type]
    with patch("urllib.request.urlopen", side_effect=responses) as mock:
        result = CliRunner().invoke(app, ["run", "nightly-report"])
    assert result.exit_code == 0, result.output
    assert mock.call_args_list[0].args[0].full_url.endswith("/candybars")
    invoke = mock.call_args_list[1].args[0]
    assert invoke.get_method() == "POST"
    assert invoke.full_url.endswith(f"/candybars/{CANDYBAR_ID}/invoke")


def test_run_unknown_name_exits_2_without_invoking() -> None:
    listing = [{"id": CANDYBAR_ID, "name": "nightly-report"}]
    with patch("urllib.request.urlopen", return_value=_FakeResponse(listing)) as mock:  # type: ignore[arg-type]
        result = CliRunner().invoke(app, ["run", "nightly"])
    assert result.exit_code == 2
    assert "did you mean: nightly-report" in result.output
    assert mock.call_count == 1  # the listing only: no invoke


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


def test_run_never_sends_a_hostile_ref_as_a_path() -> None:
    """A non-UUID ref is only ever matched against names: it can never escape
    its path segment to reach another route."""
    with patch("urllib.request.urlopen", return_value=_FakeResponse([])) as mock:  # type: ignore[arg-type]
        result = CliRunner().invoke(app, ["run", "../../metrics?x=1"])
    assert result.exit_code == 2
    assert [c.args[0].full_url.rsplit("/", 1)[1] for c in mock.call_args_list] == ["candybars"]


def test_run_requires_an_id() -> None:
    result = CliRunner().invoke(app, ["run"])
    assert result.exit_code == 2


def test_run_against_the_real_app_contract(monkeypatch: Any) -> None:
    """CLI ↔ API contract: the CLI's request hits the real route, and the keys
    it prints are the ones the real 202/404 bodies carry (no hand-made fixtures)."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from urllib.parse import urlsplit
    from uuid import UUID

    from fastapi.testclient import TestClient

    import candybar.api.app as app_module

    known = UUID(CANDYBAR_ID)

    async def fake_get_candybar(session: Any, candybar_id: UUID) -> Any:
        return SimpleNamespace(id=known, name="nightly-report") if candybar_id == known else None

    class Sink:
        def __init__(self) -> None:
            self.published: list[Any] = []

        async def publish(self, event: Any) -> None:
            self.published.append(event)

    @asynccontextmanager
    async def session_factory():  # type: ignore[no-untyped-def]
        yield object()

    monkeypatch.setattr(app_module, "get_candybar", fake_get_candybar)
    sink = Sink()
    client = TestClient(app_module.create_app(session_factory=session_factory, sink=sink))

    def via_app(request: Any, timeout: float = 0) -> Any:
        resp = client.request(request.get_method(), urlsplit(request.full_url).path)
        if resp.status_code >= 400:
            raise urllib.error.HTTPError(
                request.full_url, resp.status_code, "x", hdrs=None, fp=io.BytesIO(resp.content)
            )
        return _FakeResponse(resp.json())

    with patch("urllib.request.urlopen", side_effect=via_app):
        ok = CliRunner().invoke(app, ["run", CANDYBAR_ID])
        missing = CliRunner().invoke(app, ["run", "01900000-0000-7000-8000-00000000dead"])

    assert ok.exit_code == 0, ok.output
    assert len(sink.published) == 1
    cmd = sink.published[0]
    assert str(cmd.data.invocation_id) in ok.output
    assert cmd.idempotency_key in ok.output
    assert missing.exit_code == 1
    assert json.loads(missing.output.strip())["title"] == "Candybar not found"
    assert len(sink.published) == 1
