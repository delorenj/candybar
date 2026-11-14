"""One active `candybar watch` per watched tree (CAND-53).

The shell entry hook launches a watcher per shell, so a tree used by several
shells (or orphaned hermes-worker shells) collected duplicate watchers that
all reconciled against the same registry and ledger. The first watcher for a
tree holds a non-blocking flock for its whole life. The kernel drops the lock
when the holder dies, however it dies.

A later watcher for the same tree stands by instead of exiting: it idles
without inotify watches or API calls, and takes the slot over when the holder
exits, so coverage follows every shell still in the tree, not only the first
one. A standby leaves as soon as its launcher is gone, and so does an active
watcher (`autosync.run_watch`), so an orphan never keeps a tree on stale code.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

# How often a standby retries the slot and checks that its launcher is alive.
# A bounded lock-acquisition poll, not a schedule (AD-1 clock guard).
STANDBY_POLL_S = 2.0


def parent_pid() -> int:
    """The current parent pid. It changes only when the launcher dies and we are reparented."""
    return os.getppid()


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
def watch_singleton(settings, *, standby: Callable[[], bool] | None = None) -> Iterator[bool]:
    """Yield True once this process is the tree's only active watcher.

    Without `standby`, yield False at once when another watcher holds the tree.
    With it, wait for the slot while `standby()` stays True (the launcher is
    alive) and yield False only once it turns False. A standby holds no lock;
    SIGTERM's default action ends it cleanly.
    """
    base = _lock_dir()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (base / f'watch-{watch_key(settings)}.lock').open('a') as stream:
        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if standby is None:
                    yield False
                    return
            threading.Event().wait(STANDBY_POLL_S)
            if not standby():
                yield False
                return
        try:
            yield True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
