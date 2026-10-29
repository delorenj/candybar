"""CAND-50 fixtures cover clock ownership, not job health."""
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from candybar import scheduling_policy as policy
from candybar.cli.main import app


def empty():
    return {"version": 1, "sources": {key: {"status": "ok", "rows": []} for key in policy.REQUIRED}}


def timer(name="rogue.timer", active=True, enabled=True):
    return {"id": f"systemd:user:{name}", "active": active, "enabled": enabled}


def hermes(scheduler="internal", enabled=True, profile="new-pm"):
    return {"id": f"hermes:{profile}:abc", "profile": profile, "job_id": "abc", "scheduler": scheduler,
            "enabled": enabled, "recurring": True}


def test_rogue_timer_and_registered_service_timer_still_fail():
    data = empty()
    data["sources"]["registry"]["rows"] = [{"id": "candy", "enabled": True, "runner": {"kind": "systemd", "unit": "rogue.service"}}]
    assert policy.audit(data, {})["status"] == "pass"  # service isn't a clock
    data["sources"]["systemd-user"]["rows"] = [timer()]
    report = policy.audit(data, {})
    assert report["status"] == "fail"
    assert "not a Candybar; register" in report["violations"][0]["message"]
    data["sources"]["systemd-user"]["rows"] = [timer(active=False, enabled=False)]
    assert policy.audit(data, {})["status"] == "pass"  # retired timer


def test_rolled_back_native_clock_exact_exception_only():
    data = empty()
    data["sources"]["systemd-user"]["rows"] = [timer("newapi-allowances.timer")]
    data["sources"]["registry"]["rows"] = [{"id": "01a1793a-d2a0-7070-b267-4f6d3e41a3b5", "enabled": False,
                                              "runner": {"kind": "systemd", "unit": "newapi-allowances.service"}}]
    exceptions = policy.load_policy(Path(__file__).parents[1] / "deploy/scheduling-policy.json")
    assert policy.audit(data, exceptions)["status"] == "pass"
    assert exceptions["systemd:user:newapi-allowances.timer"]["ledger_status"] == "rolled-back"
    data["sources"]["systemd-user"]["rows"].append(timer("newapi-allowances-copy.timer"))
    assert policy.audit(data, exceptions)["status"] == "fail"


def test_explicit_self_watchdog_exception():
    data = empty()
    data["sources"]["systemd-user"]["rows"] = [timer("candybar-watchdog.timer")]
    exceptions = policy.load_policy(Path(__file__).parents[1] / "deploy/scheduling-policy.json")
    report = policy.audit(data, exceptions)
    assert report["status"] == "pass"
    assert report["approved"][0]["owner"] == "Candybar / infra owners"
    data["sources"]["systemd-user"]["rows"].append(timer("future-guard.timer"))
    assert policy.audit(data, exceptions)["status"] == "fail"  # no future grandfathering


@pytest.mark.parametrize("scheduler,enabled,expected", [("internal", True, "fail"), ("external", True, "pass"), ("internal", False, "pass")])
def test_hermes_internal_external_disabled(scheduler, enabled, expected):
    data = empty()
    data["sources"]["registry"]["rows"] = [{"id": "candy", "enabled": True, "runner": {"kind": "hermes", "profile": "new-pm", "job_id": "abc"}}]
    data["sources"]["hermes"]["rows"] = [hermes(scheduler, enabled)]
    assert policy.audit(data, {})["status"] == expected


def test_orphan_external_unknown():
    data = empty()
    data["sources"]["hermes"]["rows"] = [hermes("external")]
    assert policy.audit(data, {})["status"] == "unknown"


@pytest.mark.parametrize("mutate", [
    lambda d: d["sources"].pop("registry"),
    lambda d: d["sources"].update({"registry": {"status": "unknown"}}),
    lambda d: d["sources"]["systemd-user"].update(rows=[{"id": "broken"}]),
    lambda d: d["sources"]["hermes"].update(rows=[{"id": "broken"}]),
    lambda d: d.update(version=99),
    lambda d: d.update(sources=[]),
])
def test_malformed_or_unreachable_never_green(mutate):
    data = empty()
    mutate(data)
    assert policy.audit(data, {})["status"] == "unknown"


