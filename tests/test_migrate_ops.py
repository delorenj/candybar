"""Story 7.5 (AD-21): `candybar migrate` verbs over a stub ledger, host and API.

The fake host simulates one systemd timer (state, fires, the disable/enable
commands); the fake API is the api role's REST surface. Every call, shell or
API, lands in ONE ordered log, so the tests pin the cutover order (register
disabled -> retire native -> enable) and the rollback order (restore native
-> disable candybar), with the API reachable and unreachable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from candybar.cli.main import app
from candybar.migration import ops
from candybar.migration.client import ApiError
from candybar.migration.ledger import (
    Ledger,
    LedgerEntry,
    dump_ledger,
    load_ledger,
    save_ledger,
)
from candybar.migration.native import ShellResult
from candybar.migration.ops import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_PENDING,
    EXIT_USAGE,
    Fires,
    Host,
    guard_for,
)
from test_migrate_ledger_native import entry_data

TIMER = "cache-prune-daily.timer"
SERVICE = "cache-prune-daily.service"
UNIT_FILE = "/home/u/code/infra/systemd/user/cache-prune-daily.timer"
DISABLE = f"systemctl --user disable --now {TIMER}"
ENABLE = f"systemctl --user enable --now {UNIT_FILE}"
# 2026-10-26 11:00 EDT. The candybar (`23 5 * * *` New York) fires next at
# 2026-10-27T09:23Z; the jittered native timer next at 09:27Z, last ran
# 2026-10-26T09:30Z (after the 09:23Z occurrence: nothing pending).
T0 = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)


def usec(instant: datetime) -> int:
    return int(instant.timestamp() * 1_000_000)


class World:
    """A host with one systemd timer and the candybar API, sharing one call log."""

    def __init__(self) -> None:
        self.now = T0
        self.log: list[str] = []
        # native timer
        self.enabled = True
        self.active = True
        self.next = datetime(2026, 10, 27, 9, 27, tzinfo=UTC)
        self.last: datetime | None = datetime(2026, 10, 26, 9, 30, tzinfo=UTC)
        self.service_running = False
        self.on_failure = ""
        self.fail: set[str] = set()  # commands that exit 1
        self.ignore_disable = False  # disable "succeeds" but the timer stays active
        # API
        self.candybars: dict[str, dict[str, Any]] = {}
        self.invocations: dict[str, list[dict[str, Any]]] = {}
        self.api_down = False
        self.fail_enable = False

    # -- shell ------------------------------------------------------------
    def shell(self, command: str) -> ShellResult:
        if self.now >= self.next and self.active:  # the timer fired: advance it
            self.last = self.next
            self.next += timedelta(days=1)
        if command.startswith("systemctl --user is-enabled"):
            return ShellResult(0, "enabled\n" if self.enabled else "not-found\n")
        if command.startswith(f"systemctl --user is-active {SERVICE}"):
            return ShellResult(0, "activating\n" if self.service_running else "inactive\n")
        if command.startswith("systemctl --user is-active"):
            return ShellResult(0, "active\n" if self.active else "inactive\n")
        if command.startswith("systemctl --user list-timers"):
            row = {"unit": TIMER, "activates": SERVICE,
                   "next": usec(self.next) if self.active else None,
                   "last": usec(self.last) if self.last else 0}
            return ShellResult(0, json.dumps([row]))
        if command.startswith("systemctl --user show -p FragmentPath"):
            return ShellResult(0, "")
        if command.startswith("systemctl --user show -p OnFailure"):
            return ShellResult(0, self.on_failure + "\n")
        self.log.append(f"$ {command}")
        if command in self.fail:
            return ShellResult(1, err="Failed to frobnicate")
        if command == DISABLE:
            if not self.ignore_disable:
                self.enabled = self.active = False
            return ShellResult(0)
        if command == ENABLE:
            self.enabled = self.active = True
            return ShellResult(0)
        return ShellResult(1, err=f"unexpected command {command}")

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    # -- API --------------------------------------------------------------
    def _up(self, what: str) -> None:
        if self.api_down:
            raise ApiError("candybar API unreachable at http://127.0.0.1:8700: refused")
        self.log.append(what)

    def list_candybars(self) -> list[dict[str, Any]]:
        self._up("GET /candybars")
        return list(self.candybars.values())

    def get_candybar(self, candybar_id: str) -> dict[str, Any] | None:
        self._up(f"GET {candybar_id}")
        return self.candybars.get(candybar_id)

    def register(self, definition: dict[str, Any]) -> dict[str, Any]:
        self._up(f"POST /candybars enabled={definition.get('enabled')}")
        if any(c["name"] == definition["name"] for c in self.candybars.values()):
            raise ApiError("POST /candybars: 409 taken", status=409)
        row = {**definition, "id": str(uuid4())}
        self.candybars[row["id"]] = row
        return row

    def update(self, candybar_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        self._up(f"PATCH {candybar_id}")
        self.candybars[candybar_id].update(changes)
        return self.candybars[candybar_id]

    def set_enabled(self, candybar_id: str, enabled: bool) -> dict[str, Any]:
        self._up(f"enabled={enabled}")
        if self.fail_enable and enabled:
            raise ApiError("POST enabled: 503 bus down", status=503)
        if candybar_id not in self.candybars:
            raise ApiError("404", status=404)
        self.candybars[candybar_id]["enabled"] = enabled
        return self.candybars[candybar_id]

    def invocations_of(self, candybar_id: str, limit: int = 500) -> list[dict[str, Any]]:
        self._up("GET invocations")
        return self.invocations.get(candybar_id, [])

    def host(self) -> Host:
        api = _Api(self)
        return Host(api=api, shell=self.shell, read=lambda p: "", now=lambda: self.now,
                    sleep=self.sleep, echo=self.echoes.append)

    echoes: list[str]


class _Api:
    def __init__(self, world: World) -> None:
        self.w = world

    def __getattr__(self, name: str) -> Any:
        return getattr(self.w, "invocations_of" if name == "invocations" else name)


@pytest.fixture()
def world() -> World:
    w = World()
    w.echoes = []
    return w


@pytest.fixture()
def ledger_path(tmp_path: Path) -> Path:
    path = tmp_path / "deploy" / "migration" / "ledger.yaml"
    path.parent.mkdir(parents=True)
    entry = LedgerEntry.model_validate(
        entry_data(disable=[DISABLE], rollback=[ENABLE],
                   native={**entry_data()["native"], "unit_file": UNIT_FILE})
    )
    save_ledger(path, Ledger(jobs=[entry]))
    return path


JOB = "systemd:cache-prune-daily.timer"


def entry_of(path: Path) -> LedgerEntry:
    return load_ledger(path).entry(JOB)


def output(world: World) -> str:
    return "\n".join(world.echoes)


# ---------------------------------------------------------------------------
# cutover


def test_cutover_registers_disabled_then_retires_then_enables(world: World, ledger_path: Path) -> None:
    assert ops.cutover(world.host(), ledger_path, JOB) == EXIT_OK
    assert world.log == ["POST /candybars enabled=False", f"$ {DISABLE}", "enabled=True"]
    entry = entry_of(ledger_path)
    assert entry.status == "cutover" and entry.cutover_at == T0
    [registered] = world.candybars.values()
    assert entry.candybar_id == registered["id"] and registered["enabled"] is True
    assert registered["runner"] == {"kind": "systemd", "unit": SERVICE}
    assert registered["timezone"] == "America/New_York"
    assert not world.enabled and not world.active
    assert entry.history[-1].outcome == "ok"
    assert "2026-10-27T09:23:00Z" in entry.history[-1].detail  # the first candybar fire


def test_cutover_twice_is_refused(world: World, ledger_path: Path) -> None:
    assert ops.cutover(world.host(), ledger_path, JOB) == EXIT_OK
    world.log.clear()
    assert ops.cutover(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert world.log == []


def test_cutover_refused_near_the_native_fire(world: World, ledger_path: Path) -> None:
    world.now = world.next - timedelta(seconds=90)
    assert ops.cutover(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert world.log == []  # nothing registered, nothing retired
    entry = entry_of(ledger_path)
    assert entry.status == "planned" and entry.history[-1].outcome == "refused"
    assert "native fire" in entry.history[-1].detail


def test_cutover_refused_near_the_candybar_fire(world: World, ledger_path: Path) -> None:
    world.next = datetime(2026, 10, 27, 9, 33, tzinfo=UTC)
    world.last = datetime(2026, 10, 26, 9, 33, tzinfo=UTC)
    world.now = datetime(2026, 10, 27, 9, 22, tzinfo=UTC)  # the candybar fires in 60s
    assert ops.cutover(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert "candybar fire" in entry_of(ledger_path).history[-1].detail
    assert world.log == []


def test_cutover_refused_while_a_jittered_native_run_may_be_pending(
    world: World, ledger_path: Path
) -> None:
    # 09:23Z occurrence passed, the native timer (10m jitter) fires at 09:31Z.
    world.last = datetime(2026, 10, 26, 9, 30, tzinfo=UTC)
    world.next = datetime(2026, 10, 27, 9, 31, tzinfo=UTC)
    world.now = datetime(2026, 10, 27, 9, 25, tzinfo=UTC)
    assert ops.cutover(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert "may still be pending" in entry_of(ledger_path).history[-1].detail


def test_cutover_refused_in_the_first_seconds_of_a_fire_minute(world: World, ledger_path: Path) -> None:
    world.next = datetime(2026, 10, 27, 9, 23, tzinfo=UTC)  # no jitter on this occurrence
    world.now = datetime(2026, 10, 27, 9, 23, 5, tzinfo=UTC)
    world.last = world.now - timedelta(seconds=5)
    world.shell(":")  # the timer fires at 09:23:00
    assert ops.cutover(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert "just happened" in entry_of(ledger_path).history[-1].detail


def test_cutover_refused_while_a_native_run_is_in_progress(world: World, ledger_path: Path) -> None:
    world.service_running = True
    assert ops.cutover(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert "in progress" in entry_of(ledger_path).history[-1].detail


def test_cutover_wait_lets_the_fire_pass_then_proceeds(world: World, ledger_path: Path) -> None:
    fire = world.next
    world.now = fire - timedelta(seconds=30)
    assert ops.cutover(world.host(), ledger_path, JOB, wait_s=600) == EXIT_OK
    # Waited past the native fire (the timer ran it), then past the guard.
    assert world.last == fire and world.now >= fire
    assert world.log == ["POST /candybars enabled=False", f"$ {DISABLE}", "enabled=True"]


def test_cutover_wait_gives_up(world: World, ledger_path: Path) -> None:
    world.service_running = True
    assert ops.cutover(world.host(), ledger_path, JOB, wait_s=20) == EXIT_FAILED
    assert world.now >= T0 + timedelta(seconds=20)
    assert world.log == []


def test_disable_command_failure_restores_native_and_keeps_candybar_disabled(
    world: World, ledger_path: Path
) -> None:
    world.fail = {DISABLE}
    assert ops.cutover(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert world.log == ["POST /candybars enabled=False", f"$ {DISABLE}", f"$ {ENABLE}"]
    [registered] = world.candybars.values()
    assert registered["enabled"] is False
    entry = entry_of(ledger_path)
    assert entry.status == "planned" and entry.candybar_id == registered["id"]
    assert "native restored" in entry.history[-1].detail
    assert world.active and world.enabled


def test_native_still_active_after_disable_is_a_failure(world: World, ledger_path: Path) -> None:
    world.ignore_disable = True
    assert ops.cutover(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert "not retired" in entry_of(ledger_path).history[-1].detail
    assert "enabled=True" not in world.log


def test_enable_failure_restores_native(world: World, ledger_path: Path) -> None:
    world.fail_enable = True
    assert ops.cutover(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert world.log == [
        "POST /candybars enabled=False", f"$ {DISABLE}", "enabled=True", f"$ {ENABLE}",
    ]
    assert world.active and entry_of(ledger_path).status == "planned"
    [registered] = world.candybars.values()
    assert registered["enabled"] is False


def test_failed_restore_asks_for_manual_action(world: World, ledger_path: Path) -> None:
    world.fail_enable = True
    world.fail = {ENABLE}
    assert ops.cutover(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert "MANUAL ACTION NEEDED" in output(world)
    assert "RESTORE FAILED" in entry_of(ledger_path).history[-1].detail


def test_a_surprise_after_retirement_still_restores_native(world: World, ledger_path: Path) -> None:
    calls = {"n": 0}
    real = world.shell

    def flaky(command: str) -> ShellResult:
        # The post-disable probe blows up (anything unexpected, not an ApiError).
        if command.startswith("systemctl --user is-enabled") and not world.active:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("probe exploded")
        return real(command)

    world.shell = flaky  # type: ignore[method-assign]
    assert ops.cutover(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert world.log[-1] == f"$ {ENABLE}" and world.active
    assert "probe exploded" in entry_of(ledger_path).history[-1].detail
    [registered] = world.candybars.values()
    assert registered["enabled"] is False


def test_register_failure_touches_nothing_native(world: World, ledger_path: Path) -> None:
    world.api_down = True
    assert ops.cutover(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert world.log == [] and world.active


def test_name_taken_by_an_unrecorded_candybar_is_refused(world: World, ledger_path: Path) -> None:
    world.candybars["x"] = {"id": "x", "name": "cache-prune-daily", "enabled": True}
    assert ops.cutover(world.host(), ledger_path, JOB) == EXIT_USAGE
    assert world.active and f"$ {DISABLE}" not in world.log


def test_missing_commit_blocks_cutover(world: World, ledger_path: Path) -> None:
    ledger = load_ledger(ledger_path)
    ledger.jobs[0].source_of_truth.commit = None
    save_ledger(ledger_path, ledger)
    assert ops.cutover(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert world.log == []
    assert "source_of_truth.commit" in entry_of(ledger_path).history[-1].detail


def test_on_failure_hook_on_a_unit_with_onfailure_blocks_cutover(
    world: World, ledger_path: Path
) -> None:
    ledger = load_ledger(ledger_path)
    ledger.jobs[0].candybar["runner"] = {"kind": "systemd", "unit": SERVICE,
                                         "on_failure": ["/bin/alert"]}
    save_ledger(ledger_path, ledger)
    world.on_failure = "cache-prune-alert.service"
    assert ops.cutover(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert "OnFailure=" in entry_of(ledger_path).history[-1].detail


def test_unknown_id_is_a_usage_error(world: World, ledger_path: Path) -> None:
    assert ops.cutover(world.host(), ledger_path, "systemd:nope.timer") == EXIT_USAGE


# ---------------------------------------------------------------------------
# rollback


def cut(world: World, path: Path) -> str:
    assert ops.cutover(world.host(), path, JOB) == EXIT_OK
    world.log.clear()
    return entry_of(path).candybar_id or ""


def test_rollback_restores_native_first_then_disables(world: World, ledger_path: Path) -> None:
    cid = cut(world, ledger_path)
    assert ops.rollback(world.host(), ledger_path, JOB, reason="drill") == EXIT_OK
    assert world.log == [f"$ {ENABLE}", "enabled=False"]
    assert world.active and world.candybars[cid]["enabled"] is False
    entry = entry_of(ledger_path)
    assert entry.status == "rolled-back" and not entry.disable_pending
    assert any("drill" in h.detail for h in entry.history)


def test_rollback_with_the_api_down_still_restores_and_finishes_later(
    world: World, ledger_path: Path
) -> None:
    cid = cut(world, ledger_path)
    world.api_down = True
    assert ops.rollback(world.host(), ledger_path, JOB) == EXIT_PENDING
    assert world.log == [f"$ {ENABLE}"] and world.active
    entry = entry_of(ledger_path)
    assert entry.status == "rolled-back" and entry.disable_pending
    assert world.candybars[cid]["enabled"] is True  # could not be reached
    # The API is back: a re-run finishes the disable without touching native again.
    world.api_down = False
    world.log.clear()
    assert ops.rollback(world.host(), ledger_path, JOB) == EXIT_OK
    assert world.log == ["enabled=False"]
    assert not entry_of(ledger_path).disable_pending
    assert world.candybars[cid]["enabled"] is False


def test_rollback_whose_restore_fails_leaves_the_candybar_enabled(
    world: World, ledger_path: Path
) -> None:
    cid = cut(world, ledger_path)
    world.fail = {ENABLE}
    assert ops.rollback(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert world.log == [f"$ {ENABLE}"]  # never disabled: the job keeps one scheduler
    assert world.candybars[cid]["enabled"] is True
    assert entry_of(ledger_path).status == "cutover"


def test_rollback_of_a_planned_or_finished_entry(world: World, ledger_path: Path) -> None:
    assert ops.rollback(world.host(), ledger_path, JOB) == EXIT_FAILED
    cut(world, ledger_path)
    assert ops.rollback(world.host(), ledger_path, JOB) == EXIT_OK
    world.log.clear()
    assert ops.rollback(world.host(), ledger_path, JOB) == EXIT_OK
    assert world.log == []


def test_recutover_after_a_pending_rollback_disables_first(world: World, ledger_path: Path) -> None:
    cid = cut(world, ledger_path)
    world.api_down = True
    assert ops.rollback(world.host(), ledger_path, JOB) == EXIT_PENDING
    world.api_down = False
    world.log.clear()
    world.now += timedelta(hours=1)
    assert ops.cutover(world.host(), ledger_path, JOB) == EXIT_OK
    assert world.log == [f"GET {cid}", "enabled=False", f"PATCH {cid}", f"$ {DISABLE}",
                         "enabled=True"]
    assert entry_of(ledger_path).candybar_id == cid and len(world.candybars) == 1


# ---------------------------------------------------------------------------
# decommission


def test_decommission_a_cut_over_job_disables_the_candybar_and_keeps_native_retired(
    world: World, ledger_path: Path
) -> None:
    cid = cut(world, ledger_path)
    assert ops.decommission(world.host(), ledger_path, JOB, reason="never used") == EXIT_OK
    assert world.log == ["enabled=False"]  # native was already retired: no command run
    assert world.candybars[cid]["enabled"] is False and not world.active
    entry = entry_of(ledger_path)
    assert entry.status == "decommissioned" and not entry.disable_pending
    assert [(h.action, h.outcome) for h in entry.history[-2:]] == [
        ("decommission", "ok"), ("decommission", "ok")]
    assert entry.history[-2].detail == "never used"


def test_decommission_requires_a_reason(world: World, ledger_path: Path) -> None:
    cut(world, ledger_path)
    assert ops.decommission(world.host(), ledger_path, JOB, reason="  ") == EXIT_FAILED
    assert world.log == [] and entry_of(ledger_path).status == "cutover"


def test_decommission_of_a_planned_job_retires_the_active_native_scheduler(
    world: World, ledger_path: Path
) -> None:
    assert ops.decommission(world.host(), ledger_path, JOB, reason="not needed") == EXIT_OK
    assert world.log == [f"$ {DISABLE}"]  # no candybar was ever registered: no API call
    assert not world.active and not world.candybars
    assert entry_of(ledger_path).status == "decommissioned"


def test_decommission_whose_native_retire_fails_changes_nothing(
    world: World, ledger_path: Path
) -> None:
    world.fail = {DISABLE}
    assert ops.decommission(world.host(), ledger_path, JOB, reason="x") == EXIT_FAILED
    entry = entry_of(ledger_path)
    assert entry.status == "planned" and entry.history[-1].outcome == "failed"
    assert world.active


def test_decommission_with_the_api_down_finishes_on_a_rerun(
    world: World, ledger_path: Path
) -> None:
    cid = cut(world, ledger_path)
    world.api_down = True
    assert ops.decommission(world.host(), ledger_path, JOB, reason="gone") == EXIT_PENDING
    entry = entry_of(ledger_path)
    assert entry.status == "decommissioned" and entry.disable_pending
    assert world.candybars[cid]["enabled"] is True  # unreachable
    world.api_down = False
    world.log.clear()
    assert ops.decommission(world.host(), ledger_path, JOB, reason="") == EXIT_OK
    assert world.log == ["enabled=False"]
    assert not entry_of(ledger_path).disable_pending
    assert world.candybars[cid]["enabled"] is False


def test_decommission_is_idempotent(world: World, ledger_path: Path) -> None:
    cut(world, ledger_path)
    assert ops.decommission(world.host(), ledger_path, JOB, reason="gone") == EXIT_OK
    world.log.clear()
    assert ops.decommission(world.host(), ledger_path, JOB, reason="again") == EXIT_OK
    assert world.log == []


def test_decommission_unknown_id_is_a_usage_error(world: World, ledger_path: Path) -> None:
    assert ops.decommission(world.host(), ledger_path, "systemd:nope.timer", reason="x") == EXIT_USAGE


def test_rollback_refuses_a_decommissioned_entry(world: World, ledger_path: Path) -> None:
    cut(world, ledger_path)
    assert ops.decommission(world.host(), ledger_path, JOB, reason="never used") == EXIT_OK
    world.log.clear()
    world.echoes.clear()
    assert ops.rollback(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert world.log == []  # the native scheduler was not restored
    assert "decommissioned" in output(world) and "never used" in output(world)
    assert not world.active and entry_of(ledger_path).status == "decommissioned"


def test_cutover_and_verify_refuse_a_decommissioned_entry(world: World, ledger_path: Path) -> None:
    cut(world, ledger_path)
    assert ops.decommission(world.host(), ledger_path, JOB, reason="gone") == EXIT_OK
    world.log.clear()
    assert ops.cutover(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert ops.verify(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert world.log == [] and entry_of(ledger_path).status == "decommissioned"


def test_decommissioned_entries_are_not_planned_by_default(world: World, ledger_path: Path) -> None:
    cut(world, ledger_path)
    assert ops.decommission(world.host(), ledger_path, JOB, reason="gone") == EXIT_OK
    world.echoes.clear()
    assert ops.plan(world.host(), ledger_path, []) == EXIT_OK
    assert "no entries to plan" in output(world)
    assert ops.plan(world.host(), ledger_path, [JOB]) == EXIT_OK  # explicit id: informational
    assert "decommissioned" in output(world) and "cutover now" not in output(world)


def test_plan_flags_a_resurrected_native_scheduler(world: World, ledger_path: Path) -> None:
    cut(world, ledger_path)
    assert ops.decommission(world.host(), ledger_path, JOB, reason="gone") == EXIT_OK
    world.enabled = world.active = True  # an installer re-ran
    assert ops.plan(world.host(), ledger_path, [JOB]) == EXIT_FAILED
    assert "ACTIVE for a decommissioned job" in output(world)


def test_status_marks_a_resurrected_decommissioned_entry(world: World, ledger_path: Path) -> None:
    cid = cut(world, ledger_path)
    assert ops.decommission(world.host(), ledger_path, JOB, reason="gone") == EXIT_OK
    world.echoes.clear()
    assert ops.status(world.host(), ledger_path, as_json=True, probe_native=True) == EXIT_OK
    [row] = json.loads(output(world))
    assert row["status"] == "decommissioned" and row["enabled"] is False
    assert row["native"] == "retired" and "resurrected" not in row
    world.candybars[cid]["enabled"] = True
    world.echoes.clear()
    assert ops.status(world.host(), ledger_path, probe_native=True) == EXIT_OK
    assert "RESURRECTED" in output(world)


# ---------------------------------------------------------------------------
# verify


def run_row(trigger: str, result: str | None, started: datetime, **kw: Any) -> dict[str, Any]:
    return {"id": str(uuid4()), "trigger": trigger, "result": result,
            "startedAt": started.isoformat(), "finishedAt": started.isoformat(),
            "exitCode": 0 if result == "success" else 1, "runtimeMs": 5,
            "memoryPeakBytes": 4096, "cpuLoad": 0.2, **kw}


def test_verify_waits_for_the_first_scheduled_run(world: World, ledger_path: Path) -> None:
    cid = cut(world, ledger_path)
    world.invocations[cid] = [
        run_row("manual", "success", T0 + timedelta(minutes=1)),  # never the proof
        run_row("scheduled", None, T0 + timedelta(minutes=2)),  # still running
    ]
    assert ops.verify(world.host(), ledger_path, JOB) == EXIT_PENDING
    assert entry_of(ledger_path).status == "cutover"
    assert "2026-10-27T09:23:00Z" in output(world)


def test_verify_success(world: World, ledger_path: Path) -> None:
    cid = cut(world, ledger_path)
    first = run_row("scheduled", "success", datetime(2026, 10, 27, 9, 23, tzinfo=UTC))
    later = run_row("scheduled", "fail", datetime(2026, 10, 28, 9, 23, tzinfo=UTC))
    early = run_row("scheduled", "fail", T0 - timedelta(days=1))  # before cutover
    world.invocations[cid] = [later, first, early]
    assert ops.verify(world.host(), ledger_path, JOB) == EXIT_OK
    entry = entry_of(ledger_path)
    assert entry.status == "verified"
    assert entry.verification.invocation_id == first["id"]
    assert entry.verification.result == "success"
    assert ops.verify(world.host(), ledger_path, JOB) == EXIT_OK  # idempotent


def test_verify_failed_first_run(world: World, ledger_path: Path) -> None:
    cid = cut(world, ledger_path)
    world.invocations[cid] = [run_row("misfire", "fail", T0 + timedelta(hours=1),
                                      errorMessage="exit code 3")]
    assert ops.verify(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert entry_of(ledger_path).status == "cutover"
    assert "rollback" in output(world)


def test_verify_on_result_parity_for_a_natively_failing_job(world: World, ledger_path: Path) -> None:
    ledger = load_ledger(ledger_path)
    ledger.jobs[0].native_failing = True
    save_ledger(ledger_path, ledger)
    cid = cut(world, ledger_path)
    world.invocations[cid] = [run_row("scheduled", "fail", T0 + timedelta(hours=1))]
    assert ops.verify(world.host(), ledger_path, JOB) == EXIT_OK
    entry = entry_of(ledger_path)
    assert entry.status == "verified" and entry.verification.result == "fail"


def test_verify_catches_a_resurrected_native_scheduler(world: World, ledger_path: Path) -> None:
    cid = cut(world, ledger_path)
    world.invocations[cid] = [run_row("scheduled", "success", T0 + timedelta(hours=1))]
    world.enabled = world.active = True  # an installer re-run re-enabled the timer
    assert ops.verify(world.host(), ledger_path, JOB) == EXIT_FAILED
    assert "two schedulers" in output(world)
    assert entry_of(ledger_path).status == "cutover"


def test_verify_before_cutover(world: World, ledger_path: Path) -> None:
    assert ops.verify(world.host(), ledger_path, JOB) == EXIT_FAILED


# ---------------------------------------------------------------------------
# plan and status


def test_plan_write_fills_defaults(world: World, tmp_path: Path) -> None:
    path = tmp_path / "ledger.yaml"
    save_ledger(path, Ledger(jobs=[LedgerEntry.model_validate(entry_data())]))
    assert ops.plan(world.host(), path, []) == EXIT_FAILED  # commands missing
    assert "plan --write" in output(world)
    assert ops.plan(world.host(), path, [], write=True) == EXIT_OK
    entry = entry_of(path)
    assert entry.disable == [DISABLE]
    # No unit file known to this fake systemd: enabled by name, with a warning.
    assert entry.rollback == [f"systemctl --user enable --now {TIMER}"]
    assert world.log == []  # plan never runs a command or touches the API
    text = output(world)
    assert "cutover now: allowed" in text and "2026-10-27T09:23:00Z" in text


def test_plan_reports_the_guard(world: World, ledger_path: Path) -> None:
    world.now = world.next - timedelta(seconds=30)
    assert ops.plan(world.host(), ledger_path, [JOB]) == EXIT_OK
    assert "cutover now: refused" in output(world)


def test_status_lists_entries_with_their_enabled_flag(world: World, ledger_path: Path) -> None:
    cid = cut(world, ledger_path)
    world.echoes.clear()
    assert ops.status(world.host(), ledger_path, as_json=True, probe_native=True) == EXIT_OK
    [row] = json.loads(output(world))
    assert row == {"id": JOB, "wave": "7.7", "status": "cutover", "candybar": "cache-prune-daily",
                   "candybarId": cid, "enabled": True, "disablePending": False,
                   "verifiedBy": None, "native": "retired"}
    world.api_down = True
    world.echoes.clear()
    assert ops.status(world.host(), ledger_path) == EXIT_OK
    assert "API unreachable" in output(world)


def test_guard_shrinks_for_frequent_jobs() -> None:
    def fires(minutes: int) -> Fires:
        return Fires(prev=T0, next=T0 + timedelta(minutes=minutes))

    assert guard_for(fires(1)) == timedelta(seconds=15)
    assert guard_for(fires(2)) == timedelta(seconds=30)
    assert guard_for(fires(5)) == timedelta(seconds=75)
    assert guard_for(fires(8)) == timedelta(minutes=2)
    assert guard_for(fires(1440)) == timedelta(minutes=2)
    assert guard_for(Fires(prev=None, next=None)) == timedelta(minutes=2)


def test_cli_wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "ledger.yaml"
    path.write_text(dump_ledger(Ledger()))
    monkeypatch.setenv("CANDYBAR_API_URL", "http://127.0.0.1:9")  # nothing listens
    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "status", "--ledger", str(path)])
    assert result.exit_code == 0 and "no entries" in result.output
    result = runner.invoke(app, ["migrate", "verify", "systemd:nope.timer", "--ledger", str(path)])
    assert result.exit_code == EXIT_USAGE
    result = runner.invoke(
        app, ["migrate", "decommission", "systemd:nope.timer", "--reason", "x", "--ledger", str(path)]
    )
    assert result.exit_code == EXIT_USAGE
    result = runner.invoke(app, ["migrate", "decommission", "systemd:nope.timer", "--ledger", str(path)])
    assert result.exit_code == 2  # --reason is required (typer usage error)
    monkeypatch.setenv("CANDYBAR_LEDGER", str(path))
    result = runner.invoke(app, ["migrate", "plan"])
    assert result.exit_code == 0 and "no entries to plan" in result.output
