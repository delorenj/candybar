"""Darwin live-policy collector (W5-1): launchd is the native scheduler on macOS.

Fixtures under tests/fixtures/launchd use synthetic labels and the exact text
shape of `launchctl print` / `print-disabled` (captured from a real macOS 15
host; the services rows are `PID<ws>STATUS<ws>LABEL`). The collector is driven
through an injected `run`, so these tests execute on Linux and never call a
real launchctl. Linux collector behaviour is pinned by test_scheduling_policy.py.
"""
import json
import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from candybar import scheduling_policy as policy
from candybar.cli.main import app

FIXTURES = Path(__file__).parent / "fixtures" / "launchd"
AGENTS = FIXTURES / "LaunchAgents"
LIBRARY_AGENTS = FIXTURES / "LibraryLaunchAgents"
SENTINEL = "SENTINEL-NEVER-IN-INVENTORY"


def text(name: str) -> str:
    return (FIXTURES / name).read_text()


def completed(stdout="", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


def fake_launchctl(calls=None, *, gui=None, user=None):
    """A `run` replacement that serves fixture output and records every argv."""
    table = {
        ("print", "gui/501"): gui if gui is not None else completed(text("print-gui.txt")),
        ("print", "user/501"): user if user is not None else completed(text("print-user.txt")),
        ("print-disabled", "gui/501"): completed(text("print-disabled-gui.txt")),
        ("print-disabled", "user/501"): completed(text("print-disabled-user.txt")),
        ("print", "gui/501/test.submitted-loop"): completed(text("print-service-interval.txt")),
        ("print", "gui/501/test.submitted-quiet"): completed(text("print-service-quiet.txt")),
        ("print", "user/501/test.user-domain-only"): completed(text("print-service-calendar.txt")),
        # Unloaded between the listing and the probe.
        ("print", "gui/501/test.vanished"): completed("", 113, "Could not find service"),
    }

    def run(argv):
        assert argv[0] == "launchctl"
        if calls is not None:
            calls.append(argv)
        return table[(argv[1], argv[2])]

    return run


def jobs(**kwargs):
    return {row["label"]: row for row in policy.launchd_jobs([AGENTS, LIBRARY_AGENTS, FIXTURES / "absent"], 501, **kwargs)}


# ---- plist scan ----------------------------------------------------------------------

def test_plist_scan_flags_exactly_the_clock_keys():
    rows = policy.launchd_plists([AGENTS, LIBRARY_AGENTS])
    assert rows["test.interval-job"]["clocks"] == ["StartInterval"]
    assert rows["test.calendar-job"]["clocks"] == ["StartCalendarInterval"]
    assert rows["test.system-wide-interval"]["clocks"] == ["StartInterval"]
    # RunAtLoad/KeepAlive/ThrottleInterval/WatchPaths are supervision and events, never clocks.
    assert rows["test.keepalive-service"]["clocks"] == []
    assert rows["test.disabled-interval"]["disabled"] is True
    assert rows["test.interval-job"]["disabled"] is False


def test_plist_scan_never_reads_arguments_or_environment_into_rows():
    rows = policy.launchd_plists([AGENTS, LIBRARY_AGENTS])
    assert SENTINEL not in json.dumps(rows)
    assert (AGENTS / "test.interval-job.plist").read_text().count(SENTINEL) == 1  # the fixture does carry it


def test_duplicate_label_across_directories_unions_the_clocks(tmp_path):
    user, library = tmp_path / "u", tmp_path / "l"
    user.mkdir(), library.mkdir()
    (user / "a.plist").write_bytes(plistlib.dumps({"Label": "dup", "RunAtLoad": True}))
    (library / "a.plist").write_bytes(plistlib.dumps({"Label": "dup", "StartInterval": 60, "Disabled": True}))
    row = policy.launchd_plists([user, library])["dup"]
    assert row["clocks"] == ["StartInterval"]
    assert row["disabled"] is False  # enabled in either copy


def test_binary_plists_are_read(tmp_path):
    (tmp_path / "b.plist").write_bytes(plistlib.dumps({"Label": "bin", "StartInterval": 5}, fmt=plistlib.FMT_BINARY))
    assert policy.launchd_plists([tmp_path])["bin"]["clocks"] == ["StartInterval"]


@pytest.mark.parametrize("body", [b"<?xml version='1.0'?><plist><dict><key>Label</key><string>x", b"\x00\x01 not a plist",
                                  plistlib.dumps({"NoLabel": 1}), plistlib.dumps(["not", "a", "dict"])])
def test_malformed_plist_is_an_unreadable_inventory_not_a_skip(tmp_path, body):
    (tmp_path / "x.plist").write_bytes(body)
    with pytest.raises(ValueError):
        policy.launchd_plists([tmp_path])


# ---- launchctl output parsers -----------------------------------------------------------

def test_services_block_parser():
    labels = policy.parse_launchctl_services(text("print-gui.txt"))
    assert {"test.interval-job", "test.keepalive-service", "com.apple.example.agent"} <= labels
    assert not any("disabled" in label or label == "test.override-disabled" for label in labels)


def test_services_parser_fails_closed_on_a_shape_it_does_not_know():
    with pytest.raises(ValueError):
        policy.parse_launchctl_services("gui/501 = {\n\ttype = login\n}\n")  # no services block
    with pytest.raises(ValueError):
        policy.parse_launchctl_services("\tservices = {\n\t\tsomething else entirely\n\t}\n")


def test_disabled_parser_accepts_both_value_spellings():
    assert policy.parse_launchctl_disabled(text("print-disabled-gui.txt")) == {"test.override-disabled", "test.legacy-true"}


def test_service_probe_reads_interval_and_calendar_markers():
    assert policy.launchctl_clocks(text("print-service-interval.txt")) == ["StartInterval"]
    assert policy.launchctl_clocks(text("print-service-calendar.txt")) == ["StartCalendarInterval"]
    assert policy.launchctl_clocks(text("print-service-quiet.txt")) == []


# ---- the collector --------------------------------------------------------------------

def test_launchd_jobs_combines_disk_loaded_and_disabled_state():
    calls = []
    rows = jobs(run=fake_launchctl(calls))
    assert rows["test.interval-job"] == {"id": "launchd:test.interval-job", "label": "test.interval-job",
                                         "clocks": ["StartInterval"], "active": True, "domains": ["gui/501"]}
    assert rows["test.calendar-job"]["active"] is True and rows["test.calendar-job"]["domains"] == []  # loads at next login
    assert rows["test.disabled-interval"]["active"] is False  # Disabled key
    assert rows["test.override-disabled"]["active"] is False  # print-disabled override, not loaded
    assert rows["test.system-wide-interval"]["clocks"] == ["StartInterval"]
    # Loaded with no plist on disk: probed, clock read from launchctl print.
    assert rows["test.submitted-loop"]["clocks"] == ["StartInterval"]
    assert rows["test.submitted-quiet"]["clocks"] == []
    assert rows["test.user-domain-only"]["clocks"] == ["StartCalendarInterval"]
    assert rows["test.user-domain-only"]["domains"] == ["user/501"]
    # OS jobs are never probed; a label that unloaded between listing and probe is dropped.
    assert "com.apple.example.agent" not in rows and "test.vanished" not in rows
    assert not any(argv[-1].startswith(("gui/501/com.apple", "gui/501/application")) for argv in calls)


def test_launchd_collector_only_ever_runs_read_only_launchctl_subcommands():
    calls = []
    jobs(run=fake_launchctl(calls))
    assert {argv[1] for argv in calls} == {"print", "print-disabled"}


def test_no_gui_session_is_not_an_error():
    missing = completed("", 113, "Bad request.\nCould not find domain for user gui/501")
    rows = jobs(run=fake_launchctl(gui=missing))
    assert rows["test.interval-job"]["domains"] == [] and rows["test.interval-job"]["active"] is True


def test_launchctl_failure_other_than_missing_domain_is_unknown():
    with pytest.raises(ValueError):
        jobs(run=fake_launchctl(gui=completed("", 1, "Operation not permitted")))


# ---- audit ----------------------------------------------------------------------------

def darwin_inventory(**rows):
    sources = {key: {"status": "ok", "rows": []} for key in policy.DARWIN_REQUIRED}
    for key, value in rows.items():
        sources[key]["rows"] = value
    return {"version": 1, "platform": "darwin", "sources": sources}


def launchd_row(label="rogue", clocks=("StartInterval",), active=True):
    return {"id": f"launchd:{label}", "label": label, "clocks": list(clocks), "active": active, "domains": []}


def test_active_launchd_clock_is_a_bypass_with_its_exact_id():
    report = policy.audit(darwin_inventory(launchd=[launchd_row("io.example.resync")]), {})
    assert report["status"] == "fail"
    assert report["violations"] == [{"id": "launchd:io.example.resync", "message": policy.REMEDY}]


@pytest.mark.parametrize("row", [launchd_row(clocks=()), launchd_row(active=False), launchd_row("x", ("StartCalendarInterval",), False)])
def test_supervised_event_driven_and_inactive_launchd_jobs_pass(row):
    assert policy.audit(darwin_inventory(launchd=[row]), {})["status"] == "pass"


def test_launchd_clock_can_be_exempted_only_by_exact_id():
    exceptions = {"launchd:io.example.owner-approved": {"id": "launchd:io.example.owner-approved", "owner": "o", "reason": "r"}}
    rows = [launchd_row("io.example.owner-approved")]
    assert policy.audit(darwin_inventory(launchd=rows), exceptions)["status"] == "pass"
    rows.append(launchd_row("io.example.owner-approved-copy"))
    assert policy.audit(darwin_inventory(launchd=rows), exceptions)["status"] == "fail"


def test_darwin_cron_is_still_audited_and_launchd_unavailable_is_unknown():
    data = darwin_inventory(crontab=[{"id": "crontab:user:abc", "active": True}])
    assert policy.audit(data, {})["status"] == "fail"
    data = darwin_inventory()
    data["sources"]["launchd"] = {"status": "unknown", "error": "ValueError"}
    assert policy.audit(data, {})["status"] == "unknown"  # never green over an unreadable launchd
    del data["sources"]["launchd"]
    assert "launchd: missing inventory" in policy.audit(data, {})["unknown"]


def test_darwin_inventory_does_not_accept_systemd_sources_as_coverage():
    data = darwin_inventory()
    data["sources"]["systemd-user"] = {"status": "ok", "rows": []}
    assert policy.audit(data, {})["status"] == "unknown"  # unexpected source for this platform


@pytest.mark.parametrize("bad", [{"id": "launchd:x"}, {"id": "launchd:x", "active": "yes", "clocks": []},
                                 {"id": "launchd:x", "active": True, "clocks": "StartInterval"},
                                 {"id": "launchd:x", "active": True, "clocks": [1]}])
def test_malformed_launchd_rows_are_unknown(bad):
    assert policy.audit(darwin_inventory(launchd=[bad]), {})["status"] == "unknown"


def test_unknown_platform_is_unknown_and_linux_default_is_unchanged():
    data = darwin_inventory()
    data["platform"] = "plan9"
    assert policy.audit(data, {})["status"] == "unknown"
    linux = {"version": 1, "sources": {key: {"status": "ok", "rows": []} for key in policy.REQUIRED}}
    assert policy.audit(linux, {})["status"] == "pass"  # no `platform` key means Linux
    linux["sources"]["systemd-user"]["rows"] = [{"id": "systemd:user:rogue.timer", "active": True, "enabled": True}]
    assert policy.audit(linux, {})["status"] == "fail"


# ---- collect() platform switch and cron on macOS -------------------------------------------

def stub_probes(monkeypatch):
    monkeypatch.setattr(policy, "registry", lambda base: [])
    monkeypatch.setattr(policy, "hermes_jobs", lambda home: [])
    monkeypatch.setattr(policy, "launchd_jobs", lambda: [launchd_row("io.example.resync")])
    monkeypatch.setattr(policy, "crontabs", lambda *args, **kwargs: [])
    monkeypatch.setattr(policy, "timers", lambda scope: (_ for _ in ()).throw(AssertionError("systemctl on darwin")))


def test_collect_on_darwin_gathers_launchd_not_systemd(monkeypatch, tmp_path):
    monkeypatch.setattr(policy.sys, "platform", "darwin")
    stub_probes(monkeypatch)
    inventory = policy.collect("http://127.0.0.1:1", tmp_path)
    assert inventory["platform"] == "darwin"
    assert set(inventory["sources"]) == policy.DARWIN_REQUIRED
    assert "launchd" in inventory["scope"] and "the system launchd domain" in inventory["not_audited"]
    assert policy.audit(inventory, {})["violations"][0]["id"] == "launchd:io.example.resync"


def test_collect_on_linux_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(policy.sys, "platform", "linux")
    monkeypatch.setattr(policy, "registry", lambda base: [])
    monkeypatch.setattr(policy, "hermes_jobs", lambda home: [])
    monkeypatch.setattr(policy, "crontabs", lambda *args, **kwargs: [])
    monkeypatch.setattr(policy, "timers", lambda scope: [])
    monkeypatch.setattr(policy, "launchd_jobs", lambda: (_ for _ in ()).throw(AssertionError("launchd on linux")))
    inventory = policy.collect("http://127.0.0.1:1", tmp_path)
    assert set(inventory["sources"]) == policy.REQUIRED
    assert "platform" not in inventory  # exact Linux inventory shape


def test_crontab_tolerates_missing_cron_d_only_when_asked(monkeypatch, tmp_path):
    monkeypatch.setattr(policy, "command", lambda argv: SimpleNamespace(returncode=1, stdout="", stderr="crontab: no crontab for mac"))
    assert policy.crontabs(tmp_path, tolerate_missing_cron_d=True) == []  # macOS: no /etc/cron.d, no /etc/crontab
    with pytest.raises(OSError):
        policy.crontabs(tmp_path)  # Linux: a missing cron.d stays an unreadable inventory


def test_macos_user_crontab_with_only_comments_is_empty(monkeypatch, tmp_path):
    out = "# DISABLED: 0 9 * * * /usr/bin/true\n"
    monkeypatch.setattr(policy, "command", lambda argv: SimpleNamespace(returncode=0, stdout=out, stderr=""))
    assert policy.crontabs(tmp_path, tolerate_missing_cron_d=True) == []
    monkeypatch.setattr(policy, "command", lambda argv: SimpleNamespace(returncode=0, stdout="0 9 * * * /usr/bin/true\n", stderr=""))
    assert len(policy.crontabs(tmp_path, tolerate_missing_cron_d=True)) == 1


def test_doctor_cli_audits_a_darwin_inventory_offline(tmp_path):
    inventory = darwin_inventory(launchd=[launchd_row("io.example.resync")])
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inventory))
    result = CliRunner().invoke(app, ["doctor", "--inventory", str(path), "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["checks"]["machine"]["violations"][0]["id"] == "launchd:io.example.resync"
    path.write_text(json.dumps(darwin_inventory(launchd=[launchd_row("quiet", clocks=())])))
    assert CliRunner().invoke(app, ["doctor", "--inventory", str(path)]).exit_code == 0


def test_the_module_never_shells_out_to_a_mutating_launchctl_verb():
    source = Path(policy.__file__).read_text()
    for verb in ("bootout", "bootstrap", "kickstart", "unload", "disable", "enable", "remove", "submit", "kill"):
        assert f'"launchctl", "{verb}"' not in source
    assert subprocess  # imported for parity with the Linux suite
