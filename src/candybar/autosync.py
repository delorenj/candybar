"""Event-driven, fail-closed synchronization of enrolled Candybar manifests.

No inventory/clock lifecycle changes happen here: only definition fields are
synced, and new non-migration registrations are always disabled. Native watches
are non-recursive on a pruned directory inventory, so excluded trees do not
consume inotify watches. watchfiles 1.1.1's RustNotify is used directly to make
watcher readiness explicit before the first API request.
"""
from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import queue
import signal
import threading
import tomllib
from collections import Counter
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from watchfiles._rust_notify import RustNotify

from .migration.client import ApiError, CandybarApi
from .migration.ledger import load_ledger, locked
from .project import (
    CANDYBARS_DIR,
    PROJECT_FILE,
    SYNCED_FIELDS,
    CandybarDef,
    Project,
    drift,
    load_file,
    project_at,
)
from .runners import canonical_runner

log = logging.getLogger(__name__)
EXCLUDED = frozenset({
    '.git', '.venv', 'venv', '.worktrees', 'worktrees', 'node_modules',
    '.cache', 'cache', '__pycache__', '.pytest_cache', '.mypy_cache',
    '.ruff_cache', '.tox', 'vendor', 'build', 'dist', 'target',
    'test', 'tests', 'fixtures', 'examples', '.agents', '.claude', '.codex',
    '.gemini', '.kimi', 'kimi-code', '.opencode', 'skills', '.codegraph',
})


@dataclass
class WatchConfig:
    roots: tuple[Path, ...]
    projects: tuple[Path, ...] = ()
    legacy: tuple[Path, ...] = ()
    ledger: Path | None = None
    create_missing: bool = True
    debounce_ms: int = 750
    retries: int = 3
    # In-process tombstones: never recreate a registration observed earlier.
    _known: dict[str, str] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.create_missing) is not bool:
            raise ValueError('create_missing must be boolean')
        if type(self.debounce_ms) is not int or not 1 <= self.debounce_ms <= 60_000:
            raise ValueError('debounce_ms must be between 1 and 60000')
        if type(self.retries) is not int or not 0 <= self.retries <= 10:
            raise ValueError('retries must be between 0 and 10')


def load_config(path: Path) -> WatchConfig:
    path = path.expanduser().absolute()
    raw = tomllib.loads(path.read_text(encoding='utf-8'))
    unknown = set(raw) - {'roots', 'projects', 'legacy', 'ledger', 'create_missing', 'debounce_ms', 'retries'}
    if unknown:
        raise ValueError(f'unknown watch configuration keys: {sorted(unknown)}')

    def convert(value: Any) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ValueError('watch paths must be nonempty strings')
        expanded = Path(value).expanduser()
        return Path(os.path.abspath(path.parent / expanded))

    for key in ('roots', 'projects', 'legacy'):
        values = raw.get(key, [])
        if not isinstance(values, list):
            raise TypeError(f'{key} must be an array of paths')
        raw[key] = tuple(convert(v) for v in values)
    if 'ledger' in raw:
        raw['ledger'] = convert(raw['ledger'])
    if not (raw['roots'] or raw['projects'] or raw['legacy']):
        raise ValueError('watch requires roots, projects, or legacy paths')
    return WatchConfig(**raw)


@dataclass
class ReconcileReport:
    results: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _safe(path: Path) -> bool:
    """Do not traverse symlinks or linked worktrees, including ancestors."""
    for part in (path, *path.parents):
        if part.is_symlink():
            return False
        git = part / '.git'
        if git.is_file():
            # Submodules also have a .git file, but are not linked worktrees.
            try:
                if '/worktrees/' in git.read_text(encoding='utf-8').replace('\\', '/'):
                    return False
            except (OSError, UnicodeError):
                return False
    return True


def _walk(root: Path):
    if not root.is_dir() or not _safe(root):
        raise ValueError(f'watch root is missing, symlinked or a linked worktree: {root}')
    def fail(exc):
        raise exc
    for directory, dirs, _ in os.walk(root, followlinks=False, onerror=fail):
        here = Path(directory)
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and _safe(here / d))
        yield here


