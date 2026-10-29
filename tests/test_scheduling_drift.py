import json
import subprocess
import sys
from pathlib import Path

from candybar.scheduling_drift import transition


def test_transition_only_and_recovery_unknown_hold():
    fail = {"status": "fail", "violations": [{"id": "systemd:user:rogue.timer"}], "unknown": []}
    state, first = transition(fail, {"conditions": []})
    assert len(first) == 1 and "1 new bypass" in first[0]
    assert "rogue" not in first[0]
    assert transition(fail, state)[1] == []
    unknown = {"status": "unknown", "violations": [], "unknown": ["systemd-user: unavailable"]}
    held, messages = transition(unknown, state)
    assert "systemd:user:rogue.timer" in held["conditions"]
    assert not any("RECOVERED" in line for line in messages)
    assert transition(unknown, held)[1] == []
    clean = {"status": "pass", "violations": [], "unknown": []}
    _, recovered = transition(clean, held)
    assert len(recovered) == 1
    assert "1 cleared" in recovered[0]


def test_real_offline_drift_process_silent_repeat(tmp_path):
    root = Path(__file__).parents[1]
    command = [sys.executable, "-m", "candybar.scheduling_drift", "--state-file", str(tmp_path / "state.json"),
               "--report", str(tmp_path / "report.json"), "--policy", str(root / "deploy/scheduling-policy.json"),
               "--inventory", str(root / "tests/fixtures/scheduling-policy/rogue.json")]
    first = subprocess.run(command, text=True, capture_output=True, check=True)
    second = subprocess.run(command, text=True, capture_output=True, check=True)
    assert "1 new bypass" in first.stdout
    assert "rogue.timer" not in first.stdout
    assert second.stdout == ""
    assert json.loads((tmp_path / "report.json").read_text())["status"] == "fail"


def test_multiple_bypass_clocks_get_one_count_only_message():
    report = {"status": "fail", "violations": [
        {"id": "systemd:user:rogue.timer"},
        {"id": "systemd:user:newapi-allowances.timer"}], "unknown": []}
    _, messages = transition(report, {"conditions": []})
    assert len(messages) == 1
    assert "2 new bypass" in messages[0]
    assert "newapi" not in messages[0] and "rogue" not in messages[0]
