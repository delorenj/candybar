"""Story 7.3 (AD-18): the systemd runner.

Two layers. Most tests drive the real runner against a stub `systemctl` /
`journalctl` (a script that answers `show` from a scripted queue and logs
every call), which pins the drop-in round trip, the holder reference, the
metering arithmetic and the failure mapping deterministically on any host.

`test_live_*` run against the real user manager (systemd 257 reality check):
they are the proof that a successful oneshot's accounting survives only while
the holder references it, and that the whole path meters a real unit. They are
opt-in (`CANDYBAR_TEST_SYSTEMD=1`) because they install a throwaway unit under
`~/.config/systemd/user` and clean it up.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

import pytest

from candybar.executor import systemd_runner as sr
from candybar.executor.runner import ProcessRegistry
from candybar.executor.systemd_runner import (
    SystemdHost,
    cpu_load,
    ensure_installed,
    read_held_units,
    run_systemd,
    slice_path,
)
from candybar.executor.targets import run_on_failure, run_target
from candybar.executor.worker import resolve_outcome
from candybar.runners import SystemdRunner

PY = sys.executable
UNIT = "cache-prune.service"
RUNNER = SystemdRunner(kind="systemd", unit=UNIT)
IDS = {"invocation_id": uuid4(), "candybar_id": uuid4(), "trigger": "scheduled", "tick_id": "tick:7"}

STUB = textwrap.dedent(
    f"""\
    #!{PY}
    import json, os, sys, time
    path = os.environ["STUB_STATE"]
    state = json.load(open(path))
    args = sys.argv[1:]
    state["calls"].append(args)
    verb = next((a for a in args if a in ("show", "start", "daemon-reload", "-o")), "")

    def save():
        json.dump(state, open(path, "w"))

    if "daemon-reload" in args:
        save(); sys.exit(0)
    if "show" in args:
        queue = state["show"]
        props = queue.pop(0) if len(queue) > 1 else queue[0]
        save()
        if props is None:
            sys.exit(1)
        wanted = [args[i + 1] for i, a in enumerate(args) if a == "-p"]
        for k in wanted:
            if k in props:
                print(f"{{k}}={{props[k]}}")
        sys.exit(0)
    if "start" in args:
        # Prove the env file exists WHEN the unit would read it.
        env = os.path.join(os.environ["STUB_RUNTIME"], "candybar", "inv", args[-1] + ".env")
        state["env_at_start"] = open(env).read() if os.path.exists(env) else None
        sleep = state.get("start_sleep", 0)
        save()
        if sleep:
            time.sleep(sleep)
        sys.stderr.write(state.get("start_stderr", ""))
        sys.exit(state.get("start_rc", 0))
    if "-o" in args:  # journalctl
        save()
        sys.stdout.write(state.get("journal", ""))
        sys.exit(state.get("journal_rc", 0))
    save(); sys.exit(0)
    """
)

LOADED = {"LoadState": "loaded", "ActiveState": "inactive", "Slice": "app.slice"}
RAN_OK = {
    "Result": "success",
    "ExecMainStatus": "0",
    "ExecMainStartTimestampMonotonic": "1000000",
    "ExecMainExitTimestampMonotonic": "3000000",  # 2 s of main-process time
    "CPUUsageNSec": "1000000000",  # 1 s of CPU
    "MemoryPeak": "52428800",
    "InvocationID": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
    "ActiveState": "inactive",
}


class Stub:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.state_path = tmp_path / "state.json"
        self.runtime = tmp_path / "run"
        self.unit_dir = tmp_path / "units"
        self.runtime.mkdir()
        self.unit_dir.mkdir()
        script = tmp_path / "systemctl"
        script.write_text(STUB)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        self.script = script
        monkeypatch.setenv("STUB_STATE", str(self.state_path))
        monkeypatch.setenv("STUB_RUNTIME", str(self.runtime))
        self.host = SystemdHost(
            systemctl=str(script),
            journalctl=str(script),
            unit_dir=self.unit_dir,
            runtime_dir=self.runtime,
        )
        self.set(show=[LOADED, RAN_OK])

    def set(self, **state: Any) -> None:
        current = json.loads(self.state_path.read_text()) if self.state_path.exists() else {"calls": []}
        current.update(state)
        self.state_path.write_text(json.dumps(current))

    @property
    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text())

    def calls(self, verb: str) -> list[list[str]]:
        return [c for c in self.state["calls"] if verb in c]


@pytest.fixture()
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stub:
    return Stub(tmp_path, monkeypatch)


def go(coro: Any) -> Any:
    return asyncio.run(coro)


def run_unit(stub: Stub, **kw: Any):
    return go(run_systemd(runner=RUNNER, host=stub.host, **{**IDS, **kw}))


# ---------------------------------------------------------------------------
# Pure helpers.


def test_slice_path_nests_dashed_slices() -> None:
    assert slice_path("app.slice") == "app.slice"
    assert slice_path("a-b-c.slice") == "a.slice/a-b.slice/a-b-c.slice"
    assert slice_path("-.slice") == ""
    assert slice_path("") == ""


def test_cpu_load_is_cpu_time_over_the_main_process_span() -> None:
    assert cpu_load(1_000_000_000, 2_000_000) == 0.5  # 1 s CPU in a 2 s span
    assert cpu_load(None, 2_000_000) == 0.0
    assert cpu_load(0, 2_000_000) == 0.0
    assert cpu_load(5, 0) == 0.0


def test_uint_treats_unset_markers_as_unmeasured() -> None:
    assert sr._uint("42") == 42
    assert sr._uint("[not set]") is None
    assert sr._uint(str(2**64 - 1)) is None
    assert sr._uint(None) is None and sr._uint("") is None


def test_env_file_quotes_and_escapes_values() -> None:
    text = sr._env_file_text({"A": 'say "hi"', "B": "back\\slash", "C": "plain"})
    assert text == 'A="say \\"hi\\""\nB="back\\\\slash"\nC="plain"\n'


# ---------------------------------------------------------------------------
# The drop-in round trip and the holder reference.


def test_first_run_installs_the_dropin_and_holder_then_reloads_once(stub: Stub) -> None:
    outcome = run_unit(stub)
    assert outcome.exit_code == 0 and outcome.spawn_error is None
    dropin = stub.host.dropin(UNIT)
    assert dropin.read_text() == sr.DROPIN_TEXT
    assert "EnvironmentFile=-%t/candybar/inv/%n.env" in dropin.read_text()
    assert read_held_units(stub.host.holder_dropin) == {UNIT}
    assert len(stub.calls("daemon-reload")) == 1


def test_a_second_run_is_idempotent_and_does_not_reload(stub: Stub) -> None:
    run_unit(stub)
    stub.set(show=[LOADED, RAN_OK])
    before = len(stub.calls("daemon-reload"))
    run_unit(stub)
    assert len(stub.calls("daemon-reload")) == before


def test_holder_dropin_accumulates_sorted_units_and_survives_repeat_installs(stub: Stub) -> None:
    for unit in ("zeta.service", "alpha.service", "alpha.service"):
        go(ensure_installed(stub.host, unit))
    text = stub.host.holder_dropin.read_text()
    assert text.splitlines()[-2:] == ["After=alpha.service", "After=zeta.service"]
    assert text.count("After=alpha.service") == 1
    assert "[Unit]" in text
    assert read_held_units(stub.host.holder_dropin) == {"alpha.service", "zeta.service"}


def test_a_hand_edited_dropin_is_restored(stub: Stub) -> None:
    go(ensure_installed(stub.host, UNIT))
    stub.host.dropin(UNIT).write_text("[Service]\nEnvironmentFile=/tampered\n")
    assert go(ensure_installed(stub.host, UNIT)) is True
    assert stub.host.dropin(UNIT).read_text() == sr.DROPIN_TEXT


def test_the_env_file_exists_at_start_carries_the_protocol_and_is_removed_after(stub: Stub) -> None:
    run_unit(stub)
    env = stub.state["env_at_start"]
    assert f'CANDYBAR_INVOCATION_ID="{IDS["invocation_id"]}"' in env
    assert f'CANDYBAR_ID="{IDS["candybar_id"]}"' in env
    assert 'CANDYBAR_TRIGGER="scheduled"' in env and 'CANDYBAR_TICK_ID="tick:7"' in env
    assert f'CANDYBAR_RESULT_FILE="{stub.host.result_file(IDS["invocation_id"])}"' in env
    assert not stub.host.env_file(UNIT).exists()
    assert not stub.host.result_file(IDS["invocation_id"]).exists()
    mode = (stub.runtime / "candybar" / "inv").stat().st_mode & 0o777
    assert mode == 0o700


def test_start_is_systemctl_user_start_wait_on_the_unit(stub: Stub) -> None:
    run_unit(stub)
    assert stub.calls("start") == [["--user", "start", "--wait", UNIT]]


# ---------------------------------------------------------------------------
# Metering and verdicts.


def test_success_is_metered_from_the_units_own_accounting(stub: Stub) -> None:
    outcome = run_unit(stub)
    assert outcome.exit_code == 0 and resolve_outcome(outcome).success
    assert outcome.memory_peak_bytes == 52428800
    assert outcome.cpu_load == 0.5  # 1 s CPU over the 2 s main-process span
    assert outcome.detail is None and outcome.stderr_tail == ""
    assert outcome.scoped is True


def test_cpu_falls_back_to_wall_runtime_without_unit_timestamps(stub: Stub) -> None:
    props = {**RAN_OK, "ExecMainStartTimestampMonotonic": "0", "ExecMainExitTimestampMonotonic": "0"}
    stub.set(show=[LOADED, props], start_sleep=0.2)
    outcome = run_unit(stub)
    assert 0 < outcome.cpu_load <= 5.0  # 1 s of CPU over ~0.2 s wall: finite, positive


def test_a_collected_unit_meters_zero_but_still_succeeds(stub: Stub) -> None:
    """The systemd 257 behaviour the holder exists to prevent: [not set] and no InvocationID."""
    collected = {
        "Result": "success",
        "ExecMainStatus": "0",
        "ExecMainStartTimestampMonotonic": "0",
        "ExecMainExitTimestampMonotonic": "0",
        "CPUUsageNSec": "[not set]",
        "MemoryPeak": "[not set]",
        "InvocationID": "",
        "ActiveState": "inactive",
    }
    stub.set(show=[LOADED, collected])
    outcome = run_unit(stub)
    assert outcome.exit_code == 0 and outcome.memory_peak_bytes == 0 and outcome.cpu_load == 0.0
    assert outcome.scoped is False


def test_success_exit_status_maps_exit1_to_success(stub: Stub) -> None:
    """Story 7.13 pin: a unit with `SuccessExitStatus=1` (newapi-vm-health) exits 1 when it
    found problems it already pushed to ntfy; systemd then reports Result=success with
    ExecMainStatus=1, and the runner must record a candybar success — not a failure."""
    ok_with_exit1 = {**RAN_OK, "Result": "success", "ExecMainStatus": "1", "ActiveState": "inactive"}
    stub.set(show=[LOADED, ok_with_exit1])
    outcome = run_unit(stub)
    assert outcome.exit_code == 0
    assert outcome.detail is None
    assert resolve_outcome(outcome).success is True
    # No journal fetch for a success.
    assert stub.calls("-o") == []


def test_a_unit_that_exits_nonzero_fails_with_its_status_and_journal_tail(stub: Stub) -> None:
    failed = {**RAN_OK, "Result": "exit-code", "ExecMainStatus": "3", "ActiveState": "failed"}
    stub.set(show=[LOADED, failed], start_rc=1, start_stderr="Job failed", journal="boom\nlast line\n")
    outcome = run_unit(stub)
    assert outcome.exit_code == 3
    assert outcome.detail == f"unit {UNIT} failed: Result=exit-code, exit status 3"
    assert outcome.stderr_tail == "boom\nlast line"
    resolved = resolve_outcome(outcome)
    assert resolved.success is False and resolved.message == outcome.detail
    # The journal is asked for exactly this invocation.
    (journal,) = stub.calls("-o")
    assert f"_SYSTEMD_INVOCATION_ID={RAN_OK['InvocationID']}" in journal


def test_a_timeout_or_signal_result_is_a_failure_with_the_systemctl_code(stub: Stub) -> None:
    timed = {**RAN_OK, "Result": "timeout", "ExecMainStatus": "15", "ActiveState": "failed"}
    stub.set(show=[LOADED, timed], start_rc=1)
    outcome = run_unit(stub)
    assert outcome.exit_code == 1 and "Result=timeout" in (outcome.detail or "")


def test_a_unit_whose_start_fails_maps_to_failed(stub: Stub) -> None:
    """Never ran (dependency, start-limit, bad ExecStart): systemctl's own message is the reason."""
    never = {"Result": "resources", "ExecMainStatus": "0", "InvocationID": "", "ActiveState": "failed"}
    stub.set(
        show=[LOADED, never],
        start_rc=1,
        start_stderr="Job for cache-prune.service failed because of a start limit.",
    )
    outcome = run_unit(stub)
    assert outcome.exit_code is None
    assert outcome.spawn_error == "Job for cache-prune.service failed because of a start limit."
    assert resolve_outcome(outcome).success is False
    assert outcome.stderr_tail.startswith("Job for cache-prune.service")


