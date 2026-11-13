"""Autosync contract tests: all registry calls are in-memory, never live."""
from __future__ import annotations

import copy
import logging
import os
import threading
import time
from pathlib import Path

import pytest

from candybar import autosync
from candybar.autosync import WatchConfig, load_config, reconcile, run_watch
from candybar.migration.client import ApiError
from candybar.project import render


@pytest.fixture(autouse=True)
def _private_runtime_dir(tmp_path, monkeypatch):
    """The per-registry sync flock lives in XDG_RUNTIME_DIR; never contend with live watchers."""
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path / '_run'))


class FakeApi:
    def __init__(self):
        self.rows = {}
        self.writes = []
        self.reads = 0
        self.ignore_updates = False
        self.on_write = None

    def list_candybars(self):
        self.reads += 1
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
        if self.on_write:
            self.on_write()
        return {'id': job_id}

    def update(self, job_id, changes):
        self.writes.append(('update', job_id, copy.deepcopy(changes)))
        if not self.ignore_updates:
            self.rows[job_id].update(copy.deepcopy(changes))
        if self.on_write:
            self.on_write()
        return copy.deepcopy(self.rows[job_id])


def manifest(path, name='job', *, multi=False, **changes):
    path.parent.mkdir(parents=True, exist_ok=True)
    definition = {'name': name, 'schedule': '0 * * * *', 'enabled': True,
                  'runner': {'kind': 'exec', 'argv': ['true']}, **changes}
    path.write_text(render(definition, style='multi' if multi else 'single'))
    return path


def only_row(api):
    return next(iter(api.rows.values()))


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    done = threading.Event()
    while time.monotonic() < deadline:
        if predicate():
            return
        done.wait(0.02)
    pytest.fail('timed out waiting for native watcher')


def test_config_paths_defaults_and_validation(tmp_path):
    config_file = tmp_path / 'watch.toml'
    config_file.write_text('roots=["src"]\nprojects=["../project"]\nlegacy=["~/job/candybar.toml"]\nledger="ledger.yaml"\n')
    config = load_config(config_file)
    assert config.roots == (tmp_path / 'src',)
    assert config.projects == (tmp_path.parent / 'project',)
    assert config.legacy == (Path.home() / 'job/candybar.toml',)
    assert config.ledger == tmp_path / 'ledger.yaml'
    config_file.write_text('')
    with pytest.raises(ValueError, match='requires roots'):
        load_config(config_file)
    for text in ('wat=true', 'roots="bad"', 'retries=-1', 'debounce_ms=0', 'create_missing="yes"'):
        config_file.write_text(text)
        with pytest.raises((ValueError, TypeError)):
            load_config(config_file)


def test_both_formats_nested_discovery_and_paused_creation(tmp_path):
    root = tmp_path / 'enrolled'
    project = root / 'outer' / 'nested'
    manifest(project / '.candybar.toml', 'multi', multi=True)
    manifest(project / '.candybars' / 'single.candybar.toml', 'single')
    manifest(root / 'legacy' / 'candybar.toml', 'legacy')
    api = FakeApi()
    report = reconcile(WatchConfig((root,)), api)
    assert report.ok, report.errors
    assert len(api.rows) == 3
    assert all(row['enabled'] is False for row in api.rows.values())
    rows = {row['name']: row for row in api.rows.values()}
    assert rows['multi']['tags'] == ['project:nested']
    assert rows['single']['runner']['workdir'] == str(project)
    assert rows['legacy']['tags'] == []
    assert reconcile(WatchConfig((root,)), api).ok
    assert len(api.writes) == 3


def test_nested_project_uses_directory_identity_not_parent_git(tmp_path):
    import subprocess
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    project = tmp_path / 'services' / 'target-watch'
    manifest(project / '.candybar.toml')
    api = FakeApi()
    assert reconcile(WatchConfig((), projects=(project,)), api).ok
    assert only_row(api)['tags'] == ['project:target-watch']


@pytest.mark.parametrize('bad', ['broken TOML', '[candybars.bad]\nschedule="invalid"'])
def test_invalid_sibling_blocks_every_group(tmp_path, bad):
    root = tmp_path / 'root'
    manifest(root / 'p' / '.candybar.toml')
    (root / 'p' / '.candybars').mkdir()
    (root / 'p' / '.candybars' / 'bad.candybar.toml').write_text(bad)
    manifest(root / 'other' / 'candybar.toml', 'other')
    api = FakeApi()
    assert not reconcile(WatchConfig((root,)), api).ok
    assert api.writes == []


