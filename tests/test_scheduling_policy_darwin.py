"""Darwin live-policy collector (W5-1): launchd is the native scheduler on macOS.

Fixtures under tests/fixtures/launchd use synthetic labels and the exact text
shape of `launchctl print` / `print-disabled` (captured from a real macOS 15
host; the services rows are `PID<ws>STATUS<ws>LABEL`). The collector is driven
through an injected `run`, so these tests execute on Linux and never call a
real launchctl. Linux collector behaviour is pinned by test_scheduling_policy.py.
"""
import hashlib
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
# Byte-for-byte copies of two plists found on a real Mac (Citrix-style keystone updater
# stubs in ~/Library/LaunchAgents): both are an empty `<dict/>`, so no Label. The first
# W5-1 collector raised on them and made the whole launchd inventory unknown.
REAL_EMPTY = FIXTURES / "RealEmptyPlists"
REAL_EMPTY_SHA256 = "97704a8960b4facceef54397a08fb5d0a456247c3627359215aa2a27df22656c"
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
        ("print", "system"): completed(text("print-system.txt")),
        ("print-disabled", "gui/501"): completed(text("print-disabled-gui.txt")),
        ("print-disabled", "user/501"): completed(text("print-disabled-user.txt")),
        ("print-disabled", "system"): completed(text("print-disabled-system.txt")),
        ("print", "system/test.system-daemon-loop"): completed(text("print-system-service-interval.txt")),
        ("print", "system/test.system-daemon-quiet"): completed(text("print-service-quiet.txt")),
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
        key = (argv[1], argv[2])
        if key not in table and argv[1] == "print":
            return completed(text("print-service-quiet.txt"))  # a loaded job with a plist: its live definition has no clock
        return table[key]

    return run


def empty_launchctl(argv):
    """A host with nothing loaded and nothing disabled: only the plists on disk matter."""
    assert argv[0] == "launchctl" and argv[1] in {"print", "print-disabled"}
    return completed("\tservices = {\n\t}\n" if argv[1] == "print" else "disabled services = {\n}\n")


def launchctl_with(*, gui_labels=(), disabled=(), enabled=(), probes=None, calls=None):
    """A host with exactly these loaded gui jobs, overrides and `launchctl print` probe answers."""
    services = "".join(f"\t\t0\t0\t{label}\n" for label in gui_labels)
    overrides = "".join(f'\t\t"{label}" => disabled\n' for label in disabled)
    overrides += "".join(f'\t\t"{label}" => enabled\n' for label in enabled)

    def run(argv):
        if calls is not None:
            calls.append(argv)
        verb, target = argv[1], argv[2]
        if verb == "print" and target == "gui/501":
            return completed(f"gui/501 = {{\n\tservices = {{\n{services}\t}}\n}}\n")
        if verb == "print" and target in {"user/501", "system"}:
            return completed("\tservices = {\n\t}\n")
        if verb == "print-disabled":
            return completed(f"\tdisabled services = {{\n{overrides}\t}}\n" if target == "gui/501" else "disabled services = {\n}\n")
        return (probes or {})[target]

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


@pytest.mark.parametrize("body", [b"<?xml version='1.0'?><plist><dict><key>Label</key><string>x", b"\x00\x01 not a plist", b""])
def test_unparseable_plist_is_a_per_file_unreadable_entry_not_an_aborted_scan(tmp_path, body):
    (tmp_path / "good.plist").write_bytes(plistlib.dumps({"Label": "good", "StartInterval": 5}))
    (tmp_path / "x.plist").write_bytes(body)
    rows, files = policy.scan_launchd_plists([tmp_path])
    assert rows["good"]["clocks"] == ["StartInterval"]  # the scan kept going
    assert [(f["file"].endswith("x.plist"), f["unreadable"]) for f in files] == [(True, True)]


def test_unreadable_plist_makes_the_audit_unknown_while_every_other_job_is_still_audited(tmp_path):
    (tmp_path / "good.plist").write_bytes(plistlib.dumps({"Label": "good", "StartInterval": 5}))
    (tmp_path / "x.plist").write_bytes(b"\x00 not a plist")
    rows = policy.launchd_jobs([tmp_path], 501, run=empty_launchctl)
    report = policy.audit(darwin_inventory(launchd=rows), {})
    assert report["status"] == "unknown"  # a corrupt plist can never certify green
    assert [v["id"] for v in report["violations"]] == ["launchd:good"]  # ... and never hides a real clock
    assert any(entry.endswith("x.plist: plist unreadable; cannot certify it carries no clock") for entry in report["unknown"])


