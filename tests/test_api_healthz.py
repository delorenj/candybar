"""GET /healthz — container liveness probe (Story 1.9)."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from candybar.api.app import create_app


def _client() -> TestClient:
    # healthz must not touch the DB or the bus — pass exploding stand-ins to
    # prove it.
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("healthz touched a dependency")

    app = create_app(session_factory=boom, sink=boom)
    return TestClient(app, raise_server_exceptions=True)


def test_healthz_ok_without_dependencies() -> None:
    response = _client().get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