def test_failure_without_a_journal_falls_back_to_the_clients_stderr(stub: Stub) -> None:
    failed = {**RAN_OK, "Result": "exit-code", "ExecMainStatus": "2", "ActiveState": "failed"}
    stub.set(show=[LOADED, failed], start_rc=1, start_stderr="client said no", journal_rc=1)
    assert run_unit(stub).stderr_tail == "client said no"


def test_the_journal_tail_is_capped_and_scrubbed(stub: Stub) -> None:
    failed = {**RAN_OK, "Result": "exit-code", "ExecMainStatus": "1", "ActiveState": "failed"}
    stub.set(show=[LOADED, failed], start_rc=1, journal="x" * 2000 + "bad\x00end")
    tail = run_unit(stub).stderr_tail
    assert len(tail) <= 500 and tail.endswith("bad�end") and "\x00" not in tail


def test_a_unit_that_is_not_installed_fails_without_touching_anything(stub: Stub) -> None:
    stub.set(show=[{"LoadState": "not-found", "ActiveState": "inactive"}])
    outcome = run_unit(stub)
    assert outcome.exit_code is None and "not loaded" in (outcome.spawn_error or "")
    assert stub.calls("start") == [] and stub.calls("daemon-reload") == []
    assert not stub.host.dropin(UNIT).exists()