@pytest.mark.parametrize("data", [{"NoLabel": 1}, {}, {"Label": ""}, {"Label": 7}, ["not", "a", "dict"]])
def test_plist_without_a_usable_label_is_recorded_per_file_not_fatal(tmp_path, data):
    (tmp_path / "x.plist").write_bytes(plistlib.dumps(data))
    rows, files = policy.scan_launchd_plists([tmp_path])
    assert rows == {} and len(files) == 1
    assert files[0]["clocks"] == [] and files[0]["unreadable"] is False


def test_label_less_plist_with_a_clock_key_is_still_flagged_by_its_path(tmp_path):
    # launchd requires a Label, so this should never load, but that is not proven here:
    # a clock key is a finding, keyed on the file, and only an exact-id exception clears it.
    (tmp_path / "ghost.plist").write_bytes(plistlib.dumps({"StartInterval": 60, "ProgramArguments": ["/bin/true"]}))
    (tmp_path / "quiet.plist").write_bytes(plistlib.dumps({"RunAtLoad": True}))
    rows = {r["file"]: r for r in policy.launchd_jobs([tmp_path], 501, run=empty_launchctl)}
    ghost = next(r for f, r in rows.items() if f.endswith("ghost.plist"))
    quiet = next(r for f, r in rows.items() if f.endswith("quiet.plist"))
    assert ghost["clocks"] == ["StartInterval"] and ghost["active"] is True and ghost["id"].startswith("launchd:file:")
    assert quiet["clocks"] == [] and quiet["active"] is False
    report = policy.audit(darwin_inventory(launchd=list(rows.values())), {})
    assert [v["id"] for v in report["violations"]] == [ghost["id"]] and report["unknown"] == []
    assert "/bin/true" not in json.dumps(rows)  # arguments are never read into the inventory


def test_real_empty_mac_plists_are_the_real_bytes():
    for name in ("com.citrolabs.keystone.agent.plist", "com.citrolabs.keystone.xpcservice.plist"):
        assert hashlib.sha256((REAL_EMPTY / name).read_bytes()).hexdigest() == REAL_EMPTY_SHA256
        assert plistlib.loads((REAL_EMPTY / name).read_bytes()) == {}


def test_real_empty_mac_plists_do_not_abort_the_inventory_and_the_real_clocks_surface():
    dirs = [AGENTS, REAL_EMPTY, LIBRARY_AGENTS]
    rows = policy.launchd_jobs(dirs, 501, run=fake_launchctl())
    by_id = {r["id"]: r for r in rows}
    keystone = [r for r in rows if r.get("file", "").endswith(("keystone.agent.plist", "keystone.xpcservice.plist"))]
    assert len(keystone) == 2 and all(r["active"] is False and r["clocks"] == [] and r["unreadable"] is False for r in keystone)
    assert by_id["launchd:test.interval-job"]["clocks"] == ["StartInterval"]  # real findings still present
    report = policy.audit(darwin_inventory(launchd=rows), {})
    assert report["unknown"] == []  # the empty plists are not an unknown inventory
    assert report["status"] == "fail"
    assert "launchd:test.interval-job" in {v["id"] for v in report["violations"]}
    assert not any("keystone" in v["id"] for v in report["violations"])  # an empty dict is not a clock
    # Through the same collector `doctor --live` uses, pointed at a real-shaped directory set.
    assert policy.launchd_plists(dirs).keys() >= {"test.interval-job", "test.calendar-job"}


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


# ---- launchd's own precedence rules, live definitions, the system domain -------------------

def test_enabled_override_beats_a_plist_disabled_key(tmp_path):
    (tmp_path / "a.plist").write_bytes(plistlib.dumps({"Label": "re.enabled", "StartInterval": 60, "Disabled": True}))
    (tmp_path / "b.plist").write_bytes(plistlib.dumps({"Label": "still.off", "StartInterval": 60, "Disabled": True}))
    (tmp_path / "c.plist").write_bytes(plistlib.dumps({"Label": "forced.off", "StartInterval": 60}))
    run = launchctl_with(enabled=["re.enabled"], disabled=["forced.off"])
    rows = {r["label"]: r for r in policy.launchd_jobs([tmp_path], 501, run=run)}
    assert rows["re.enabled"]["active"] is True  # `launchctl enable` wins: launchd loads it at next login and it fires
    assert rows["still.off"]["active"] is False
    assert rows["forced.off"]["active"] is False  # `launchctl disable` wins over a plist with no Disabled key
    assert policy.parse_launchctl_overrides('\t"a" => enabled\n\t"b" => disabled\n\t"c" => false\n\t"d" => true\n') == ({"b", "d"}, {"a", "c"})


