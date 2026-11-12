"""CAND-53: one `candybar watch` per watched tree."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from candybar.cli.main import app


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


def test_cli_duplicate_watch_exits_zero_quietly_without_watching(tmp_path, monkeypatch):
    from candybar.watch_lock import watch_singleton

    tree = tmp_path / "tree"
    tree.mkdir()
    ran = []
    module = SimpleNamespace(load_config=None, reconcile=None, run_watch=lambda *a, **k: ran.append(a))
    monkeypatch.setitem(sys.modules, "candybar.autosync", module)
    monkeypatch.setitem(sys.modules, "candybar.context_sync", SimpleNamespace(context_config=lambda p: settings(tree)))
    with watch_singleton(settings(tree)):
        result = CliRunner().invoke(app, ["watch", str(tree), "--if-present"])
    assert result.exit_code == 0, result.output
    assert result.output == "" and not ran
    result = CliRunner().invoke(app, ["watch", str(tree), "--if-present"])  # slot free: watches
    assert result.exit_code == 0 and len(ran) == 1