def test_global_duplicates_and_project_identity_collisions(tmp_path):
    manifest(tmp_path / 'a' / '.candybar.toml')
    manifest(tmp_path / 'b' / 'candybar.toml')
    api = FakeApi()
    report = reconcile(WatchConfig((tmp_path,)), api)
    assert not report.ok and 'duplicate manifest claim' in str(report.errors)
    assert not api.writes
    (tmp_path / 'b' / 'candybar.toml').unlink()
    manifest(tmp_path / 'nested' / 'a' / '.candybar.toml', 'different')
    report = reconcile(WatchConfig((tmp_path,)), api)
    assert not report.ok and 'duplicate project identity' in str(report.errors)
    assert not api.writes


def test_exclusions_symlinks_and_linked_worktree(tmp_path):
    root = tmp_path / 'root'
    manifest(root / 'real' / '.candybar.toml', 'real')
    for ignored in ('tests', 'fixtures', 'examples', '.git', '.venv', '.agents', 'node_modules', 'build', 'worktrees'):
        manifest(root / ignored / 'candybar.toml', 'real')
    external = tmp_path / 'external'
    manifest(external / 'candybar.toml', 'real')
    (root / 'linked').symlink_to(external, target_is_directory=True)
    worktree = root / 'linked-worktree'
    manifest(worktree / '.candybar.toml', 'real')
    (worktree / '.git').write_text('gitdir: /source/.git/worktrees/branch\n')
    api = FakeApi()
    assert reconcile(WatchConfig((root,)), api).ok
    assert len(api.rows) == 1
    assert not reconcile(WatchConfig((), projects=(worktree,)), api).ok
    (root / 'real' / '.candybar.toml').unlink()
    (root / 'real' / '.candybar.toml').symlink_to(external / 'candybar.toml')
    assert not reconcile(WatchConfig((root,)), api).ok


def test_updates_readback_and_preserves_lifecycle(tmp_path):
    file = manifest(tmp_path / '.candybar.toml')
    config = WatchConfig((), projects=(tmp_path,))
    api = FakeApi()
    assert reconcile(config, api).ok
    row = only_row(api)
    row['enabled'] = True
    row['skips'] = ['operator choice']
    manifest(file, description='changed', schedule='5 * * * *', enabled=False)
    report = reconcile(config, api)
    assert report.ok, report.errors
    assert row['enabled'] is True and row['skips'] == ['operator choice']
    assert set(api.writes[-1][2]) == {'description', 'schedule'}
    row['archived'] = True
    manifest(file, description='archived change')
    writes = len(api.writes)
    assert reconcile(config, api).results[0]['status'] == 'archived'
    assert len(api.writes) == writes
    row['archived'] = False
    api.ignore_updates = True
    report = reconcile(config, api)
    assert not report.ok and 'readback still drifted' in str(report.errors)
    assert report.results[0]['status'] == 'error'


def test_source_removal_is_reported_without_registry_mutation(tmp_path):
    file = manifest(tmp_path / 'candybar.toml')
    config = WatchConfig((), legacy=(file,))
    api = FakeApi()
    assert reconcile(config, api).ok
    file.unlink()
    reads = api.reads
    report = reconcile(config, api)
    assert api.reads == reads
    assert report.ok and report.results == [{'name': 'job', 'source': '', 'status': 'source-removed'}]
    assert len(api.rows) == 1 and len(api.writes) == 1


def test_missing_known_registration_never_revived(tmp_path):
    file = manifest(tmp_path / 'candybar.toml')
    config = WatchConfig((), legacy=(file,))
    api = FakeApi()
    assert reconcile(config, api).ok
    api.rows.clear()
    report = reconcile(config, api)
    assert not report.ok
    assert len(api.writes) == 1


def test_adoption_foreign_owners_and_legacy_conflict(tmp_path):
    file = manifest(tmp_path / '.candybar.toml')
    config = WatchConfig((), projects=(tmp_path,))
    api = FakeApi()
    assert reconcile(config, api).ok
    row = only_row(api)
    row['tags'] = []
    assert reconcile(config, api).ok
    assert row['tags'] == [f'project:{tmp_path.name}']
    row['tags'] = ['project:foreign']
    writes = len(api.writes)
    assert not reconcile(config, api).ok
    assert len(api.writes) == writes
    legacy = manifest(tmp_path / 'candybar.toml')
    assert not reconcile(WatchConfig((), legacy=(legacy,)), api).ok
    manifest(legacy, tags=['project:foreign'])
    assert not reconcile(WatchConfig((), legacy=(legacy,)), FakeApi()).ok
    assert file.exists()