def test_system_domain_daemons_are_audited_with_their_own_overrides(tmp_path):
    daemons = tmp_path / "LaunchDaemons"
    daemons.mkdir()
    (daemons / "off.plist").write_bytes(plistlib.dumps({"Label": "test.system-disabled", "StartInterval": 60}))
    (daemons / "on.plist").write_bytes(plistlib.dumps({"Label": "test.system-quiet-plist", "StartInterval": 60}))
    calls = []
    rows = {r["label"]: r for r in policy.launchd_jobs([daemons], 501, run=fake_launchctl(calls))}
    assert rows["test.system-disabled"]["active"] is False  # `launchctl disable system/...` honoured: no permanent false bypass
    assert rows["test.system-quiet-plist"]["active"] is True
    # Loaded in `system` with no plist anywhere: probed in that domain, its live interval is read.
    assert rows["test.system-daemon-loop"] == {"id": "launchd:test.system-daemon-loop", "label": "test.system-daemon-loop",
                                               "clocks": ["StartInterval"], "active": True, "domains": ["system"]}
    assert ["launchctl", "print", "system/test.system-daemon-loop"] in calls
    assert not any(argv[-1].startswith("system/com.apple") for argv in calls)  # OS-owned: the disclosed boundary


def test_the_live_definition_is_probed_even_when_the_plist_on_disk_has_no_clock(tmp_path):
    # An operator "retires" a clock by deleting StartInterval from the plist but never boots the job out:
    # the loaded job keeps firing, and the audit must still see it.
    (tmp_path / "x.plist").write_bytes(plistlib.dumps({"Label": "io.example.edited", "RunAtLoad": True}))
    probes = {"gui/501/io.example.edited": completed(text("print-service-interval.txt"))}
    calls = []
    rows = {r["label"]: r for r in policy.launchd_jobs([tmp_path], 501, run=launchctl_with(gui_labels=["io.example.edited"], probes=probes, calls=calls))}
    assert rows["io.example.edited"]["clocks"] == ["StartInterval"] and rows["io.example.edited"]["active"] is True
    assert policy.audit(darwin_inventory(launchd=list(rows.values())), {})["violations"] == [
        {"id": "launchd:io.example.edited", "message": policy.REMEDY}]


def test_a_failed_service_probe_is_unknown_not_a_silently_dropped_job():
    denied = {"gui/501/vendor.loop": completed("", 1, "Operation not permitted")}
    with pytest.raises(ValueError):
        policy.launchd_jobs([], 501, run=launchctl_with(gui_labels=["vendor.loop"], probes=denied))
    gone = {"gui/501/vendor.loop": completed("", 113, "Could not find service \"vendor.loop\" in domain for user gui: 501")}
    assert policy.launchd_jobs([], 501, run=launchctl_with(gui_labels=["vendor.loop"], probes=gone)) == []  # the benign race
    by_text = {"gui/501/vendor.loop": completed("", 1, "Could not find service")}
    assert policy.launchd_jobs([], 501, run=launchctl_with(gui_labels=["vendor.loop"], probes=by_text)) == []


@pytest.mark.parametrize("date", ["2024-01-01T00:00:00", "2024Z", "not-a-date"])
def test_a_plist_with_a_malformed_date_is_one_unreadable_file_not_an_aborted_inventory(tmp_path, date):
    # plistlib raises AttributeError / TypeError / ValueError depending on the malformed <date>.
    bad = f"<?xml version='1.0'?><plist version='1.0'><dict><key>Label</key><string>x</string><key>d</key><date>{date}</date></dict></plist>"
    (tmp_path / "bad.plist").write_text(bad)
    (tmp_path / "good.plist").write_bytes(plistlib.dumps({"Label": "good", "StartInterval": 5}))
    rows, files = policy.scan_launchd_plists([tmp_path])
    assert rows["good"]["clocks"] == ["StartInterval"] and [f["unreadable"] for f in files] == [True]


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
    assert "launchd" in inventory["scope"] and "system" in inventory["scope"]
    # The system domain IS audited now; the one coverage boundary (OS-owned loaded jobs with no plist) is disclosed.
    assert "the system launchd domain" not in inventory["not_audited"]
    assert policy.LAUNCHD_NOT_PROBED in inventory["not_audited"] and "com.apple." in policy.LAUNCHD_NOT_PROBED
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
