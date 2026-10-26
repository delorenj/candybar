"""Unit tests for `candybar register` (CLI → POST /candybars).

Network is stubbed at urllib.request.urlopen; manifest handling uses tmp dirs.
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any, Self
from unittest.mock import patch

from typer.testing import CliRunner

from candybar.cli.main import app


def _manifest_dir(tmp_path: Path, *, name: str = "demo", schedule: str = "* * * * *") -> Path:
    (tmp_path / "run.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tmp_path / "candybar.toml").write_text(
        f'[candybar]\nname = "{name}"\nschedule = "{schedule}"\nentrypoint = "./run.sh"\n',
        encoding="utf-8",
    )
    return tmp_path


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_register_happy_path_prints_id_and_exits_zero(tmp_path: Path) -> None:
    d = _manifest_dir(tmp_path)
    created = {"id": "00000000-0000-7000-8000-000000000001", "name": "demo"}
    with patch("urllib.request.urlopen", return_value=_FakeResponse(created)) as mock:
        result = CliRunner().invoke(app, ["register", str(d)])
    assert result.exit_code == 0, result.output
    assert "registered demo" in result.output
    assert "00000000-0000-7000-8000-000000000001" in result.output
    # The POST carried the validated manifest.
    request = mock.call_args.args[0]
    sent = json.loads(request.data.decode())
    assert sent["name"] == "demo" and sent["schedule"] == "* * * * *"
    assert request.full_url.endswith("/candybars")


def test_register_invalid_manifest_exits_2_without_network(tmp_path: Path) -> None:
    d = _manifest_dir(tmp_path, name="Bad_Name")
    with patch("urllib.request.urlopen") as mock:
        result = CliRunner().invoke(app, ["register", str(d)])
    assert result.exit_code == 2
    mock.assert_not_called()


def test_register_missing_manifest_exits_2(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["register", str(tmp_path)])
    assert result.exit_code == 2
    assert "no candybar.toml" in result.output


def test_register_api_rejection_prints_problem_json_exits_1(tmp_path: Path) -> None:
    d = _manifest_dir(tmp_path)
    body = json.dumps(
        {"type": "about:blank", "title": "Candybar name already registered", "status": 409,
         "detail": "name 'demo' is taken"}
    ).encode()
    err = urllib.error.HTTPError(
        url="http://x/candybars", code=409, msg="Conflict", hdrs=None, fp=io.BytesIO(body)
    )
    with patch("urllib.request.urlopen", side_effect=err):
        result = CliRunner().invoke(app, ["register", str(d)])
    assert result.exit_code == 1
    assert '"status": 409' in result.output or '"status":409' in result.output


def test_register_api_unreachable_exits_1(tmp_path: Path) -> None:
    d = _manifest_dir(tmp_path)
    err = urllib.error.URLError("Connection refused")
    with patch("urllib.request.urlopen", side_effect=err):
        result = CliRunner().invoke(app, ["register", str(d)])
    assert result.exit_code == 1
    assert "unreachable" in result.output
