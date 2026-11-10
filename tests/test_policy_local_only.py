"""Canonical route regressions; no live API or sender is used."""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from candybar.autosync import WatchConfig, reconcile
from candybar.cli import control
from candybar.cli.main import app
from candybar.registry import load_manifest
from test_autosync import FakeApi

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "deploy/policy/candybar.toml"


def assert_local(runner):
    argv = runner["argv"]
    assert "--local-only" in argv and "candybar.scheduling_drift" in argv
    assert "--profile" not in argv and "--target" not in argv
    assert not any("telegram:" in arg for arg in argv)
    assert not runner.get("on_failure")


def test_canonical_register_then_autosync_repairs_old_route(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    file = tmp_path / "candybar.toml"
    file.write_bytes(MANIFEST.read_bytes())
    api = FakeApi()
    register = api.register
    def registered(definition):
        return {**register(definition), "name": definition["name"]}
    monkeypatch.setattr(api, "register", registered)
    monkeypatch.setattr(control, "_api", lambda: api)
    result = CliRunner().invoke(app, ["register", str(file)])
    assert result.exit_code == 0, result.output
    row = next(iter(api.rows.values()))
    assert_local(row["runner"])
    # Reconciliation of a saved canonical manifest removes the old route and
    # never resumes a job paused by containment/operator choice.
    row["enabled"] = False
    row["runner"] = copy.deepcopy(row["runner"])
    row["runner"]["argv"].remove("--local-only")
    row["runner"]["argv"][3:3] = ["--profile", "33god-pm", "--target", "telegram:old"]
    config = WatchConfig((), legacy=(file,))
    report = reconcile(config, api)
    assert report.ok, report.errors
    assert_local(row["runner"])
    assert row["enabled"] is False
    writes = len(api.writes)
    assert reconcile(config, api).ok
    assert len(api.writes) == writes


def test_real_local_wrapper_pending_failure_active_repeat_recovery(tmp_path):
    # A tripwire executable proves the real wrapper never starts hermes send.
    home = tmp_path / "home"
    sender = home / ".local/bin/hermes"
    sender.parent.mkdir(parents=True)
    attempts = home / "sender-called"
    sender.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(attempts)!r}).touch()\nraise SystemExit(99)\n")
    sender.chmod(0o755)
    inventory = tmp_path / "inventory.json"
    state = tmp_path / "transitions.json"
    report = tmp_path / "report.json"
    pending = Path(str(state) + ".pending.json")
    state.write_text('{"conditions":[]}')
    pending.write_text(json.dumps({"version": 1, "profile": "old", "target": "telegram:old",
                                   "message": "old alert", "candidate": json.dumps({"conditions": []})}))
    pending_bytes = pending.read_bytes()
    env = {**os.environ, "HOME": str(home), "TMPDIR": str(tmp_path),
           "PYTHONPATH": str(ROOT / "src")}
    for key in ("CANDYBAR_RESULT_FILE", "CANDYBAR_INVOCATION_ID", "CANDYBAR_TRIGGER"):
        env.pop(key, None)
    command = [sys.executable, "-m", "candybar.transition_exec", "--local-only",
               "--state-file", str(state), "--", sys.executable,
               "-m", "candybar.scheduling_drift", "--policy",
               str(ROOT / "deploy/scheduling-policy.json"), "--inventory", str(inventory),
               "--report", str(report)]
    def run():
        return subprocess.run(command, text=True, capture_output=True, env=env, check=False)
    original = state.read_bytes()
    blocked = run()
    assert blocked.returncode == 1 and json.loads(blocked.stdout)["delivery"] == "unknown"
    assert pending.read_bytes() == pending_bytes and state.read_bytes() == original
    # Test-local administrative quarantine, never mutate the host outbox.
    pending.rename(pending.with_suffix(".quarantined"))
    # Exercise an actual subprocess failure and recovery through the local route.
    bad = command[:command.index("--") + 1] + [sys.executable, "-c", "raise SystemExit(2)"]
    failed = subprocess.run(bad, text=True, capture_output=True, env=env, check=False)
    assert failed.returncode == 1 and state.read_bytes() == original
    assert json.loads(Path(str(state) + ".local-report.json").read_text())["error"]
    inventory.write_bytes((ROOT / "tests/fixtures/scheduling-policy/rogue.json").read_bytes())
    assert run().returncode == 0
    active = json.loads(Path(str(state) + ".local-report.json").read_text())
    assert active["conditions"] == ["systemd:user:rogue.timer"] and active["error"] is None
    assert "1 new bypass" in active["output"]
    assert run().returncode == 0
    assert json.loads(Path(str(state) + ".local-report.json").read_text())["output"] == ""
    inventory.write_bytes((ROOT / "tests/fixtures/scheduling-policy/clean.json").read_bytes())
    assert run().returncode == 0
    recovered = json.loads(Path(str(state) + ".local-report.json").read_text())
    assert recovered["conditions"] == [] and "1 cleared" in recovered["output"]
    assert json.loads(report.read_text())["status"] == "pass"
    assert not attempts.exists() and not pending.exists()
    assert len(Path(str(state) + ".local-events.jsonl").read_text().splitlines()) == 5


def test_manifest_load_is_local():
    assert_local(load_manifest(MANIFEST).runner)