def test_new_profiles_discovered_every_time(tmp_path):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    assert policy.hermes_jobs(tmp_path) == []
    path = profiles / "brand-new-pm/cron/jobs.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"jobs": [{"id": "abc", "enabled": True, "schedule": {"kind": "interval", "minutes": 15}, "prompt": "secret-is-not-retained"}]}))
    rows = policy.hermes_jobs(tmp_path)
    assert rows[0]["id"] == "hermes:brand-new-pm:abc"
    assert "secret" not in json.dumps(rows)
    path.write_text('{"jobs":null}')
    with pytest.raises(TypeError):
        policy.hermes_jobs(tmp_path)


def test_crontab_active_and_no_command_leak():
    rows = policy.cron_rows('# skip\nSHELL=/bin/bash\n*/5 * * * * curl -H SECRET\n', "user")
    assert len(rows) == 1
    assert "SECRET" not in json.dumps(rows)
    data = empty()
    data["sources"]["crontab"]["rows"] = rows
    assert policy.audit(data, {})["status"] == "fail"


def test_repo_rogue_service_and_deleted_timer(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "allowed.service").write_text('[Service]\nExecStart=/bin/true\n')
    assert policy.repo_lint(tmp_path, {})["status"] == "pass"
    (tmp_path / "rogue.timer").write_text('[Timer]\nOnCalendar=daily\n')
    assert policy.repo_lint(tmp_path, {})["status"] == "fail"
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    (tmp_path / "rogue.timer").unlink()
    assert policy.repo_lint(tmp_path, {})["status"] == "pass"


def test_policy_rejects_blank_owner_or_wildcard(tmp_path):
    path = tmp_path / "policy.json"
    for entry in [{"id": "foo", "owner": "", "reason": "reason"}, {"id": "systemd:*", "owner": "ops", "reason": "reason"}]:
        path.write_text(json.dumps({"version": 1, "exceptions": [entry]}))
        with pytest.raises(ValueError):
            policy.load_policy(path)


def test_n8n_optional_active_schedule_trigger(tmp_path):
    path = tmp_path / "workflows.json"
    workflow = {"id": "wf", "active": True, "nodes": [{"id": "clock", "type": "n8n-nodes-base.scheduleTrigger"}]}
    path.write_text(json.dumps([workflow]))
    data = empty()
    data["sources"]["n8n"] = {"status": "ok", "rows": policy.n8n_rows(path)}
    assert policy.audit(data, {})["status"] == "fail"
    workflow["active"] = False
    path.write_text(json.dumps([workflow]))
    data["sources"]["n8n"]["rows"] = policy.n8n_rows(path)
    assert policy.audit(data, {})["status"] == "pass"


def test_registry_sanitizes_runner(monkeypatch):
    def request(base, route):
        if route == "/candybars":
            return [{"id": "cb", "name": "job", "enabled": True}]
        return {"runner": {"kind": "exec", "argv": ["SECRET"], "env": {"TOKEN": "SECRET"}}, "entrypoint": None}
    monkeypatch.setattr(policy, "request", request)
    assert "SECRET" not in json.dumps(policy.registry("http://localhost"))


def test_collect_unreachable_is_unknown(monkeypatch, tmp_path):
    def fail(*args):
        raise OSError("unreachable")
    monkeypatch.setattr(policy, "registry", fail)
    monkeypatch.setattr(policy, "timers", lambda *a: [])
    monkeypatch.setattr(policy, "crontabs", list)
    (tmp_path / "profiles").mkdir()
    assert policy.audit(policy.collect("http://unreachable", tmp_path), {})["status"] == "unknown"


def test_cli_fixture_gates(tmp_path):
    path = tmp_path / "inventory.json"
    data = empty()
    path.write_text(json.dumps(data))
    runner = CliRunner()
    assert runner.invoke(app, ["doctor", "--inventory", str(path), "--json"]).exit_code == 0
    data["sources"]["systemd-user"]["rows"] = [timer()]
    path.write_text(json.dumps(data))
    result = runner.invoke(app, ["doctor", "--inventory", str(path)])
    assert result.exit_code == 1 and "not a Candybar; register" in result.output
    path.write_text("broken")
    assert runner.invoke(app, ["doctor", "--inventory", str(path), "--json"]).exit_code == 2


def test_systemd_enabled_but_inactive_and_active_transient(monkeypatch):
    inventories = [[{"unit": "transient.timer", "active": "active"}], [{"unit_file": "enabled.timer", "state": "enabled"}]]
    monkeypatch.setattr(policy, "json_command", lambda *a: inventories.pop(0))
    data = empty()
    data["sources"]["systemd-user"]["rows"] = policy.timers("user")
    assert len(policy.audit(data, {})["violations"]) == 2
