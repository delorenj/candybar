"""CAND-50 F1/F2 correctness regressions; all delivery uses isolated fake commands."""
import copy
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from candybar import scheduling_policy as policy
from candybar import transition_exec as wrap
from candybar.migration import ops
from candybar.migration.client import ApiError
from candybar.migration.ledger import Ledger, load_ledger, locked, save_ledger
from candybar.migration.native import ShellResult
from candybar.scheduling_drift import transition

ROOT = Path(__file__).parents[1]
POLICY = ROOT / "deploy/scheduling-policy.json"
TIMER = "systemd:user:newapi-allowances.timer"
OWNER = "01a1793a-d2a0-7070-b267-4f6d3e41a3b5"
LEDGER_ID = "systemd:newapi-allowances"


def detector(tmp_path):
    control = tmp_path / "observed.json"
    control.write_text("[]")
    script = tmp_path / "detector.py"
    script.write_text('''import argparse,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--state-file');a=p.parse_args()
s=Path(a.state_file)
before=json.loads(s.read_text())['conditions'] if s.exists() else []
current=json.loads(Path(__CONTROL__).read_text())
if current != before: print('transition:',before,'->',current)
s.write_text(json.dumps({'conditions':current}))
'''.replace("__CONTROL__", repr(str(control))))
    return [sys.executable, str(script)], control


def outbox(state):
    return state.with_name(state.name + ".pending.json")


def reject(*args):
    raise RuntimeError("fixture transport rejection")


def test_transient_retry_precedes_recovery_and_commits_candidate(tmp_path):
    cmd, control = detector(tmp_path)
    state = tmp_path / "state.json"
    state.write_text('{"conditions":[]}')
    control.write_text('["transient"]')
    with pytest.raises(RuntimeError):
        wrap.execute(cmd, state, "fixture", "fixture", sender=reject)
    pending_bytes = outbox(state).read_bytes()
    pending = json.loads(pending_bytes)
    assert json.loads(pending["candidate"]) == {"conditions": ["transient"]}
    assert json.loads(state.read_text()) == {"conditions": []}
    # Repeated failure must not run the now-healthy detector or replace old work.
    control.write_text("[]")
    for _ in range(2):
        with pytest.raises(RuntimeError):
            wrap.execute(cmd, state, "fixture", "fixture", sender=reject)
        assert outbox(state).read_bytes() == pending_bytes
    sent = []
    def sender(profile, target, body, timeout):
        sent.append((body, json.loads(state.read_text())))
    result = wrap.execute(cmd, state, "fixture", "fixture", sender=sender)
    assert result["delivery"] == "confirmed"
    assert sent[0] == (pending["message"], {"conditions": []})
    assert sent[1][1] == {"conditions": ["transient"]}
    assert "['transient'] -> []" in sent[1][0]
    assert not outbox(state).exists()
    assert json.loads(state.read_text()) == {"conditions": []}
    assert wrap.execute(cmd, state, "fixture", "fixture", sender=sender)["delivery"] == "silent"
    assert len(sent) == 2


@pytest.mark.parametrize("stage", ["during_send", "before_commit", "after_commit"])
def test_interruption_preserves_at_least_once_retry(tmp_path, monkeypatch, stage):
    cmd, control = detector(tmp_path)
    control.write_text('["transient"]')
    state = tmp_path / "state.json"
    sent = []
    def interrupted(*args):
        # Even a hard termination during transport has a durable payload/state.
        assert outbox(state).exists()
        sent.append(args[2])
        raise KeyboardInterrupt
    original = wrap.atomic
    def crash_commit(path, content):
        if path == state:
            if stage == "after_commit":
                original(path, content)
            raise KeyboardInterrupt
        original(path, content)
    if stage != "during_send":
        monkeypatch.setattr(wrap, "atomic", crash_commit)
    with pytest.raises(KeyboardInterrupt):
        wrap.execute(cmd, state, "fixture", "fixture", sender=interrupted if stage == "during_send" else lambda *a: sent.append(a[2]))
    pending = json.loads(outbox(state).read_text())
    monkeypatch.setattr(wrap, "atomic", original)
    control.write_text("[]")
    wrap.execute(cmd, state, "fixture", "fixture", sender=lambda *a: sent.append(a[2]))
    # Remote acceptance cannot be transactional with local state: duplicates are honest.
    assert sent[:2] == [pending["message"], pending["message"]]
    assert len(sent) == 3 and "['transient'] -> []" in sent[-1]
    assert not outbox(state).exists()


