"""Context sync CLI dispatch uses a fake core, never the live registry."""

import json
import sys
from types import SimpleNamespace

from typer.testing import CliRunner

from candybar.autosync import ReconcileReport
from candybar.cli.main import app


def test_sync_saved_file_passes_context_and_json(monkeypatch, tmp_path):
    calls = []
    def sync_context(path, *, dry_run):
        calls.append((path, dry_run))
        return ReconcileReport(results=[{'name': 'job', 'status': 'unchanged'}])
    monkeypatch.setitem(sys.modules, 'candybar.context_sync', SimpleNamespace(sync_context=sync_context))
    manifest = tmp_path / 'candybar.toml'
    result = CliRunner().invoke(app, ['sync', str(manifest), '--dry-run', '--json'])
    assert result.exit_code == 0, result.output
    assert calls == [(manifest, True)]
    assert json.loads(result.output)['ok'] is True


def test_sync_no_manifest_has_machine_readable_error(monkeypatch):
    def missing(*args, **kwargs):
        raise ValueError('no candybar manifests in this context')
    monkeypatch.setitem(sys.modules, 'candybar.context_sync', SimpleNamespace(sync_context=missing))
    result = CliRunner().invoke(app, ['sync', '--json'])
    assert result.exit_code == 2
    assert json.loads(result.output)['ok'] is False


def test_sync_validation_failure_is_nonzero(monkeypatch):
    monkeypatch.setitem(sys.modules, 'candybar.context_sync', SimpleNamespace(
        sync_context=lambda *a, **k: ReconcileReport(errors=['invalid sibling'])))
    result = CliRunner().invoke(app, ['sync'])
    assert result.exit_code == 1
    assert 'invalid sibling' in result.output