def test_an_unreachable_user_manager_is_a_failed_run(stub: Stub) -> None:
    stub.set(show=[None])
    outcome = run_unit(stub)
    assert "user manager" in (outcome.spawn_error or "")
    assert stub.calls("start") == []


@pytest.mark.parametrize("state", ["active", "activating", "reloading", "deactivating"])
def test_an_already_running_unit_is_refused_not_joined(stub: Stub, state: str) -> None:
    stub.set(show=[{**LOADED, "ActiveState": state, "SubState": "running"}])
    outcome = run_unit(stub)
    assert "overlapping start refused" in (outcome.spawn_error or "")
    assert f"ActiveState={state}, SubState=running" in outcome.spawn_error
    assert stub.calls("start") == []  # joining would meter someone else's run


def test_a_result_file_the_unit_wrote_is_honoured(stub: Stub) -> None:
    result = stub.host.result_file(IDS["invocation_id"])
    stub.set(start_sleep=0)

    async def write_then_run() -> Any:
        # The unit would write this during `start --wait`; the runner reads it after.
        stub.host.inv_dir.mkdir(parents=True, exist_ok=True)
        result.write_text(json.dumps({"status": "success", "metrics": {"token_spend": 41}}))
        return await run_systemd(runner=RUNNER, host=stub.host, **IDS)

    outcome = go(write_then_run())
    assert outcome.result is not None and outcome.result.metrics.token_spend == 41
    assert resolve_outcome(outcome).metrics.token_spend == 41