def test_foreign_owner_preflight_blocks_earlier_valid_update(tmp_path):
    first = manifest(tmp_path / 'a' / 'candybar.toml', 'first')
    second = manifest(tmp_path / 'b' / 'candybar.toml', 'second')
    config = WatchConfig((tmp_path,))
    api = FakeApi()
    assert reconcile(config, api).ok
    rows = {row['name']: row for row in api.rows.values()}
    rows['second']['tags'] = ['project:foreign']
    manifest(first, 'first', description='must not apply')
    manifest(second, 'second', description='foreign')
    before = len(api.writes)
    report = reconcile(config, api)
    assert not report.ok and 'foreign project ownership' in str(report.errors)
    assert len(api.writes) == before
    assert rows['first']['description'] != 'must not apply'


def test_dry_run_create_missing_and_entrypoint_conversion(tmp_path):
    script = tmp_path / 'run.sh'
    script.write_text('#!/bin/sh\ntrue\n')
    file = tmp_path / 'candybar.toml'
    file.write_text('[candybar]\nname="job"\nschedule="0 * * * *"\nentrypoint="run.sh"\n')
    config = WatchConfig((), legacy=(file,))
    api = FakeApi()
    report = reconcile(config, api, dry_run=True)
    assert report.ok and report.results[0]['status'] == 'would-create-disabled'
    assert api.writes == []
    config.create_missing = False
    assert reconcile(config, api).results[0]['status'] == 'missing'
    config.create_missing = True
    assert reconcile(config, api).ok
    row = only_row(api)
    assert row['entrypoint'] is None
    assert row['runner']['argv'] == [str(script)]
    assert row['runner']['workdir'] == str(tmp_path)
    manifest(file, description='drift')
    assert reconcile(config, api, dry_run=True).results[0]['status'] == 'would-update'
    assert len(api.writes) == 1


def migration(tmp_path, status='verified', candybar_id='id-0', disable_pending=False):
    import yaml
    file = manifest(tmp_path / 'deploy/migration/manifests/job/candybar.toml')
    ledger = file.parents[2] / 'ledger.yaml'
    job = {'id': 'systemd:job.timer', 'wave': '7.5', 'candybar': {
        'name': 'job', 'schedule': '0 * * * *', 'runner': {'kind': 'exec', 'argv': ['true']}},
        'native': {'kind': 'systemd-timer', 'unit': 'job.timer', 'schedule': 'hourly'},
        'source_of_truth': {'repo': 'test', 'path': 'job.service'},
        'status': status, 'candybar_id': candybar_id, 'disable_pending': disable_pending}
    ledger.write_text(yaml.safe_dump({'version': 1, 'jobs': [job]}))
    return file, ledger, job


@pytest.mark.parametrize('status', ['planned', 'rolled-back', 'decommissioned'])
def test_migration_inactive_never_created(tmp_path, status):
    file, _, _ = migration(tmp_path, status=status)
    api = FakeApi()
    report = reconcile(WatchConfig((), legacy=(file,)), api)
    assert report.ok and report.results[0]['status'] == 'migration-inactive'
    assert not api.writes


def test_migration_exact_identity_lock_and_reread(tmp_path, monkeypatch):
    from contextlib import contextmanager
    file, ledger, _ = migration(tmp_path)
    api = FakeApi()
    normal = manifest(tmp_path / 'normal/candybar.toml')
    assert reconcile(WatchConfig((), legacy=(normal,)), api).ok
    config = WatchConfig((), legacy=(file,))
    manifest(file, description='updated')
    locked_paths = []
    real_lock = autosync.locked

    @contextmanager
    def observing_lock(path, **kw):
        with real_lock(path, **kw):
            locked_paths.append(path)
            yield
    monkeypatch.setattr(autosync, 'locked', observing_lock)
    assert reconcile(config, api).ok
    assert locked_paths == [ledger]
    assert only_row(api)['description'] == 'updated'
    assert not reconcile(WatchConfig((), legacy=(file,), ledger=tmp_path / 'missing'), api).ok
    only_row(api)['id'] = 'wrong'
    api.rows['wrong'] = api.rows.pop('id-0')
    report = reconcile(config, api)
    assert not report.ok and 'exact ledger candybar_id' in str(report.errors)


@pytest.mark.parametrize('mode', ['pending', 'missing-id', 'missing', 'malformed', 'duplicate-name', 'duplicate-id'])
def test_migration_fail_closed(tmp_path, mode):
    import yaml
    file, ledger, job = migration(tmp_path, disable_pending=mode == 'pending',
                                  candybar_id=None if mode == 'missing-id' else 'id-0')
    if mode == 'missing':
        ledger.unlink()
    elif mode == 'malformed':
        ledger.write_text('jobs: [ broken')
    elif mode.startswith('duplicate'):
        other = copy.deepcopy(job)
        other['id'] = 'systemd:other.timer'
        if mode == 'duplicate-id':
            other['candybar']['name'] = 'other'
        ledger.write_text(yaml.safe_dump({'version': 1, 'jobs': [job, other]}))
    api = FakeApi()
    report = reconcile(WatchConfig((), legacy=(file,)), api)
    assert not report.ok, report.results
    assert not api.writes