def _inventory(config: WatchConfig) -> tuple[list[tuple[Path, Project | None]], set[Path]]:
    projects: set[Path] = set()
    legacy: set[Path] = set()
    watched: set[Path] = set()
    for root in config.roots:
        for directory in _walk(root):
            watched.add(directory)
            if (directory / PROJECT_FILE).is_file() or (directory / CANDYBARS_DIR).is_dir():
                projects.add(directory)
            if (directory / 'candybar.toml').is_file():
                legacy.add(directory / 'candybar.toml')
    for root in config.projects:
        if not root.is_dir() or not _safe(root):
            raise ValueError(f'invalid project root: {root}')
        projects.add(root)
        watched.add(root)
        directory = root / CANDYBARS_DIR
        if directory.is_dir() and _safe(directory):
            watched.add(directory)
    for path in config.legacy:
        if path.is_dir():
            path = path / 'candybar.toml'
        if not path.parent.is_dir() or not _safe(path):
            raise ValueError(f'invalid legacy path: {path}')
        watched.add(path.parent)
        if path.exists():
            legacy.add(path)
    files: list[tuple[Path, Project | None]] = []
    identities: dict[str, Path] = {}
    for root in sorted(projects):
        project = project_at(root)
        if project.tag in identities and identities[project.tag] != root:
            raise ValueError(f'duplicate project identity {project.tag}: {identities[project.tag]}, {root}')
        identities[project.tag] = root
        for path in project.files():
            if not _safe(path):
                raise ValueError(f'unsafe project manifest: {path}')
            files.append((path, project))
    for path in sorted(legacy):
        if not _safe(path):
            raise ValueError(f'unsafe legacy manifest: {path}')
        files.append((path, None))
    return files, watched


def _migration_ledger(path: Path, config: WatchConfig) -> Path | None:
    # .../deploy/migration/manifests/<name>/candybar.toml
    if path.name == 'candybar.toml' and path.parent.parent.name == 'manifests' and path.parents[2].name == 'migration' and path.parents[3].name == 'deploy':
        return config.ledger or path.parents[2] / 'ledger.yaml'
    return None


def _ledger_entry(path: Path, name: str):
    ledger = load_ledger(path)
    ids = [e.candybar_id for e in ledger.jobs if e.candybar_id]
    if len(ids) != len(set(ids)):
        raise ValueError(f'{path}: duplicate candybar ids')
    matches = [e for e in ledger.jobs if e.name == name]
    if len(matches) != 1:
        raise ValueError(f'{path}: no unique ledger entry for {name}')
    return matches[0]


def _active_entry(entry) -> bool:
    if entry.disable_pending:
        raise ValueError(f'{entry.name}: migration disable_pending')
    if entry.status not in {'verified', 'cutover'}:
        return False
    if not entry.candybar_id:
        raise ValueError(f'{entry.name}: active migration has no candybar_id')
    return True


def _read(api, job_id: str) -> dict[str, Any]:
    row = api.get_candybar(job_id)
    if row is None:
        raise ValueError(f'registration {job_id} disappeared; refusing to recreate')
    result = {**row, **api.runner(job_id)}
    missing = set(SYNCED_FIELDS) - result.keys()
    if missing:
        raise ValueError(f'{job_id}: incomplete API readback: {sorted(missing)}')
    return result


def _sync_one(config, api, definition, project, ledger_path, report, dry_run):
    manifest = definition.manifest
    name = manifest.name
    result = {'name': name, 'source': str(definition.source), 'status': 'unchanged'}
    report.results.append(result)
    entry = _ledger_entry(ledger_path, name) if ledger_path else None
    if entry is not None and not _active_entry(entry):
        result['status'] = 'migration-inactive'
        return
    rows = [r for r in api.list_candybars() if r['name'] == name]
    if len(rows) > 1:
        raise ValueError(f'{name}: duplicate registry names')
    row = _read(api, rows[0]['id']) if rows else None
    if entry is not None and (row is None or str(row['id']) != entry.candybar_id):
        raise ValueError(f'{name}: migration requires exact ledger candybar_id {entry.candybar_id}')
    if row is not None:
        if row['name'] != name:
            raise ValueError(f'{name}: registration name changed')
        owners = {tag for tag in row.get('tags', []) if tag.startswith('project:')}
        if owners and (project is None or owners != {project.tag}):
            raise ValueError(f'{name}: foreign project ownership {sorted(owners)}')
        if name in config._known and config._known[name] != str(row['id']):
            raise ValueError(f'{name}: registration identity changed; refusing replacement')
        config._known[name] = str(row['id'])
        if row.get('archived'):
            result['status'] = 'archived'
            return
        changes = drift(manifest, row)
        if not changes:
            return
        result['fields'] = sorted(changes)
        result['status'] = 'would-update' if dry_run else 'updated'
        if dry_run:
            return
        if ledger_path:
            fresh = _ledger_entry(ledger_path, name)
            if not _active_entry(fresh) or fresh.candybar_id != str(row['id']):
                raise ValueError(f'{name}: migration changed before update')
        api.update(row['id'], changes)
        job_id = row['id']
    else:
        if name in config._known:
            raise ValueError(f'{name}: known registration disappeared; refusing to recreate')
        if not config.create_missing:
            result['status'] = 'missing'
            return
        result['status'] = 'would-create-disabled' if dry_run else 'created-disabled'
        if dry_run:
            return
        body = manifest.model_dump(mode='json')
        body['enabled'] = False
        job_id = api.register(body)['id']
        config._known[name] = str(job_id)
    readback = _read(api, job_id)
    if readback['name'] != name or drift(manifest, readback):
        raise ValueError(f'{name}: API mutation readback still drifted')
    if row is None and readback.get('enabled') is not False:
        raise ValueError(f'{name}: new registration was not disabled')
    if row is not None and any(readback.get(k) != row.get(k) for k in ('enabled', 'archived')):
        raise ValueError(f'{name}: API changed operator-owned lifecycle state')


