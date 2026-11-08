"""Separate-process serialization for editor and watcher reconciliation."""

import multiprocessing
import os


def _hold(runtime, entered, release):
    os.environ['XDG_RUNTIME_DIR'] = runtime
    from candybar.autosync import _reconcile_lock
    with _reconcile_lock():
        entered.set()
        release.wait(5)


def test_registry_reconcile_lock_serializes_processes(tmp_path):
    context = multiprocessing.get_context('spawn')
    first, second, release_first, release_second = [context.Event() for _ in range(4)]
    a = context.Process(target=_hold, args=(str(tmp_path), first, release_first))
    b = context.Process(target=_hold, args=(str(tmp_path), second, release_second))
    a.start()
    try:
        assert first.wait(5)
        b.start()
        assert not second.wait(0.2)
        release_first.set()
        assert second.wait(5)
    finally:
        release_first.set()
        release_second.set()
        a.join(5)
        if b.pid is not None:
            b.join(5)
    assert a.exitcode == 0 and b.exitcode == 0
    assert list((tmp_path / 'candybar').glob('sync-*.lock'))