@pytest.mark.parametrize("content", ["broken", "[]", "{}", '{"version":99}',
                                     '{"version":1,"profile":"fixture","target":"fixture","message":"alert","candidate":"[]"}'])
def test_malformed_pending_unknown_never_dropped_or_overwritten(tmp_path, content):
    cmd, _ = detector(tmp_path)
    state = tmp_path / "state.json"
    outbox(state).write_text(content)
    with pytest.raises(wrap.PendingError, match="unknown.*preserved"):
        wrap.execute(cmd, state, "fixture", "fixture", sender=lambda *a: pytest.fail("must not send"))
    assert outbox(state).read_text() == content and not state.exists()


def test_unreadable_or_dangling_pending_never_treated_as_absent(tmp_path, monkeypatch):
    cmd, _ = detector(tmp_path)
    state = tmp_path / "state.json"
    path = outbox(state)
    path.symlink_to(tmp_path / "missing.json")
    with pytest.raises(wrap.PendingError):
        wrap.execute(cmd, state, "fixture", "fixture", sender=lambda *a: pytest.fail("must not send"))
    assert path.is_symlink() and not state.exists()
    original = Path.lstat
    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError("fixture access denied")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(wrap.PendingError, match="unknown.*inspect"):
        wrap.execute(cmd, state, "fixture", "fixture", sender=lambda *a: pytest.fail("must not send"))
    assert not state.exists()


def test_pending_route_change_unknown_and_restore_retries(tmp_path):
    cmd, control = detector(tmp_path)
    control.write_text('["alert"]')
    state = tmp_path / "state.json"
    with pytest.raises(RuntimeError):
        wrap.execute(cmd, state, "fixture", "old", sender=reject)
    before = outbox(state).read_bytes()
    with pytest.raises(wrap.PendingError):
        wrap.execute(cmd, state, "fixture", "new", sender=lambda *a: pytest.fail("wrong route"))
    assert outbox(state).read_bytes() == before
    sent = []
    wrap.execute(cmd, state, "fixture", "old", sender=lambda *a: sent.append(a))
    assert len(sent) == 1 and sent[0][1] == "old"


def test_alias_and_concurrency_share_pending_lock(tmp_path):
    cmd, control = detector(tmp_path)
    control.write_text('["alert"]')
    state = tmp_path / "state.json"
    alias = tmp_path / "alias.json"
    alias.symlink_to(state)
    with pytest.raises(RuntimeError):
        wrap.execute(cmd, alias, "fixture", "fixture", sender=reject)
    assert outbox(state).exists() and alias.is_symlink()
    sent = []
    def one(path):
        return wrap.execute(cmd, path, "fixture", "fixture", sender=lambda *a: sent.append(a[2]))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(one, [state, alias]))
    assert sorted(r["delivery"] for r in results) == ["confirmed", "silent"]
    assert len(sent) == 1 and alias.is_symlink() and not outbox(state).exists()


def test_outbox_fsync_file_and_directory_before_transport(tmp_path, monkeypatch):
    cmd, control = detector(tmp_path)
    control.write_text('["alert"]')
    state = tmp_path / "state.json"
    original = os.fsync
    synced = []
    def fsync(fd):
        synced.append(os.readlink(f"/proc/self/fd/{fd}"))
        original(fd)
    monkeypatch.setattr(wrap.os, "fsync", fsync)
    def sender(*args):
        assert json.loads(outbox(state).read_text())["message"] == args[2]
        assert len(synced) >= 2 and synced[-1] == str(tmp_path)
    wrap.execute(cmd, state, "fixture", "fixture", sender=sender)


def test_tool_failure_pending_retries_before_healthy_recovery(tmp_path):
    cmd, _ = detector(tmp_path)
    script = Path(cmd[1])
    healthy = script.read_text()
    script.write_text(healthy + "\nraise SystemExit(2)\n")
    state = tmp_path / "state.json"
    with pytest.raises(RuntimeError):
        wrap.execute(cmd, state, "fixture", "fixture", sender=reject)
    pending = json.loads(outbox(state).read_text())
    assert pending["candidate"] is None and "TOOL FAILURE" in pending["message"]
    script.write_text(healthy)
    sent = []
    wrap.execute(cmd, state, "fixture", "fixture", sender=lambda *a: sent.append(a[2]))
    assert len(sent) == 2 and "TOOL FAILURE" in sent[0] and "RECOVERED" in sent[1]
    assert not state.with_name(state.name + ".delivery-health").exists()