@contextmanager
def _reconcile_lock():
    """Serialize editor saves and native workers sharing a registry endpoint.

    The flock is process-wide and released on crash. Lock state lives outside
    repositories; no project enrollment or persistent inventory is required.
    """
    from .migration.client import api_url

    base = Path(os.environ.get('XDG_RUNTIME_DIR', str(Path.home() / '.cache'))) / 'candybar'
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(api_url().encode()).hexdigest()[:24]
    with (base / f'sync-{key}.lock').open('a') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def reconcile(config: WatchConfig, api=None, *, dry_run: bool = False) -> ReconcileReport:
    """Serialize and re-read manifests before reconciling against the registry."""
    with _reconcile_lock():
        return _reconcile(config, api, dry_run=dry_run)


def _reconcile(config: WatchConfig, api=None, *, dry_run: bool = False) -> ReconcileReport:
    """Validate all enrolled sources before any write; never delete or enable."""
    report = ReconcileReport()
    try:
        files, _ = _inventory(config)
        definitions = []
        for path, project in files:
            loaded = load_file(path, project=project)
            report.errors.extend(loaded.errors)
            for definition in loaded.defs:
                manifest = definition.manifest
                if project is None and any(t.startswith('project:') for t in manifest.tags):
                    report.errors.append(f'{path}: legacy manifests must not claim project tags')
                if manifest.entrypoint:
                    script = Path(manifest.entrypoint)
                    manifest = manifest.model_copy(update={'entrypoint': None, 'runner': canonical_runner({
                        'kind': 'exec', 'argv': [str(script)], 'workdir': str(script.parent),
                    })})
                    definition = CandybarDef(manifest, path, definition.table)
                definitions.append((definition, project, _migration_ledger(path, config)))
        counts = Counter(d.name for d, _, _ in definitions)
        for removed in sorted(config._known.keys() - counts.keys()):
            report.results.append({'name': removed, 'source': '', 'status': 'source-removed'})
        report.errors.extend(f'duplicate manifest claim: {name}' for name, count in counts.items() if count > 1)
        if report.errors or not definitions:
            return report  # No registry access when the last manifest was removed.
        with ExitStack() as stack:
            for ledger in sorted({p for _, _, p in definitions if p is not None}):
                stack.enter_context(locked(ledger))
                # Preflight every migration before any other group's write.
                for definition, _, path in definitions:
                    if path == ledger:
                        _active_entry(_ledger_entry(ledger, definition.name))
            api = api if api is not None else CandybarApi()
            # Ownership/identity failures are validation failures, not a partial
            # apply: preflight ALL sources before writing the first definition.
            rows = api.list_candybars()
            by_name = {row['name']: row for row in rows}
            if len(rows) != len(by_name):
                raise ValueError('duplicate registry names')
            for definition, project, ledger in definitions:
                entry = _ledger_entry(ledger, definition.name) if ledger else None
                if entry is not None and not _active_entry(entry):
                    continue
                row = by_name.get(definition.name)
                actual = _read(api, row['id']) if row is not None else None
                owners = {tag for tag in (actual or {}).get('tags', []) if tag.startswith('project:')}
                if owners and (project is None or owners != {project.tag}):
                    report.errors.append(f'{definition.name}: foreign project ownership {sorted(owners)}')
                if entry is not None and (actual is None or str(actual['id']) != entry.candybar_id):
                    report.errors.append(f'{definition.name}: migration requires exact ledger candybar_id {entry.candybar_id}')
                known = config._known.get(definition.name)
                if known is not None and (actual is None or str(actual['id']) != known):
                    report.errors.append(f'{definition.name}: known registration disappeared or changed identity')
            if report.errors:
                return report
            for definition, project, ledger in definitions:
                try:
                    _sync_one(config, api, definition, project, ledger, report, dry_run)
                except (ApiError, OSError, ValueError, KeyError, TypeError) as exc:
                    report.errors.append(f'{definition.source} ({definition.name}): {exc}')
                    if report.results and report.results[-1]['name'] == definition.name:
                        report.results[-1]['status'] = 'error'
    except (ApiError, OSError, ValueError, KeyError, TypeError) as exc:
        report.errors.append(str(exc))
    return report