def test_empty_watch_and_native_backend_error_are_visible(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='at least one'):
        run_watch(WatchConfig(()))
    def failed(*args):
        raise RuntimeError('native backend unavailable')
    monkeypatch.setattr(autosync, 'RustNotify', failed)
    with pytest.raises(RuntimeError, match='native backend unavailable'):
        run_watch(WatchConfig((tmp_path,)))


def test_native_atomic_replace_new_directory_and_rename(tmp_path, monkeypatch):
    """Real inotify events; API is a fake. No recurring scan can rescue this."""
    api = FakeApi()
    monkeypatch.setattr(autosync, 'CandybarApi', lambda: api)
    project = tmp_path / 'project'
    project.mkdir()
    stop = threading.Event()
    errors = []
    config = WatchConfig((), projects=(project,), debounce_ms=30, retries=0)
    native_ready = threading.Event()
    real_native = autosync.RustNotify
    def native(*args):
        assert args[2] is False and args[4] is False
        watcher = real_native(*args)
        native_ready.set()
        return watcher
    monkeypatch.setattr(autosync, 'RustNotify', native)
    def run():
        try:
            run_watch(config, stop_event=stop)
        except Exception as exc:  # noqa: BLE001 -- surface background failures to this test
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert native_ready.wait(3)
        directory = project / '.candybars'
        temp = manifest(project / 'temporary', description='created')
        directory.mkdir()
        os.replace(temp, directory / 'job.candybar.toml')
        wait_for(lambda: len(api.rows) == 1)
        row = only_row(api)
        assert row['description'] == 'created'
        temp = manifest(directory / 'replacement', description='atomic')
        os.replace(temp, directory / 'job.candybar.toml')
        wait_for(lambda: row['description'] == 'atomic')
        os.rename(directory / 'job.candybar.toml', directory / 'renamed.candybar.toml')
        manifest(directory / 'renamed.candybar.toml', description='after rename')
        wait_for(lambda: row['description'] == 'after rename')
        before = api.reads
        (project / 'irrelevant.py').write_text('not a candybar')
        threading.Event().wait(0.25)
        assert api.reads == before
        (directory / 'renamed.candybar.toml').unlink()
        threading.Event().wait(0.1)
        assert len(api.rows) == 1  # No pruning on deletion.
    finally:
        stop.set()
        thread.join(3)
    assert not thread.is_alive()
    assert errors == []


def test_events_during_startup_api_are_not_lost(tmp_path, monkeypatch):
    file = manifest(tmp_path / '.candybar.toml')
    api = FakeApi()
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    def during_write():
        entered.set()
        assert release.wait(3)
    api.on_write = during_write
    monkeypatch.setattr(autosync, 'CandybarApi', lambda: api)
    thread = threading.Thread(target=run_watch, args=(WatchConfig((), projects=(tmp_path,), retries=0, debounce_ms=20),), kwargs={'stop_event': stop})
    thread.start()
    try:
        assert entered.wait(3)
        manifest(file, description='latest during API')
        api.on_write = None
        release.set()
        wait_for(lambda: only_row(api)['description'] == 'latest during API')
    finally:
        release.set()
        stop.set()
        thread.join(3)
    assert not thread.is_alive()


def test_retry_reads_latest_then_remains_available(tmp_path, monkeypatch, caplog):
    file = manifest(tmp_path / '.candybar.toml')
    api = FakeApi()
    count = 0
    def factory():
        nonlocal count
        count += 1
        if count == 1:
            manifest(file, description='latest retry bytes')
            raise ApiError('temporary outage')
        return api
    # API construction is outside the inner per-job handler too.
    monkeypatch.setattr(autosync, 'CandybarApi', factory)
    stop = threading.Event()
    caplog.set_level(logging.ERROR, logger='candybar.autosync')
    thread = threading.Thread(target=run_watch, args=(WatchConfig((), projects=(tmp_path,), retries=1, debounce_ms=20),), kwargs={'stop_event': stop})
    thread.start()
    try:
        wait_for(lambda: bool(api.rows))
        assert only_row(api)['description'] == 'latest retry bytes'
    finally:
        stop.set()
        thread.join(3)
    assert 'unsynced' in caplog.text


