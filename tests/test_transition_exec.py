import json
import sys
from pathlib import Path

import pytest

from candybar.transition_exec import bounded, execute


def detector(tmp_path: Path, text="alert", code=0):
    script = tmp_path / "detector.py"
    script.write_text(f'''import argparse,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--state-file');a=p.parse_args()
s=Path(a.state_file)
old=json.loads(s.read_text()) if s.exists() else {{}}
if old != {{'conditions':['test:one']}}:
 print({text!r})
s.write_text(json.dumps({{'conditions':['test:one']}}))
raise SystemExit({code})
''')
    return [sys.executable, str(script)]


def test_transition_then_silent(tmp_path):
    sent = []
    state = tmp_path / "watchdog.json"
    command = detector(tmp_path)
    sender = lambda *args: sent.append(args)
    first = execute(command, state, "33god-pm", "telegram:7564050286", sender=sender)
    second = execute(command, state, "33god-pm", "telegram:7564050286", sender=sender)
    assert first["delivery"] == "confirmed"
    assert second["delivery"] == "silent"
    assert len(sent) == 1
    assert sent[0][:3] == ("33god-pm", "telegram:7564050286", "alert")


def test_delivery_failure_keeps_old_state_and_retries(tmp_path):
    state = tmp_path / "watchdog.json"
    state.write_text('{"conditions": []}')
    command = detector(tmp_path)
    def fail(*args):
        raise RuntimeError("notification was not confirmed")
    with pytest.raises(RuntimeError, match="not confirmed"):
        execute(command, state, "profile", "telegram:target", sender=fail)
    assert state.read_text() == '{"conditions": []}'
    sent = []
    execute(command, state, "profile", "telegram:target", sender=lambda *a: sent.append(a))
    assert len(sent) == 1
    assert json.loads(state.read_text())["conditions"] == ["test:one"]


def test_execution_failure_one_notice_recovery_and_no_state_loss(tmp_path):
    state = tmp_path / "watchdog.json"
    state.write_text('{"conditions": []}')
    command = detector(tmp_path, code=2)
    sent = []
    sender = lambda *args: sent.append(args)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="state preserved"):
            execute(command, state, "profile", "telegram", sender=sender)
    assert len(sent) == 1
    assert json.loads(state.read_text()) == {"conditions": []}
    execute(detector(tmp_path), state, "profile", "telegram", sender=sender)
    assert len(sent) == 2
    assert "RECOVERED" in sent[-1][2]


def test_bounded_timeout_and_output_limit():
    with pytest.raises(RuntimeError, match="timed out"):
        bounded([sys.executable, "-c", "import time;time.sleep(5)"], .02)
    with pytest.raises(RuntimeError, match="output limit"):
        bounded([sys.executable, "-c", "print('x'*70000)"], 5)


def test_no_false_delivery_receipts(monkeypatch):
    from candybar import transition_exec
    for output in ('{}', '{"success":true,"skipped":true}', 'not json'):
        monkeypatch.setattr(transition_exec, "bounded", lambda *a, value=output: (0, value))
        with pytest.raises(RuntimeError, match="not confirmed"):
            transition_exec.notify("profile", "telegram", "body", 1)
