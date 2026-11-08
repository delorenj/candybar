"""Local context sync is tested only with temporary manifests and an in-memory API."""
from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from threading import Event

import pytest

from candybar.context_sync import context_config, sync_context, watch_context
from candybar.project import render


class FakeApi:
    def __init__(self):
        self.rows = {}
        self.writes = []

    def list_candybars(self):
        return copy.deepcopy(list(self.rows.values()))

    def get_candybar(self, job_id):
        return copy.deepcopy(self.rows.get(job_id))

    def runner(self, job_id):
        row = self.rows[job_id]
        return {'runner': copy.deepcopy(row['runner']), 'entrypoint': row['entrypoint']}

    def register(self, definition):
        job_id = f'id-{len(self.rows)}'
        self.rows[job_id] = {**copy.deepcopy(definition), 'id': job_id, 'archived': False}
        self.writes.append(('register', copy.deepcopy(definition)))
        return {'id': job_id}

    def update(self, job_id, changes):
        self.rows[job_id].update(copy.deepcopy(changes))
        self.writes.append(('update', copy.deepcopy(changes)))
        return copy.deepcopy(self.rows[job_id])


def manifest(path: Path, name='job', **changes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render({'name': name, 'schedule': '0 * * * *',
                            'runner': {'kind': 'exec', 'argv': ['true']}, **changes}))
    return path


def names(api):
    return {row['name'] for row in api.rows.values()}


def test_project_file_selects_siblings_and_fails_closed(tmp_path):
    root = tmp_path / 'app'
    file = manifest(root / '.candybars' / 'first.candybar.toml', 'first')
    sibling = root / '.candybar.toml'
    sibling.write_text('broken TOML')
    config = context_config(file)
    assert config.projects == (root,) and config.roots == () and config.legacy == ()
    api = FakeApi()
    report = sync_context(file, api=api)
    assert not report.ok and not api.writes
    manifest(sibling, 'second')
    report = sync_context(file, api=api)
    assert report.ok, report.errors
    assert names(api) == {'first', 'second'}
    assert all(not row['enabled'] for row in api.rows.values())
    assert all(row['tags'] == ['project:app'] for row in api.rows.values())
    assert sync_context(file, dry_run=True, api=api).ok
    assert len(api.writes) == 2


def test_legacy_file_isolated_and_relative_entrypoint(tmp_path):
    root = tmp_path / 'app'
    file = root / 'jobs' / 'candybar.toml'
    file.parent.mkdir(parents=True)
    (file.parent / 'run.sh').write_text('#!/bin/sh\n')
    file.write_text('[candybar]\nname="legacy"\nschedule="0 * * * *"\nentrypoint="./run.sh"\n')
    manifest(root / '.candybar.toml', 'other')
    api = FakeApi()
    assert context_config(file).legacy == (file,)
    report = sync_context(file, api=api)
    assert report.ok, report.errors
    assert names(api) == {'legacy'}
    row = next(iter(api.rows.values()))
    assert row['runner']['argv'] == [str(file.parent / 'run.sh')]
    assert row['runner']['workdir'] == str(file.parent)
    assert row['tags'] == [] and row['enabled'] is False


def test_nested_project_under_outer_git_uses_nested_identity(tmp_path, monkeypatch):
    outer = tmp_path / 'bloodbank'
    outer.mkdir()
    subprocess.run(['git', 'init', '-q', str(outer)], check=True)
    nested = outer / 'services' / 'target-watch'
    manifest(nested / '.candybar.toml', 'first')
    manifest(nested / '.candybars' / 'second.candybar.toml', 'second')
    manifest(outer / 'elsewhere' / 'candybar.toml', 'unrelated')
    cwd = nested / 'src' / 'package'
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    config = context_config()
    assert config.projects == (nested,) and config.roots == ()
    api = FakeApi()
    assert sync_context(api=api).ok
    assert names(api) == {'first', 'second'}
    assert all(row['tags'] == ['project:target-watch'] for row in api.rows.values())


def test_git_root_discovers_only_pruned_local_manifests(tmp_path):
    root = tmp_path / 'checkout'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    manifest(root / '.candybar.toml', 'root-job')
    manifest(root / 'services' / 'worker' / '.candybar.toml', 'worker')
    manifest(root / 'jobs' / 'candybar.toml', 'legacy')
    for excluded in ('tests', 'fixtures', 'examples', '.worktrees', '.venv', 'node_modules'):
        manifest(root / excluded / 'candybar.toml', 'ignored')
    worktree = root / 'linked-worktree'
    manifest(worktree / '.candybar.toml', 'ignored')
    (worktree / '.git').write_text('gitdir: /source/.git/worktrees/branch\n')
    external = tmp_path / 'outside'
    manifest(external / 'candybar.toml', 'ignored')
    (root / 'symlink').symlink_to(external, target_is_directory=True)
    config = context_config(root)
    assert config.roots == (root,) and config.projects == ()
    api = FakeApi()
    report = sync_context(root, api=api)
    assert report.ok, report.errors
    assert names(api) == {'root-job', 'worker', 'legacy'}


def test_root_legacy_directory_discovers_nested_project(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    manifest(root / 'candybar.toml', 'legacy-root')
    manifest(root / 'service/.candybar.toml', 'nested')
    api = FakeApi()
    assert context_config(root).roots == (root,)
    assert sync_context(root, api=api).ok
    assert names(api) == {'legacy-root', 'nested'}


def test_no_manifest_no_api_or_global_discovery(tmp_path, monkeypatch):
    root = tmp_path / 'checkout'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    manifest(tmp_path / 'other' / 'candybar.toml', 'other')
    manifest(root / 'tests' / 'candybar.toml', 'ignored')
    monkeypatch.chdir(root)
    api = FakeApi()
    with pytest.raises(ValueError, match='no candybar manifests'):
        sync_context(api=api)
    assert not api.writes
    with pytest.raises(ValueError, match='excluded'):
        context_config(root / 'tests')
    with pytest.raises(ValueError, match='excluded'):
        context_config(root / '.git' / 'config')


def test_watch_context_uses_native_watcher(monkeypatch, tmp_path):
    manifest(tmp_path / '.candybar.toml')
    calls = []
    monkeypatch.setattr('candybar.context_sync.run_watch',
                        lambda config, *, stop_event: calls.append((config, stop_event)))
    stop = Event()
    watch_context(tmp_path, stop_event=stop)
    assert calls[0][0].projects == (tmp_path,) and calls[0][1] is stop
