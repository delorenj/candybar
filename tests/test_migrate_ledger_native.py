"""Story 7.5 (AD-21): the migration ledger file and the native-scheduler probes.

Ledger: round trip, validation, the atomic write and the exclusive lock.
Native: the probes over a fake host, and the DEFAULT commands run for real
through `bash -euo pipefail -c` against a fake `crontab` binary (never the
user's crontab). `CANDYBAR_TEST_SYSTEMD=1` adds the live proof on the real
systemd user manager that the default systemd-timer commands round-trip a
`systemctl link`-ed throwaway timer.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from candybar.migration.ledger import (
    Ledger,
    LedgerEntry,
    LedgerError,
    NativeScheduler,
    dump_ledger,
    load_ledger,
    locked,
    save_ledger,
)
from candybar.migration.native import (
    ShellResult,
    cron_fires,
    default_commands,
    probe,
    run_shell,
    unit_file_of,
)

NOW = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)


def entry_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "systemd:cache-prune-daily.timer",
        "wave": "7.7",
        "candybar": {
            "name": "cache-prune-daily",
            "description": "daily cache prune (migrated)",
            "tags": ["migrated", "infra"],
            "schedule": "23 5 * * *",
            "timezone": "America/New_York",
            "runner": {"kind": "systemd", "unit": "cache-prune-daily.service"},
        },
        "native": {
            "kind": "systemd-timer",
            "schedule": "OnCalendar=*-*-* 05:23:00, RandomizedDelaySec=10m",
            "jitter_s": 600,
            "unit": "cache-prune-daily.timer",
        },
        "deviations": ["RandomizedDelaySec=10m dropped"],
        "source_of_truth": {"repo": "delorenj/infra", "path": "systemd/user/", "commit": "abc1234"},
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Ledger.


def test_round_trip_keeps_every_field(tmp_path: Path) -> None:
    path = tmp_path / "ledger.yaml"
    ledger = Ledger(jobs=[LedgerEntry.model_validate(entry_data())])
    ledger.jobs[0].record(NOW, "plan", "written", "defaults")
    save_ledger(path, ledger)
    text = path.read_text()
    assert text.startswith("# Candybar migration ledger")
    loaded = load_ledger(path)
    assert loaded == ledger
    raw = yaml.safe_load(text)
    entry = raw["jobs"][0]
    # The AD-21 field list, all present in the committed file.
    for key in ("id", "candybar", "native", "deviations", "source_of_truth", "disable",
                "rollback", "candybar_id", "verification", "status"):
        assert key in entry
    assert entry["status"] == "planned" and entry["verification"]["invocation_id"] is None
    assert "line" not in entry["native"]  # kind-irrelevant fields are not written
    assert list(tmp_path.iterdir()) == [path]  # no temp file left behind


def test_empty_ledger_loads(tmp_path: Path) -> None:
    path = tmp_path / "ledger.yaml"
    path.write_text(dump_ledger(Ledger()))
    assert load_ledger(path).jobs == []
    (tmp_path / "blank.yaml").write_text("# only a comment\n")
    assert load_ledger(tmp_path / "blank.yaml").jobs == []


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"candybar": {**entry_data()["candybar"], "enabled": False}}, "set by `candybar migrate`"),
        ({"candybar": {**entry_data()["candybar"], "schedule": "nope"}}, "5-field"),
        ({"candybar": {**entry_data()["candybar"], "timezone": "Mars/Base"}}, "timezone"),
        ({"native": {"kind": "systemd-timer", "schedule": "x", "unit": "x.service"}}, ".timer"),
        ({"native": {"kind": "crontab", "schedule": "x", "line": "# 0 * * * * x",
                     "timezone": "UTC"}}, "uncommented"),
        ({"native": {"kind": "crontab", "schedule": "x", "line": "0 * * * * x"}}, "timezone"),
        ({"native": {"kind": "hermes", "schedule": "x", "profile": "p"}}, "jobs_file"),
        ({"status": "done"}, "status"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_invalid_entries_are_refused(patch: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        LedgerEntry.model_validate(entry_data(**patch))


def test_duplicate_ids_and_names_are_refused(tmp_path: Path) -> None:
    one = entry_data()
    other = entry_data(id="systemd:other.timer")
    with pytest.raises(ValueError, match="duplicate candybar names"):
        Ledger.model_validate({"jobs": [one, other]})
    with pytest.raises(ValueError, match="duplicate inventory ids"):
        Ledger.model_validate({"jobs": [one, one]})
    path = tmp_path / "ledger.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "jobs": [one, one]}))
    with pytest.raises(LedgerError, match="duplicate"):
        load_ledger(path)


def test_crontab_cron_defaults_to_the_line_schedule() -> None:
    native = NativeScheduler.model_validate(
        {"kind": "crontab", "schedule": "user crontab line 22",
         "line": "30 */2 * * * /home/x/sync-vault-to-drive.sh", "timezone": "America/New_York"}
    )
    assert native.cron == "30 */2 * * *"


def test_missing_and_malformed_files(tmp_path: Path) -> None:
    with pytest.raises(LedgerError, match="no migration ledger"):
        load_ledger(tmp_path / "absent.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("jobs: [unclosed\n")
    with pytest.raises(LedgerError, match="invalid YAML"):
        load_ledger(bad)


def test_lock_refuses_a_second_writer(tmp_path: Path) -> None:
    path = tmp_path / "ledger.yaml"
    path.write_text(dump_ledger(Ledger()))
    with locked(path), pytest.raises(LedgerError, match="another `candybar migrate`"), locked(path):
        pass
    with locked(path):  # released
        pass


def test_save_refuses_to_write_an_unloadable_ledger(tmp_path: Path) -> None:
    path = tmp_path / "ledger.yaml"
    path.write_text(dump_ledger(Ledger()))
    ledger = Ledger(jobs=[LedgerEntry.model_validate(entry_data())])
    ledger.jobs.append(LedgerEntry.model_validate(entry_data()))  # bypasses the validator
    with pytest.raises(ValueError, match="duplicate"):
        save_ledger(path, ledger)
    assert load_ledger(path).jobs == []  # the old file stands


# ---------------------------------------------------------------------------
# Native probes over a fake host.


class FakeShell:
    def __init__(self, responses: dict[str, ShellResult]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, command: str) -> ShellResult:
        self.calls.append(command)
        for prefix, result in self.responses.items():
            if command.startswith(prefix):
                return result
        return ShellResult(rc=1, err=f"unexpected: {command}")


def test_systemd_probe_reads_state_fires_and_a_running_service() -> None:
    timers = [{"unit": "cache-prune-daily.timer", "activates": "cache-prune-daily.service",
               "next": 1793086980000000, "last": 1793000580000000}]
    shell = FakeShell({
        "systemctl --user is-enabled cache-prune-daily.timer": ShellResult(0, "enabled\n"),
        "systemctl --user is-active cache-prune-daily.timer": ShellResult(0, "active\n"),
        "systemctl --user list-timers": ShellResult(0, json.dumps(timers)),
        "systemctl --user is-active cache-prune-daily.service": ShellResult(0, "activating\n"),
    })
    native = LedgerEntry.model_validate(entry_data()).native
    state = probe(native, shell=shell, now=NOW)
    assert state.active and not state.retired and state.running
    assert state.next_fire == datetime.fromtimestamp(1793086980, UTC)
    assert state.last_fire == datetime.fromtimestamp(1793000580, UTC)


@pytest.mark.parametrize(
    ("enabled", "active", "expect_active", "expect_retired"),
    [("disabled", "inactive", False, True), ("not-found", "inactive", False, True),
     ("enabled", "inactive", False, False), ("linked", "active", True, False)],
)
def test_systemd_retired_means_inactive_and_not_enabled(
    enabled: str, active: str, expect_active: bool, expect_retired: bool
) -> None:
    shell = FakeShell({
        "systemctl --user is-enabled": ShellResult(0, enabled),
        "systemctl --user is-active": ShellResult(0, active),
        "systemctl --user list-timers": ShellResult(0, "[]"),
    })
    state = probe(LedgerEntry.model_validate(entry_data()).native, shell=shell, now=NOW)
    assert (state.active, state.retired) == (expect_active, expect_retired)
    assert state.next_fire is None


def test_hermes_probe_reads_the_job_record(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs.json"
    record = {"id": "b240575fae0c", "enabled": True, "scheduler": None,
              "next_run_at": "2026-10-27T05:53:00-04:00",
              "last_run_at": "2026-10-26T05:59:11.329083-04:00", "state": "scheduled"}
    jobs.write_text(json.dumps({"jobs": [record]}))
    native = NativeScheduler(kind="hermes", schedule="53 5 * * * (profile tz)",
                             profile="peter-griffin", job_id="b240575fae0c", jobs_file=str(jobs))
    state = probe(native, now=NOW)
    assert state.active and not state.retired
    assert state.next_fire == datetime(2026, 10, 27, 9, 53, tzinfo=UTC)
    jobs.write_text(json.dumps({"jobs": [{**record, "scheduler": "external"}]}))
    state = probe(native, now=NOW)
    assert state.retired and not state.active and state.next_fire is None
    missing = probe(native.model_copy(update={"job_id": "nope"}), now=NOW)
    assert not missing.active and not missing.retired


def test_cron_fires_are_dst_correct() -> None:
    # 01:40 local on the 2026-11-01 fall-back day fires once, at 05:40Z (AD-16).
    prev, nxt = cron_fires("40 1 * * *", "America/New_York", datetime(2026, 11, 1, 4, tzinfo=UTC))
    assert nxt == datetime(2026, 11, 1, 5, 40, tzinfo=UTC)
    assert prev == datetime(2026, 10, 31, 5, 40, tzinfo=UTC)


def test_unit_file_is_resolved_through_links(tmp_path: Path) -> None:
    real = tmp_path / "repo" / "x.timer"
    real.parent.mkdir()
    real.write_text("[Timer]\n")
    link = tmp_path / "x.timer"
    link.symlink_to(real)
    shell = FakeShell({"systemctl --user show -p FragmentPath": ShellResult(0, f"{link}\n")})
    assert unit_file_of("x.timer", shell) == str(real)
    assert unit_file_of("x.timer", FakeShell({"systemctl": ShellResult(0, "\n")})) is None


def test_default_systemd_rollback_enables_by_path() -> None:
    native = NativeScheduler(kind="systemd-timer", schedule="x", unit="a.timer",
                             unit_file="/home/u/code/infra/systemd/user/a.timer")
    disable, rollback = default_commands(native)
    assert disable == ["systemctl --user disable --now a.timer"]
    assert rollback == ["systemctl --user enable --now /home/u/code/infra/systemd/user/a.timer"]


# ---------------------------------------------------------------------------
# The default crontab and hermes commands, run for real against fake binaries.


def _fake_bin(tmp_path: Path, name: str, body: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / name
    script.write_text("#!/bin/bash\nset -euo pipefail\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


@pytest.fixture()
def fake_crontab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    store = tmp_path / "crontab.txt"
    _fake_bin(
        tmp_path,
        "crontab",
        f'''store={store}
if [ "${{1:-}}" = "-l" ]; then
  [ -f "$store" ] || {{ echo "no crontab for $USER" >&2; exit 1; }}
  cat "$store"
elif [ "${{1:-}}" = "-" ]; then
  # Like the real crontab: read the whole new table, then install it.
  tmp=$(mktemp)
  cat > "$tmp"
  mv "$tmp" "$store"
else
  exit 2
fi
''',
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    return store


def test_default_crontab_commands_remove_and_restore_exactly_one_line(fake_crontab: Path) -> None:
    line = "30 */2 * * * /home/u/scripts/sync-vault-to-drive.sh 'quoted arg' >> /tmp/x.log 2>&1"
    other = "*/10 * * * * /home/u/bin/traefik-deathwatch"
    fake_crontab.write_text(f"# header\n{other}\n{line}\n")
    native = NativeScheduler(kind="crontab", schedule="crontab:22", line=line,
                             timezone="America/New_York")
    disable, rollback = default_commands(native)

    assert probe(native, shell=run_shell, now=NOW).active
    for command in disable:
        assert run_shell(command).ok
    assert fake_crontab.read_text() == f"# header\n{other}\n"
    state = probe(native, shell=run_shell, now=NOW)
    assert state.retired and not state.active

    for _ in range(2):  # idempotent: a re-run never duplicates the line
        for command in rollback:
            assert run_shell(command).ok
    assert fake_crontab.read_text() == f"# header\n{other}\n{line}\n"
    assert probe(native, shell=run_shell, now=NOW).active


def test_crontab_disable_of_the_only_line_and_rollback_into_no_crontab(fake_crontab: Path) -> None:
    line = "0 3 * * * /bin/true"
    fake_crontab.write_text(f"{line}\n")
    native = NativeScheduler(kind="crontab", schedule="x", line=line, timezone="UTC")
    disable, rollback = default_commands(native)
    assert all(run_shell(c).ok for c in disable)
    assert fake_crontab.read_text() == ""
    fake_crontab.unlink()  # no crontab at all
    assert all(run_shell(c).ok for c in rollback)
    assert fake_crontab.read_text() == f"{line}\n"


def test_crontab_disable_fails_loudly_without_a_crontab(fake_crontab: Path) -> None:
    native = NativeScheduler(kind="crontab", schedule="x", line="0 3 * * * /bin/true",
                             timezone="UTC")
    [disable], _ = default_commands(native)
    assert not run_shell(disable).ok  # pipefail: never install an empty crontab blindly


def test_default_hermes_commands_pin_and_unpin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "hermes.log"
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    hermes = home / ".local" / "bin" / "hermes"
    hermes.write_text(f'#!/bin/bash\necho "$@" >> {log}\n')
    hermes.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    native = NativeScheduler(kind="hermes", schedule="x", profile="peter-griffin",
                             job_id="b240575fae0c", jobs_file="/dev/null")
    disable, rollback = default_commands(native)
    assert all(run_shell(c).ok for c in disable + rollback)
    assert log.read_text().splitlines() == [
        "-p peter-griffin cron edit b240575fae0c --scheduler external",
        "-p peter-griffin cron edit b240575fae0c --scheduler internal",
    ]


def test_run_shell_reports_failure_and_timeout() -> None:
    assert run_shell("exit 3").rc == 3
    assert run_shell("false | true").rc == 1  # pipefail
    assert run_shell("sleep 5", timeout=0.2).rc == 124


# ---------------------------------------------------------------------------
# Live: the default systemd-timer commands on the real user manager.


@pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_SYSTEMD") != "1" or shutil.which("systemctl") is None,
    reason="set CANDYBAR_TEST_SYSTEMD=1 to run against the real systemd user manager",
)
def test_live_default_commands_round_trip_a_linked_timer(tmp_path: Path) -> None:
    """`disable` of a `systemctl link`-ed timer removes its link; the default rollback
    (enable by the resolved path) brings it back enabled and active."""
    name = f"cb-ledger-test-{os.getpid()}"
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    (unit_dir / f"{name}.service").write_text("[Service]\nType=oneshot\nExecStart=/bin/true\n")
    (unit_dir / f"{name}.timer").write_text(
        "[Timer]\nOnCalendar=*-01-01 03:33:00\n[Install]\nWantedBy=timers.target\n"
    )
    timer = f"{name}.timer"

    def sh(*args: str) -> str:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                              check=False).stdout.strip()

    try:
        sh("link", str(unit_dir / f"{name}.service"), str(unit_dir / timer))
        sh("enable", "--now", timer)
        native = NativeScheduler(kind="systemd-timer", schedule="OnCalendar=*-01-01 03:33",
                                 unit=timer)
        native.unit_file = unit_file_of(timer)
        assert native.unit_file == str((unit_dir / timer).resolve())
        assert probe(native).active
        disable, rollback = default_commands(native)
        assert all(run_shell(c).ok for c in disable)
        retired = probe(native)
        assert retired.retired and not retired.active
        assert sh("is-enabled", timer) == "not-found"  # the link went with `disable`
        assert run_shell(f"systemctl --user enable --now {timer}").rc != 0  # by name: gone
        assert all(run_shell(c).ok for c in rollback)
        back = probe(native)
        assert back.active and back.next_fire is not None
    finally:
        sh("disable", "--now", timer)
        sh("disable", f"{name}.service")
        sh("daemon-reload")
        sh("reset-failed", timer)
