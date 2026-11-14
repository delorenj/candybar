"""Watch CLI contracts without contacting a live Candybar API."""

import json
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from candybar.cli.main import app


@dataclass
class Report:
    results: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self):
        return not self.errors


@pytest.fixture
def core(monkeypatch):
    calls = []
    report = Report(results=[{"name": "nightly", "source": "/tmp/candybar.toml", "status": "unchanged"}])

    def load(path):
        calls.append(("load", path))
        return "settings"

    def reconcile(settings, *, dry_run=False):
        calls.append(("reconcile", settings, dry_run))
        return report

    def run(settings, *, config_path, launcher_pid=None):
        calls.append(("watch", settings, config_path))

    module = SimpleNamespace(load_config=load, reconcile=reconcile, run_watch=run)
    monkeypatch.setitem(sys.modules, "candybar.autosync", module)
    def context(path):
        calls.append(("context", path))
        return "settings"
    monkeypatch.setitem(sys.modules, "candybar.context_sync", SimpleNamespace(context_config=context))
    return calls, report, module


def test_watch_dry_run_is_one_shot(core, tmp_path):
    calls, _, _ = core
    result = CliRunner().invoke(app, ["watch", "-c", str(tmp_path / "watch.toml"), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert calls[1] == ("reconcile", "settings", True)
    assert len(calls) == 2
    assert "unchanged nightly" in result.output


def test_watch_json_report_contains_computed_ok(core):
    _, _, _ = core
    result = CliRunner().invoke(app, ["watch", "--once", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"] is True


def test_watch_failed_reconcile_is_nonzero(core):
    _, report, _ = core
    report.errors.append("duplicate source")
    result = CliRunner().invoke(app, ["watch", "--once", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["ok"] is False


def test_watch_config_error_is_usage_error(core):
    _, _, module = core

    def invalid(path):
        raise ValueError("unknown key")

    module.load_config = invalid
    result = CliRunner().invoke(app, ["watch", "--config", "/tmp/watch.toml", "--once"])
    assert result.exit_code == 2
    assert "unknown key" in result.output


def test_watch_config_type_error_is_usage_error(core):
    _, _, module = core

    def invalid(path):
        raise TypeError("roots must be an array")

    module.load_config = invalid
    result = CliRunner().invoke(app, ["watch", "--config", "/tmp/watch.toml", "--once"])
    assert result.exit_code == 2
    assert "roots must be an array" in result.output


def test_watch_json_requires_one_shot(core):
    calls, _, _ = core
    result = CliRunner().invoke(app, ["watch", "--json"])
    assert result.exit_code == 2
    assert calls == []


def test_watch_discovers_context_without_reading_global_config(core, monkeypatch, tmp_path):
    calls, _, _ = core
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["watch"])
    assert result.exit_code == 0, result.output
    assert calls == [("context", None), ("watch", "settings", None)]


@pytest.mark.parametrize('message,expected', [
    ('no candybar manifests in context: /empty', 0),
    ('excluded candybar context: /repo/tests', 2),
    ('unsafe candybar context: /symlink', 2),
])
def test_if_present_only_suppresses_empty_context(core, monkeypatch, message, expected):
    calls, _, _ = core

    def invalid(path):
        raise ValueError(message)

    monkeypatch.setitem(sys.modules, 'candybar.context_sync', SimpleNamespace(context_config=invalid))
    result = CliRunner().invoke(app, ['watch', '--if-present'])
    assert result.exit_code == expected, result.output
    assert not calls
    if expected == 0:
        assert not result.output
    else:
        assert message in result.output


def test_watch_rejects_path_with_config(core):
    calls, _, _ = core
    result = CliRunner().invoke(app, ['watch', '/repo', '--config', '/watch.toml'])
    assert result.exit_code == 2
    assert not calls


def test_watch_backend_failure_is_nonzero(core):
    _, _, module = core

    def failure(*args, **kwargs):
        raise RuntimeError("native backend unavailable")

    module.run_watch = failure
    result = CliRunner().invoke(app, ["watch"])
    assert result.exit_code == 1
    assert "native backend unavailable" in result.output
