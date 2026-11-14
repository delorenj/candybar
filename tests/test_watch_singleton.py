"""CAND-53: one `candybar watch` per watched tree."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


def settings(*roots: Path):
    return SimpleNamespace(roots=tuple(roots), projects=(), legacy=(), ledger=None)


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))


def test_second_watcher_for_the_same_tree_is_refused_and_slot_is_released(tmp_path):
    from candybar.watch_lock import watch_singleton

    tree, other = tmp_path / "tree", tmp_path / "other"
    tree.mkdir(), other.mkdir()
    with watch_singleton(settings(tree)) as first:
        assert first
        with watch_singleton(settings(tree / ".." / "tree")) as dup:  # same resolved tree
            assert not dup
        with watch_singleton(settings(other)) as unrelated:
            assert unrelated
    with watch_singleton(settings(tree)) as again:
        assert again


def _fake_watch_modules(monkeypatch, tree, ran):
    def run_watch(*args, **kwargs):
        ran.append(kwargs)
    module = SimpleNamespace(load_config=None, reconcile=None, run_watch=run_watch)
    monkeypatch.setitem(sys.modules, "candybar.autosync", module)
    monkeypatch.setitem(sys.modules, "candybar.context_sync", SimpleNamespace(context_config=lambda p: settings(tree)))


def _cli_thread(tree, results):
    from candybar.cli.watch import watch

    def run():
        try:
            watch(path=tree, config=None, once=False, dry_run=False, if_present=True, as_json=False)
            results.append("returned")
        except BaseException as exc:  # noqa: BLE001 -- typer.Exit and friends reach the test
            results.append(exc)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_second_shell_stands_by_and_takes_over_when_the_first_watcher_exits(tmp_path, monkeypatch):
    """Review finding: coverage must not follow only the first shell into a tree."""
    from candybar import watch_lock
    from candybar.watch_lock import watch_singleton

    tree = tmp_path / "tree"
    tree.mkdir()
    ran, results = [], []
    _fake_watch_modules(monkeypatch, tree, ran)
    monkeypatch.setattr(watch_lock, "STANDBY_POLL_S", 0.05, raising=False)
    monkeypatch.setattr(watch_lock, "parent_pid", lambda: 4242, raising=False)
    first = watch_singleton(settings(tree))  # shell A's watcher
    assert first.__enter__()
    try:
        thread = _cli_thread(tree, results)  # shell B enters the same tree
        threading.Event().wait(0.4)
        assert thread.is_alive() and not ran and not results, "shell B's watcher exited instead of standing by"
    finally:
        first.__exit__(None, None, None)  # shell A exits: zshexit TERMs its watcher
    thread.join(3)
    assert not thread.is_alive() and results == ["returned"]
    assert ran == [{"config_path": None, "launcher_pid": 4242}]  # B took over, following B's shell
    with watch_singleton(settings(tree)) as again:  # and released the slot when it stopped
        assert again


def test_a_standby_whose_launcher_is_gone_exits_without_watching(tmp_path, monkeypatch):
    from candybar import watch_lock
    from candybar.watch_lock import watch_singleton

    tree = tmp_path / "tree"
    tree.mkdir()
    ran, results = [], []
    _fake_watch_modules(monkeypatch, tree, ran)
    parent = [4242]
    monkeypatch.setattr(watch_lock, "STANDBY_POLL_S", 0.05, raising=False)
    monkeypatch.setattr(watch_lock, "parent_pid", lambda: parent[0], raising=False)
    with watch_singleton(settings(tree)) as first:
        assert first
        thread = _cli_thread(tree, results)
        threading.Event().wait(0.4)
        assert thread.is_alive() and not results, "standby exited before its launcher went away"
        parent[0] = 4333  # shell B was SIGKILLed: reparented, no zshexit TERM
        thread.join(3)
        assert not thread.is_alive() and results == ["returned"] and not ran


def test_without_standby_a_held_slot_is_refused_at_once(tmp_path):
    from candybar.watch_lock import watch_singleton

    tree = tmp_path / "tree"
    tree.mkdir()
    with watch_singleton(settings(tree)) as first:
        assert first
        with watch_singleton(settings(tree)) as dup:
            assert not dup