def test_config_atomic_edit_exits_for_restart(tmp_path, monkeypatch):
    config_file = tmp_path / 'watch.toml'
    config_file.write_text('roots=[]')
    api = FakeApi()
    ready = threading.Event()
    monkeypatch.setattr(autosync, 'CandybarApi', lambda: (ready.set(), api)[1])
    manifest(tmp_path / '.candybar.toml')
    errors, stop = [], threading.Event()
    def run():
        try:
            run_watch(WatchConfig((), projects=(tmp_path,), debounce_ms=20), config_path=config_file, stop_event=stop)
        except RuntimeError as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert ready.wait(3)
        temp = tmp_path / 'replacement'
        temp.write_text('roots=[]\n# changed')
        os.replace(temp, config_file)
        thread.join(3)
        assert not thread.is_alive()
        assert 'configuration changed' in str(errors)
    finally:
        stop.set()
        thread.join(3)


# --- CAND-53: the ledger lock must not be held across a registry pass ----------


class LockProbeApi(FakeApi):
    """Every registry call tries to take the ledger lock like a concurrent `migrate`."""

    def __init__(self, ledger):
        super().__init__()
        self.ledger = ledger
        self.probes = []

    def _probe(self):
        from candybar.migration.ledger import LedgerError, locked
        try:
            with locked(self.ledger):
                self.probes.append(True)
        except LedgerError:
            self.probes.append(False)

    def list_candybars(self):
        self._probe()
        return super().list_candybars()

    def get_candybar(self, job_id):
        self._probe()
        return super().get_candybar(job_id)

    def runner(self, job_id):
        self._probe()
        return super().runner(job_id)


def _adopted_migration(tmp_path):
    file, ledger, _ = migration(tmp_path)
    plain = manifest(tmp_path / 'normal/candybar.toml')
    seed = FakeApi()
    assert reconcile(WatchConfig((), legacy=(plain,)), seed).ok
    api = LockProbeApi(ledger)
    api.rows = seed.rows
    return file, ledger, api


def test_registry_reads_do_not_hold_the_migration_ledger_lock(tmp_path):
    file, ledger, api = _adopted_migration(tmp_path)
    assert reconcile(WatchConfig((), legacy=(file,)), api).ok
    assert api.probes and all(api.probes), api.probes


def test_noop_reconcile_never_touches_the_ledger_lock(tmp_path, monkeypatch):
    from contextlib import contextmanager
    file, ledger, api = _adopted_migration(tmp_path)
    taken = []

    @contextmanager
    def spy(path, **kw):
        taken.append(path)
        yield
    monkeypatch.setattr(autosync, 'locked', spy)
    before = sorted((p.name, p.stat().st_mtime_ns) for p in ledger.parent.iterdir())
    for _ in range(3):
        assert reconcile(WatchConfig((), legacy=(file,)), api).ok
    assert taken == []
    assert before == sorted((p.name, p.stat().st_mtime_ns) for p in ledger.parent.iterdir())


def test_migration_write_waits_a_bounded_time_for_a_running_migrate(tmp_path, monkeypatch):
    from candybar.migration.ledger import locked
    file, ledger, api = _adopted_migration(tmp_path)
    manifest(file, description='changed')
    monkeypatch.setattr(autosync, 'LEDGER_WAIT_S', 0.2)
    with locked(ledger):
        report = reconcile(WatchConfig((), legacy=(file,)), api)
    assert not report.ok and 'another `candybar migrate`' in str(report.errors)
    assert not [w for w in api.writes if w[0] == 'update' and w[2].get('description')]
    assert reconcile(WatchConfig((), legacy=(file,)), api).ok  # released: applied
    assert only_row(api)['description'] == 'changed'


def test_two_reconcilers_on_one_tree_leave_the_lock_free_for_migrate(tmp_path, monkeypatch):
    import time
    from candybar.migration.ledger import locked
    file, ledger, _ = _adopted_migration(tmp_path)
    api = FakeApi()
    plain = manifest(tmp_path / 'again/candybar.toml')
    assert reconcile(WatchConfig((), legacy=(plain,)), api).ok
    slow = api.list_candybars

    def slow_list():
        time.sleep(0.15)
        return slow()
    api.list_candybars = slow_list
    stop = threading.Event()
    config = WatchConfig((), legacy=(file,))

    def watcher():
        while not stop.is_set():
            reconcile(config, api)
    threads = [threading.Thread(target=watcher, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    try:
        time.sleep(0.1)
        started = time.monotonic()
        with locked(ledger, wait=2):  # what `candybar migrate` does
            pass
        assert time.monotonic() - started < 1
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)
