"""ABI tests for the single-job adapter; no live profile or notifications."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "deploy/scoped/crank-beat" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "doc,exit_code,status,tokens,rc",
    [
        (
            {"status": "completed", "input_tokens": 11, "output_tokens": 9},
            0,
            "success",
            20,
            0,
        ),
        (
            {"status": "failed", "error": "semantic failure", "input_tokens": 5},
            1,
            "fail",
            5,
            1,
        ),
        ({"status": "unknown"}, 0, "fail", 0, 1),
        ({"status": "completed"}, 7, "success", 0, 7),
        (
            {"status": "completed", "input_tokens": True, "output_tokens": -4},
            0,
            "success",
            0,
            0,
        ),
    ],
)
def test_canonical_result_parser_and_exit_primacy(doc, exit_code, status, tokens, rc):
    result, actual = load("launch").resolve_cli(
        "noise\ncron-run-result: " + json.dumps(doc), exit_code
    )
    assert result["status"] == status
    assert result["metrics"]["token_spend"] == tokens
    assert actual == rc


@pytest.mark.parametrize("stdout", ["", "cron-run-result: nope", "cron-run-result: []"])
def test_missing_terminal_fails_closed(stdout):
    result, rc = load("launch").resolve_cli(stdout, 0)
    assert result["status"] == "fail" and rc == 1


def test_no_agent_is_not_progress():
    m = load("launch")
    assert m.classify([]) == "no_agent_execution_not_attendance"
    assert m.classify([{"event": "economics_refusal"}]) == "refused_before_inference"
    assert (
        m.classify([{"event": "agent_constructed"}])
        == "agent_executed_check_terminal_receipt"
    )


def test_wrong_job_rejected_before_spawn(monkeypatch):
    monkeypatch.setenv("CANDYBAR_ID", "different-job")
    with pytest.raises(RuntimeError, match="scoped"):
        load("launch").parent()


def test_provenance_rejects_wrong_source_and_hash(tmp_path):
    m = load("launch")
    module = SimpleNamespace(__file__=str(tmp_path / "module.py"))
    Path(module.__file__).write_text("reviewed")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        m.verify_modules(tmp_path, {"cron.monitor": module}, {"module.py": "wrong"})
    with pytest.raises(ValueError):
        m.verify_modules(tmp_path / "different", {"cron.monitor": module}, {})


def test_atomic_receipt_and_snapshot_readback(tmp_path, monkeypatch):
    m = load("launch")
    monkeypatch.setattr(m, "HOME", tmp_path)
    output = tmp_path / "cron/output" / m.JOB_ID
    output.mkdir(parents=True)
    (output / "monitor_last_output.txt").write_text("A")
    pending = {"last_output_hash": "A-hash", "pending_output_hash": "B-hash"}
    jobs = {
        "jobs": [
            {
                "id": m.JOB_ID,
                "scheduler": "external",
                "no_agent": False,
                "monitor_state": pending,
                "economics": {"max_daily_runs": 96},
            }
        ]
    }
    m.atomic_json(tmp_path / "cron/jobs.json", jobs)
    before = m.snapshot()
    assert before["monitor_state"] == pending
    assert before["object_sha256"] is None
    assert before == m.snapshot()  # observation never acknowledges work itself


def test_manifest_retains_clock_and_no_duplicate_failure_hook():
    m = load("activate")
    import tomllib

    doc = tomllib.loads(
        (ROOT / "deploy/migration/manifests/crank-beat/candybar.toml").read_text()
    )["candybar"]
    assert doc["schedule"] == "*/5 * * * *" and doc["timezone"] == "America/New_York"
    runner = m.desired_runner()
    assert runner["kind"] == "exec" and runner["timeout_s"] == 1860
    assert "on_failure" not in runner and "env" not in runner
    assert str(m.launcher.RUNTIME / "launch.py") in runner["argv"]


@pytest.mark.parametrize("bad_readback", [False, True])
def test_patch_transaction_or_rollback(tmp_path, monkeypatch, bad_readback):
    m = load("activate")
    monkeypatch.setattr(m, "TRANSACTION", tmp_path / "activation.json")
    monkeypatch.setattr(m.launcher, "HOME", tmp_path)
    m.launcher.atomic_json(
        tmp_path / "cron/jobs.json",
        {"jobs": [{"id": m.launcher.JOB_ID, "fire_claim": None}]},
    )
    before = {
        "id": m.launcher.CANDYBAR_ID,
        "name": "crank-beat",
        "schedule": "*/5 * * * *",
        "timezone": "America/New_York",
        "noise": True,
        "tags": [],
        "enabled": True,
    }
    original = {
        "kind": "hermes",
        "job_id": m.launcher.JOB_ID,
        "profile": "33god-pm",
        "timeout_s": 1800,
    }
    state = {"runner": original, "enabled": True}
    calls = []

    def api(method, suffix, body=None):
        calls.append((method, suffix, body))
        if method == "PATCH":
            state["runner"] = body["runner"]
        if method == "POST":
            state["enabled"] = body["enabled"]
        if suffix.endswith("/runner"):
            from candybar.runners import canonical_runner

            current = canonical_runner(state["runner"])
            if bad_readback and current["kind"] == "exec":
                current = {"kind": "bad"}
            return {"runner": current, "entrypoint": None}
        return {**before, "enabled": state["enabled"]}

    monkeypatch.setattr(m, "request", api)
    if bad_readback:
        with pytest.raises(RuntimeError, match="readback"):
            m.cutover()
        assert state["runner"] == original and state["enabled"]
        assert json.loads(m.TRANSACTION.read_text())["status"] == "rolled_back"
    else:
        assert m.cutover()["status"] == "activated"
        assert state["runner"]["kind"] == "exec" and state["enabled"]
    assert calls[2][2] == {"enabled": False}