def run_watch(config: WatchConfig, *, config_path: Path | None = None,
              stop_event: threading.Event | None = None) -> None:
    """Block on native events, retry per burst, and keep watching after errors.

    A configuration edit or native backend error raises RuntimeError, allowing
    Restart=on-failure to reload configuration. Stop events/SIGTERM return cleanly.
    """
    stop = stop_event if stop_event is not None else threading.Event()
    events: queue.Queue = queue.Queue()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
    config_path = config_path.absolute() if config_path else None

    def paths():
        files, watched = _inventory(config)
        if config_path:
            watched.add(config_path.parent)
        if config.ledger:
            watched.add(config.ledger.parent)
        # Also watch inferred ledgers, not just manifests.
        for manifest, _ in files:
            ledger = _migration_ledger(manifest, config)
            if ledger:
                watched.add(ledger.parent)
        if not watched:
            raise ValueError('watch requires at least one enrolled root, project or legacy path')
        return sorted(watched)

    def native(watched):
        # Explicit False ignores WATCHFILES_FORCE_POLLING. Nonrecursive is
        # essential: Python event filters do NOT prune OS watch registration.
        return RustNotify([str(p) for p in watched], False, False, 300, False, False)

    watcher = None
    thread = None
    try:
        watched = paths()
        watcher = native(watched)  # Constructor installs watches synchronously.

        def collect():
            nonlocal watcher, watched
            try:
                while not stop.is_set():
                    changes = watcher.watch(config.debounce_ms, config.debounce_ms, 200, stop)
                    if changes in ('stop', 'signal'):
                        return
                    if changes == 'timeout':
                        continue  # Native wait timeout only, never a reconciliation scan.
                    changed = {Path(p) for _, p in changes}
                    if config_path and config_path in changed:
                        raise RuntimeError('watch configuration changed; restart to reload')
                    relevant = any(
                        p.name not in EXCLUDED and (
                            p == config_path or p == config.ledger
                            or p.name in {PROJECT_FILE, 'candybar.toml', 'ledger.yaml'}
                            or p.name.endswith('.candybar.toml')
                            or p in watched or p.is_dir()
                        ) for p in changed
                    )
                    if not relevant:
                        continue
                    # Subscribe to newly created directories BEFORE releasing
                    # the old inventory; the consumer always rereads current files.
                    new_paths = paths()
                    if new_paths != watched:
                        replacement = native(new_paths)
                        old, watcher, watched = watcher, replacement, new_paths
                        old.close()
                    events.put(None)
            except (OSError, ValueError, RuntimeError) as exc:
                events.put(RuntimeError(f'native watcher failed: {exc}'))

        thread = threading.Thread(target=collect, name='candybar-native-watch', daemon=True)
        thread.start()

        def sync():
            for attempt in range(config.retries + 1):
                if stop.is_set():
                    return
                report = reconcile(config)  # Latest bytes on every bounded retry.
                if report.ok:
                    for result in report.results:
                        log.info('autosync %s %s (%s)', result['status'], result['name'], result['source'])
                    return
                log.error('autosync unsynced (attempt %s/%s): %s', attempt + 1,
                          config.retries + 1, '; '.join(report.errors))
                if attempt < config.retries and stop.wait(min(2 ** attempt, 8)):
                    return
            log.error('autosync UNSYNCED: retry budget exhausted; waiting for a file change')

        sync()
        while not stop.is_set():
            try:
                event = events.get(timeout=0.2)
            except queue.Empty:
                continue
            if isinstance(event, Exception):
                raise event
            # Coalesce queued bursts without throwing away backend errors.
            while True:
                try:
                    event = events.get_nowait()
                except queue.Empty:
                    break
                if isinstance(event, Exception):
                    raise event
            sync()
    finally:
        stop.set()
        if thread:
            thread.join(timeout=5)
        if watcher and (thread is None or not thread.is_alive()):
            watcher.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