def test_a_result_file_that_reports_failure_fails_a_zero_exit(stub: Stub) -> None:
    result = stub.host.result_file(IDS["invocation_id"])

    async def go_() -> Any:
        stub.host.inv_dir.mkdir(parents=True, exist_ok=True)
        result.write_text(json.dumps({"status": "fail", "message": "soft failure"}))
        return await run_systemd(runner=RUNNER, host=stub.host, **IDS)

    resolved = resolve_outcome(go(go_()))
    assert resolved.success is False and resolved.message == "soft failure"


def test_the_systemctl_client_is_registered_so_shutdown_can_reach_it(stub: Stub) -> None:
    stub.set(start_sleep=30)
    registry = ProcessRegistry()

    async def drive() -> Any:
        task = asyncio.create_task(
            run_systemd(runner=RUNNER, host=stub.host, registry=registry, **IDS)
        )
        for _ in range(200):
            await asyncio.sleep(0.02)
            if registry.procs:
                break
        assert registry.procs
        await registry.terminate_all()
        return await asyncio.wait_for(task, 20)

    outcome = go(drive())
    assert outcome.exit_code not in (0, None)
    assert resolve_outcome(outcome, stopped=True).message == "executor stopped (SIGTERM)"
    assert not registry.procs


def test_on_wait_fires_while_the_unit_runs(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sr, "WAIT_SLICE_SECONDS", 0.1)
    stub.set(start_sleep=0.6)
    beats: list[int] = []

    async def beat() -> None:
        beats.append(1)

    run_unit(stub, on_wait=beat)
    assert len(beats) >= 2  # the bus message is kept alive across a long unit