def test_real_cli_fake_transport_transient_rejection_then_ordered_recovery(tmp_path):
    cmd, control = detector(tmp_path)
    home = tmp_path / "home"
    binary = home / ".local/bin/hermes"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!" + sys.executable + '''
import json,sys
from pathlib import Path
body=sys.stdin.read();h=Path.home()
with open(h/'attempts.jsonl','a') as f: f.write(json.dumps({'body':body,'argv':sys.argv[1:]})+'\\n')
print(json.dumps({'success':not (h/'reject').exists()}))
''')
    binary.chmod(0o755)
    reject_file = home / "reject"
    reject_file.touch()
    state = tmp_path / "state.json"
    env = {**os.environ, "HOME": str(home), "TMPDIR": str(tmp_path)}
    for key in ("CANDYBAR_RESULT_FILE", "CANDYBAR_INVOCATION_ID", "CANDYBAR_TRIGGER"):
        env.pop(key, None)
    command = [sys.executable, "-m", "candybar.transition_exec", "--state-file", str(state), "--profile", "fixture", "--target", "fixture", "--", *cmd]
    control.write_text('["transient"]')
    first = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
    assert first.returncode == 1 and outbox(state).exists() and not state.exists()
    reject_file.unlink()
    control.write_text("[]")
    second = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
    third = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
    assert second.returncode == third.returncode == 0
    assert json.loads(second.stdout)["delivery"] == "confirmed"
    assert json.loads(third.stdout)["delivery"] == "silent"
    attempts = [json.loads(line) for line in (home / "attempts.jsonl").read_text().splitlines()]
    assert len(attempts) == 3
    assert attempts[0] == attempts[1] and "['transient'] -> []" in attempts[2]["body"]
    outbox(state).write_text("broken")
    uncertain = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
    assert uncertain.returncode == 1 and json.loads(uncertain.stdout)["delivery"] == "unknown"
    assert outbox(state).read_text() == "broken"


def inventory(enabled=False):
    data = {"version": 1, "sources": {key: {"status": "ok", "rows": []} for key in policy.REQUIRED}}
    data["sources"]["systemd-user"]["rows"] = [{"id": TIMER, "active": True, "enabled": True}]
    data["sources"]["registry"]["rows"] = [{"id": OWNER, "enabled": enabled, "runner": {"kind": "systemd", "unit": "newapi-allowances.service"}}]
    return data


@pytest.fixture
def canonical(tmp_path, monkeypatch):
    ledger = load_ledger(policy.CANONICAL_LEDGER)
    entry = copy.deepcopy(ledger.entry(LEDGER_ID))
    path = tmp_path / "ledger.yaml"
    save_ledger(path, Ledger(jobs=[entry]))
    monkeypatch.setattr(policy, "CANONICAL_LEDGER", path)
    return path, entry


def test_conditional_rollback_requires_real_disabled_exact_owner(canonical):
    exceptions = policy.load_policy(POLICY)
    assert policy.audit(inventory(), exceptions)["status"] == "pass"
    report = policy.audit(inventory(enabled=True), exceptions)
    assert report["status"] == "fail" and "duplicate" in report["violations"][0]["message"]
    assert not report["approved"]


@pytest.mark.parametrize("change,expected", [
    (lambda e: setattr(e, "disable_pending", True), "unknown"),
    (lambda e: setattr(e, "status", "verified"), "fail"),
    (lambda e: setattr(e, "candybar_id", "wrong-uuid"), "unknown"),
    (lambda e: setattr(e.native, "unit", "newapi-allowances-copy.timer"), "unknown"),
])
def test_fresh_canonical_status_and_identity_not_policy_labels(canonical, change, expected):
    path, entry = canonical
    exceptions = policy.load_policy(POLICY)
    assert policy.audit(inventory(), exceptions)["status"] == "pass"
    change(entry)
    save_ledger(path, Ledger(jobs=[entry]))
    report = policy.audit(inventory(), exceptions)
    assert report["status"] == expected and not report["approved"]


@pytest.mark.parametrize("change", [
    lambda d: d["sources"]["registry"].update(rows=[]),
    lambda d: d["sources"]["registry"].update(status="unknown"),
    lambda d: d["sources"]["registry"]["rows"][0].update(id="same-name-not-owner", name="newapi-allowances"),
    lambda d: d["sources"]["registry"]["rows"][0].update(enabled="false"),
    lambda d: d["sources"]["registry"]["rows"][0].update(runner={"kind": "systemd", "unit": "other.service"}),
    lambda d: d["sources"]["registry"]["rows"].append(d["sources"]["registry"]["rows"][0].copy()),
])
def test_missing_malformed_ambiguous_or_name_only_registry_unknown(canonical, change):
    data = inventory()
    change(data)
    report = policy.audit(data, policy.load_policy(POLICY))
    assert report["status"] == "unknown" and not report["approved"]


def test_canonical_missing_malformed_and_concurrent_migration_unknown(canonical):
    path, _ = canonical
    exceptions = policy.load_policy(POLICY)
    with locked(path):
        report = policy.audit(inventory(), exceptions)
        assert report["status"] == "unknown" and not report["approved"]
    path.write_text("jobs: [broken")
    assert policy.audit(inventory(), exceptions)["status"] == "unknown"
    path.unlink()
    assert policy.audit(inventory(), exceptions)["status"] == "unknown"


def test_source_overrides_and_inventory_ledger_cannot_spoof_canonical(canonical, tmp_path, monkeypatch):
    path, entry = canonical
    spoof = tmp_path / "spoof.yaml"
    save_ledger(spoof, Ledger(jobs=[entry]))
    monkeypatch.setenv("CANDYBAR_LEDGER", str(spoof))
    entry.disable_pending = True
    save_ledger(path, Ledger(jobs=[entry]))
    data = inventory()
    data["ledger"] = {"status": "rolled-back", "disable_pending": False}
    data["sources"]["ledger"] = {"status": "ok", "rows": [{"id": LEDGER_ID, "status": "rolled-back"}]}
    # --policy is an exception declaration, never a source override for evidence.
    override = tmp_path / "policy.json"
    raw = json.loads(POLICY.read_text())
    raw["ledger_path"] = str(spoof)
    override.write_text(json.dumps(raw))
    report = policy.audit(data, policy.load_policy(override))
    assert report["status"] == "unknown" and not report["approved"]


def test_live_registry_is_refreshed_under_canonical_lock(canonical, monkeypatch):
    path, _ = canonical
    observed = []
    def registry(base):
        with pytest.raises(ValueError, match="another.*migrate"), locked(path):
            pytest.fail("migration acquired lock during policy read")
        observed.append(base)
        return inventory(enabled=True)["sources"]["registry"]["rows"]
    monkeypatch.setattr(policy, "registry", registry)
    report = policy.audit(inventory(), policy.load_policy(POLICY), base="http://fixture")
    assert observed == ["http://fixture"] and report["status"] == "fail"
    def unavailable(base):
        raise OSError("fixture API outage")
    monkeypatch.setattr(policy, "registry", unavailable)
    assert policy.audit(inventory(), policy.load_policy(POLICY), base="http://fixture")["status"] == "unknown"


def test_real_doctor_cli_duplicate_fails_missing_owner_unknown(tmp_path):
    fixture = tmp_path / "inventory.json"
    env = {**os.environ, "HOME": str(tmp_path), "TMPDIR": str(tmp_path)}
    for data, code in [(inventory(enabled=True), 1), (inventory(), 0)]:
        fixture.write_text(json.dumps(data))
        result = subprocess.run([str(ROOT / ".venv/bin/candybar"), "doctor", "--inventory", str(fixture), "--policy", str(POLICY), "--json"], capture_output=True, text=True, env=env, check=False)
        assert result.returncode == code, result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert report["checks"]["machine"]["status"] == ("fail" if code else "pass")
    data = inventory()
    data["sources"]["registry"]["rows"] = []
    fixture.write_text(json.dumps(data))
    result = subprocess.run([str(ROOT / ".venv/bin/candybar"), "doctor", "--inventory", str(fixture), "--policy", str(POLICY), "--json"], capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 2 and json.loads(result.stdout)["status"] == "unknown"


def test_pending_rollback_unknown_holds_previous_drift_condition(canonical):
    path, entry = canonical
    entry.disable_pending = True
    save_ledger(path, Ledger(jobs=[entry]))
    report = policy.audit(inventory(), policy.load_policy(POLICY))
    state, messages = transition(report, {"conditions": [TIMER]})
    assert TIMER in state["conditions"] and len(messages) == 1 and "0 cleared" in messages[0]


def test_recovery_failure_retains_separate_pending_then_retries(tmp_path):
    cmd, control = detector(tmp_path)
    control.write_text('["transient"]')
    state = tmp_path / "state.json"
    with pytest.raises(RuntimeError):
        wrap.execute(cmd, state, "fixture", "fixture", sender=reject)
    alert = json.loads(outbox(state).read_text())["message"]
    control.write_text("[]")
    sent = []
    def fail_recovery(*args):
        sent.append(args[2])
        if len(sent) == 2:
            raise RuntimeError("fixture recovery rejection")
    with pytest.raises(RuntimeError):
        wrap.execute(cmd, state, "fixture", "fixture", sender=fail_recovery)
    assert sent[0] == alert and "['transient'] -> []" in sent[1]
    assert json.loads(state.read_text())["conditions"] == ["transient"]
    assert json.loads(outbox(state).read_text())["message"] == sent[1]
    wrap.execute(cmd, state, "fixture", "fixture", sender=lambda *a: sent.append(a[2]))
    assert sent[1] == sent[2] and len(sent) == 3
    assert json.loads(state.read_text())["conditions"] == []


def test_abrupt_process_death_after_remote_acceptance_replays(tmp_path):
    cmd, control = detector(tmp_path)
    control.write_text('["transient"]')
    state = tmp_path / "state.json"
    script = """import os,sys
from pathlib import Path
from candybar.transition_exec import execute
state=Path(sys.argv[1])
def accepted_then_died(*args):
    assert state.with_name(state.name+'.pending.json').exists()
    os._exit(17)
execute(sys.argv[2:],state,'fixture','fixture',sender=accepted_then_died)
"""
    child = subprocess.run([sys.executable, "-c", script, str(state), *cmd], check=False)
    assert child.returncode == 17 and outbox(state).exists() and not state.exists()
    control.write_text("[]")
    sent = []
    wrap.execute(cmd, state, "fixture", "fixture", sender=lambda *a: sent.append(a[2]))
    assert len(sent) == 2 and "[] -> ['transient']" in sent[0] and "['transient'] -> []" in sent[1]


@pytest.mark.parametrize("field,value", [("ledger_id", "wrong"), ("candybar_id", "wrong"),
                                         ("ledger_status", "verified"), ("ledger_id", None)])
def test_policy_override_cannot_launder_protected_clock(tmp_path, field, value):
    raw = json.loads(POLICY.read_text())
    entry = next(e for e in raw["exceptions"] if e["id"] == TIMER)
    if value is None:
        for key in ("ledger_id", "ledger_status", "candybar_id"):
            entry.pop(key)
    else:
        entry[field] = value
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        policy.load_policy(path)
    assert policy.audit(inventory(), {TIMER: entry})["status"] == "unknown"


def test_api_only_rollback_outage_is_not_a_single_clock_guarantee(tmp_path):
    entry = copy.deepcopy(load_ledger(ROOT / "deploy/migration/ledger.yaml").entry("hermes:33god-pm:061c4dbedc1c"))
    ledger = tmp_path / "ledger.yaml"
    save_ledger(ledger, Ledger(jobs=[entry]))
    job = {"id": "061c4dbedc1c", "enabled": False, "paused_at": "fixture", "scheduler": "external"}
    def shell(command):
        if "--scheduler internal" in command:
            job["scheduler"] = "internal"
        elif "cron resume" in command:
            job.update(enabled=True, paused_at=None)
        else:
            raise AssertionError(command)
        return ShellResult(0)
    class Api:
        enabled = True
        def set_enabled(self, *args):
            raise ApiError("fixture API-only outage")
    api = Api()
    host = ops.Host(api=api, shell=shell, read=lambda p: json.dumps({"jobs": [job]}), echo=lambda *a: None)
    code = ops.rollback(host, ledger, entry.id, reason="isolated fixture")
    assert code == ops.EXIT_PENDING and job["enabled"] and job["scheduler"] == "internal" and api.enabled
    assert load_ledger(ledger).entry(entry.id).disable_pending
    data = inventory()
    data["sources"]["hermes"]["rows"] = [{"id": entry.id, "profile": "33god-pm", "job_id": job["id"], "enabled": True, "recurring": True, "scheduler": "internal"}]
    assert policy.audit(data, policy.load_policy(POLICY))["status"] == "fail"
