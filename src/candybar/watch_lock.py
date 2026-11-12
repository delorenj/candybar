"""One `candybar watch` per watched tree (CAND-53).

The shell entry hook launches a watcher per shell, so a tree used by several
shells (or orphaned hermes-worker shells) collected duplicate watchers that
all reconciled against the same registry and ledger. The first watcher for a
tree holds a non-blocking flock for its whole life; later ones see it taken and
exit quietly. The kernel drops the lock when the holder dies, however it dies.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _resolved(paths) -> list[str]:
    return sorted(os.path.realpath(p) for p in (paths or ()))


def watch_key(settings) -> str:
    """Stable identity of the watched tree: resolved roots, projects, legacy paths and ledger."""
    parts = [
        *('root:' + p for p in _resolved(getattr(settings, 'roots', ()))),
        *('project:' + p for p in _resolved(getattr(settings, 'projects', ()))),
        *('legacy:' + p for p in _resolved(getattr(settings, 'legacy', ()))),
    ]
    ledger = getattr(settings, 'ledger', None)
    if ledger:
        parts.append('ledger:' + os.path.realpath(ledger))
    if not parts:
        parts.append(repr(settings))
    return hashlib.sha256('\n'.join(parts).encode()).hexdigest()[:24]


def _lock_dir() -> Path:
    return Path(os.environ.get('XDG_RUNTIME_DIR', str(Path.home() / '.cache'))) / 'candybar'


@contextmanager
def watch_singleton(settings) -> Iterator[bool]:
    """Yield True when this process is the tree's only watcher, False when one already runs."""
    base = _lock_dir()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (base / f'watch-{watch_key(settings)}.lock').open('a') as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