# ---------------------------------------------------------------------------
# run_target dispatch and on_failure for systemd.


def test_run_target_dispatches_a_systemd_runner_dict(stub: Stub) -> None:
    outcome = go(run_target(runner={"kind": "systemd", "unit": UNIT}, host=stub.host, **IDS))
    assert outcome.exit_code == 0 and outcome.memory_peak_bytes == 52428800
    assert stub.calls("start")


def test_on_failure_is_skipped_when_the_unit_already_has_onfailure(
    stub: Stub, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    marker = tmp_path / "hook-ran"
    runner = {"kind": "systemd", "unit": UNIT, "on_failure": ["touch", str(marker)]}
    stub.set(show=[{"OnFailure": "ntfy-failure@%n.service"}])
    go(run_on_failure(runner, message="x", host=stub.host, **IDS))
    assert not marker.exists() and "already has OnFailure" in caplog.text


def test_on_failure_runs_when_the_unit_has_no_onfailure(stub: Stub, tmp_path: Path) -> None:
    marker = tmp_path / "hook-ran"
    runner = {"kind": "systemd", "unit": UNIT, "on_failure": ["touch", str(marker)]}
    stub.set(show=[{"OnFailure": ""}])
    go(run_on_failure(runner, message="x", host=stub.host, **IDS))
    assert marker.exists()


# ---------------------------------------------------------------------------
# Tracked deploy artifacts.

HOST_DIR = Path(__file__).resolve().parent.parent / "deploy" / "host"


def test_holder_unit_is_a_bare_sleep_with_no_accounting_cost() -> None:
    text = (HOST_DIR / "candybar-unit-holder.service").read_text()
    assert "ExecStart=/usr/bin/sleep infinity" in text
    assert "Restart=always" in text and "WantedBy=default.target" in text
    assert sr.HOLDER_UNIT == "candybar-unit-holder.service"
    assert sr.HOLDER_DROPIN_NAME in text  # the doc names the generated drop-in


def test_smoke_unit_is_a_side_effect_free_oneshot_nobody_enables() -> None:
    text = (HOST_DIR / "candybar-smoke.service").read_text()
    assert "Type=oneshot" in text and "[Install]" not in text


def test_install_script_installs_the_holder_and_links_the_smoke_unit() -> None:
    text = (HOST_DIR / "install.sh").read_text()
    assert "candybar-unit-holder.service" in text
    assert 'link "$here/candybar-smoke.service"' in text
    assert "start candybar-unit-holder.service" in text


# ---------------------------------------------------------------------------
# Live: the real user manager (systemd 257 reality check).

LIVE = os.environ.get("CANDYBAR_TEST_SYSTEMD") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set CANDYBAR_TEST_SYSTEMD=1 (installs a throwaway user unit)")


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=False)


class LiveUnit:
    """A throwaway oneshot in the real user unit dir, removed (with its holder entry) afterwards."""

    def __init__(self, body: str, extra: str = "") -> None:
        self.name = f"candybar-test-{uuid4().hex[:10]}.service"
        self.host = SystemdHost.from_env()
        self.path = self.host.unit_dir / self.name
        self.body = body
        self.extra = extra

    def __enter__(self) -> Self:
        self.host.unit_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            f"[Unit]\nDescription=candybar 7.3 live test (temporary)\n{self.extra}\n"
            f"[Service]\nType=oneshot\nExecStart={self.body}\n"
        )
        _systemctl("daemon-reload")
        return self

    def __exit__(self, *exc: object) -> None:
        _systemctl("stop", self.name)
        _systemctl("reset-failed", self.name)
        self.path.unlink(missing_ok=True)
        dropin = self.host.unit_dir / f"{self.name}.d"
        for child in dropin.glob("*"):
            child.unlink()
        if dropin.exists():
            dropin.rmdir()
        held = read_held_units(self.host.holder_dropin) - {self.name}
        if self.host.holder_dropin.exists():
            if held:
                self.host.holder_dropin.write_text(sr.held_units_text(held))
            else:
                self.host.holder_dropin.unlink()
        _systemctl("daemon-reload")


