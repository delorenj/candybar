"""Real cgroup v2 canaries for Story 3.1 (env-gated: CANDYBAR_TEST_CGROUP_ROOT).

Point CANDYBAR_TEST_CGROUP_ROOT at a writable cgroup v2 directory whose parent
offers the memory controller (the runner delegates +memory/+cpu to it), e.g.
as root:

    sudo mkdir -p /sys/fs/cgroup/candybar-test
    sudo -E env PYTHONDONTWRITEBYTECODE=1 \\
        CANDYBAR_TEST_CGROUP_ROOT=/sys/fs/cgroup/candybar-test \\
        .venv/bin/python -m pytest -p no:cacheprovider \\
        tests/test_cgroup_metering_integration.py

CI runs exactly that on the GitHub-hosted runner. A canary allocating a known
size must report a peak inside the asserted band; a one-core busy loop must
report a CPU load near 1.0. The worker path proves the same values reach the
terminal record, the event, and the gauge.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from prometheus_client import CollectorRegistry

from candybar.executor.metrics import MEMORY_PEAK_METRIC, ExecutorMetrics
from candybar.executor.runner import run_invocation
from candybar.executor.worker import Executor
from test_executor import CANDYBAR_ID, FakeBus, FakeMsg, FakeStore, make_command, run

ROOT = os.environ.get("CANDYBAR_TEST_CGROUP_ROOT", "")
pytestmark = pytest.mark.skipif(
    not ROOT, reason="set CANDYBAR_TEST_CGROUP_ROOT to a writable delegated cgroup v2 dir"
)

MIB = 1024 * 1024
CANARY_MIB = 64
# Lower bound: the canary itself; upper: + interpreter + page cache slack.
PEAK_BAND = (CANARY_MIB * MIB, 2 * CANARY_MIB * MIB)
CPU_BAND = (0.5, 1.5)


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


MEMORY_CANARY = f"""
import time
time.sleep(0.2)  # let the executor move us into the scope before we allocate
blob = b"\\x01" * ({CANARY_MIB} * 1024 * 1024)  # written pages, not lazy zero pages
time.sleep(0.2)
assert blob[-1] == 1
"""

CPU_CANARY = """
import time
time.sleep(0.05)
end = time.monotonic() + 0.8
n = 0
while time.monotonic() < end:
    n += 1
"""


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str):
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    return run(
        run_invocation(
            entrypoint=entrypoint,
            invocation_id=uuid4(),
            candybar_id=CANDYBAR_ID,
            trigger="manual",
            tick_id=None,
            root=ROOT,
        )
    )


def test_memory_canary_peak_within_band(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = _run(tmp_path, monkeypatch, _script(tmp_path, "mem.py", MEMORY_CANARY))
    assert outcome.exit_code == 0, outcome.stderr_tail
    assert outcome.scoped
    low, high = PEAK_BAND
    assert low <= outcome.memory_peak_bytes <= high, outcome.memory_peak_bytes / MIB


def test_cpu_canary_load_near_one_core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = _run(tmp_path, monkeypatch, _script(tmp_path, "cpu.py", CPU_CANARY))
    assert outcome.exit_code == 0, outcome.stderr_tail
    low, high = CPU_BAND
    assert low <= outcome.cpu_load <= high, outcome.cpu_load


def test_scope_removed_after_measurement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = {p.name for p in Path(ROOT).iterdir() if p.is_dir()}
    _run(tmp_path, monkeypatch, _script(tmp_path, "noop.py", "pass"))
    after = {p.name for p in Path(ROOT).iterdir() if p.is_dir()}
    assert after == before


def test_canary_values_reach_record_event_and_gauge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    store, bus = FakeStore(entrypoint=_script(tmp_path, "mem.py", MEMORY_CANARY)), FakeBus()
    metrics = ExecutorMetrics(CollectorRegistry())

    async def scoped_run(**kwargs):
        return await run_invocation(**kwargs, root=ROOT)

    executor = Executor(
        by_key=store.by_key,
        entrypoint=store.get_entrypoint,
        record=store.record,
        publish=bus.publish,
        run=scoped_run,
        metrics=metrics,
    )
    run(executor.handle_message(FakeMsg(make_command().model_dump_json().encode())))
    rec = store.records[0]
    low, high = PEAK_BAND
    assert rec.result == "success"
    assert low <= rec.memory_peak_bytes <= high
    assert bus.events[1].data.metrics.memory_peak_bytes == rec.memory_peak_bytes
    gauge = metrics.registry.get_sample_value(
        MEMORY_PEAK_METRIC, {"candybar_id": str(CANDYBAR_ID), "result": "success"}
    )
    assert gauge == rec.memory_peak_bytes
