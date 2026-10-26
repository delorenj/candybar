"""Scaffold guard tests: Structural Seed layout, pinned stack, CLI entry point.

These tests codify Story 1.1's acceptance criteria: the layout must match the
architecture spine's Structural Seed and the dependency pins must match the
spine's locked stack table exactly. If a later story intentionally changes a
pin, it changes EXPECTED_PINS here in the same commit — the test is the
tripwire, the spine is the authority.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from candybar import __version__
from candybar.cli.main import app

ROOT = Path(__file__).resolve().parent.parent

EXPECTED_PINS = {
    "fastapi": "0.162.0",
    "uvicorn": "0.54.0",
    "nats-py": "2.16.0",
    "croniter": "6.2.4",
    "psycopg": "3.3.6",
    "sqlalchemy": "2.1.6",
    "alembic": "1.20.1",
    "prometheus-client": "0.26.0",
    "pydantic": "2.14.1",
    "typer": "0.27.3",
}

SEED_DIRS = [
    "src/candybar/heartbeat",
    "src/candybar/dispatcher",
    "src/candybar/executor",
    "src/candybar/registry",
    "src/candybar/api",
    "src/candybar/cli",
    "src/candybar/events",
    "sdks/python",
    "sdks/node",
    "sdks/rust",
    "deploy",
    "schemas",
]

ROLE_PACKAGES = ["heartbeat", "dispatcher", "executor", "registry", "api", "cli", "events"]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("seed_dir", SEED_DIRS)
def test_structural_seed_directory_exists(seed_dir: str) -> None:
    path = ROOT / seed_dir
    assert path.is_dir(), f"missing Structural Seed directory: {seed_dir}"


@pytest.mark.parametrize("package", ROLE_PACKAGES)
def test_role_package_importable(package: str) -> None:
    __import__(f"candybar.{package}")


def _parse_pins() -> dict[str, str]:
    """Parse project.dependencies into {name: version}, requiring exact == pins."""
    parsed: dict[str, str] = {}
    for dep in _pyproject()["project"]["dependencies"]:
        name, sep, version = dep.partition("==")
        assert sep, f"runtime dependency is not exact-pinned with '==': {dep}"
        parsed[name] = version
    return parsed


def test_dependency_pins_match_spine() -> None:
    parsed = _parse_pins()
    for name, version in EXPECTED_PINS.items():
        assert parsed.get(name) == version, (
            f"spine pin drifted: expected {name}=={version}, "
            f"found {name}=={parsed.get(name)!r} in pyproject.toml"
        )


def test_requires_python_floor() -> None:
    assert _pyproject()["project"]["requires-python"] == ">=3.13"


def test_version_exposed() -> None:
    assert __version__ == "0.1.0"


def test_cli_help_exits_zero() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output


def test_cli_no_args_shows_help_without_traceback() -> None:
    result = CliRunner().invoke(app, [])
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output
    assert "Traceback" not in result.output


def test_cli_version_flag() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert f"candybar {__version__}" in result.output