BURN = (
    "/bin/sh -c 'head -c 30000000 /dev/zero | tail -c 1 >/dev/null; "
    "echo out-line; echo err-line >&2; exit ${LIVE_EXIT:-0}'"
)


@live_only
def test_live_unit_accounting_is_lost_for_a_successful_oneshot_without_the_holder() -> None:
    """The premise the holder rests on: a finished successful oneshot is GC'd (257)."""
    with LiveUnit(BURN) as unit:
        assert _systemctl("start", "--wait", unit.name).returncode == 0
        shown = _systemctl("show", unit.name, "-p", "MemoryPeak", "-p", "InvocationID").stdout
        assert "MemoryPeak=[not set]" in shown and "InvocationID=\n" in shown


@live_only
def test_live_successful_unit_is_metered_end_to_end_and_the_env_reaches_it(tmp_path: Path) -> None:
    seen = tmp_path / "seen.txt"
    body = f"/bin/sh -c 'echo $CANDYBAR_ID $CANDYBAR_TRIGGER $CANDYBAR_TICK_ID > {seen}; head -c 30000000 /dev/zero | tail -c 1 >/dev/null; sleep 0.2'"
    with LiveUnit(body) as unit:
        runner = SystemdRunner(kind="systemd", unit=unit.name)
        outcome = go(run_systemd(runner=runner, **IDS))
        assert outcome.exit_code == 0, outcome
        assert outcome.memory_peak_bytes > 0
        assert outcome.cpu_load > 0
        assert seen.read_text().split() == [str(IDS["candybar_id"]), "scheduled", "tick:7"]
        # Run it again: no daemon-reload needed, still metered (holder stays referenced).
        again = go(run_systemd(runner=runner, **{**IDS, "invocation_id": uuid4()}))
        assert again.exit_code == 0 and again.memory_peak_bytes > 0


@live_only
def test_live_failing_unit_maps_to_failed_with_status_and_journal_tail() -> None:
    body = "/bin/sh -c 'echo diag-from-unit >&2; exit 3'"
    with LiveUnit(body) as unit:
        outcome = go(run_systemd(runner=SystemdRunner(kind="systemd", unit=unit.name), **IDS))
        assert outcome.exit_code == 3
        assert "exit status 3" in (outcome.detail or "")
        assert "diag-from-unit" in outcome.stderr_tail
        assert resolve_outcome(outcome).success is False


@live_only
def test_live_a_unit_that_cannot_start_is_a_failed_run() -> None:
    with LiveUnit("/nonexistent/binary/for/candybar/test") as unit:
        outcome = go(run_systemd(runner=SystemdRunner(kind="systemd", unit=unit.name), **IDS))
        assert resolve_outcome(outcome).success is False


@live_only
def test_live_unknown_unit_and_running_unit() -> None:
    missing = go(run_systemd(runner=SystemdRunner(kind="systemd", unit="candybar-no-such-unit.service"), **IDS))
    assert "not loaded" in (missing.spawn_error or "")
    with LiveUnit("/bin/sleep 3") as unit:
        runner = SystemdRunner(kind="systemd", unit=unit.name)

        async def overlap() -> Any:
            first = asyncio.create_task(run_systemd(runner=runner, **IDS))
            await asyncio.sleep(1.0)
            second = await run_systemd(runner=runner, **{**IDS, "invocation_id": uuid4()})
            return second, await first

        second, first = go(overlap())
        assert "overlapping start refused" in (second.spawn_error or "")
        assert first.exit_code == 0


@live_only
def test_live_unit_with_its_own_onfailure_skips_the_hook(tmp_path: Path) -> None:
    marker = tmp_path / "hook-ran"
    with LiveUnit("/bin/false", extra="OnFailure=candybar-test-noop.service") as unit:
        runner = {"kind": "systemd", "unit": unit.name, "on_failure": ["touch", str(marker)]}
        go(run_on_failure(runner, message="x", **IDS))
        assert not marker.exists()
    with LiveUnit("/bin/false") as unit:
        runner = {"kind": "systemd", "unit": unit.name, "on_failure": ["touch", str(marker)]}
        go(run_on_failure(runner, message="x", **IDS))
        assert marker.exists()

