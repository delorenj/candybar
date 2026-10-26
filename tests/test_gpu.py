"""Story 3.2: average GPU load per invocation (nvidia-smi pmon, or zero).

Covers the spec's I/O matrix at three levels:

- the pmon parser and attribution, white-box, with a fake /proc;
- run_invocation against a fake `nvidia-smi` (a Python script emitting pmon
  blocks for the PIDs that child jobs announce), which proves the shared
  sampler, concurrency, and the overhead budget on any host;
- the real `nvidia-smi` plus a CUDA burner, behind CANDYBAR_TEST_GPU=1
  (big-chungus's RTX 3090).
"""

from __future__ import annotations

import asyncio
import logging
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from prometheus_client import CollectorRegistry
from prometheus_client.openmetrics.exposition import generate_latest as om_generate
from prometheus_client.openmetrics.parser import text_string_to_metric_families

from candybar.events.envelope import (
    CandybarCompletedEvent,
    CandybarFailedEvent,
    InvocationMetrics,
)
from candybar.executor import gpu as gpu_mod
from candybar.executor.gpu import (
    GPU_SAMPLING_BUDGET_MS,
    GpuSampler,
    GpuSession,
    _PmonRun,
    parse_sid,
    resolve_nvidia_smi,
)
from candybar.executor.metrics import EXEMPLAR_LABEL, GPU_LOAD_METRIC, ExecutorMetrics
from candybar.executor.runner import RunOutcome, run_invocation
from candybar.executor.worker import Executor, record_from_row, terminal_event_for
from test_executor import (
    CANDYBAR_ID,
    INVOCATION_ID,
    FakeBus,
    FakeMsg,
    FakeStore,
    invoke_kwargs,
    make_command,
    make_outcome,
    make_row,
    make_script,
    run,
)

REPO = Path(__file__).resolve().parents[1]
GPU_BURN = REPO / "scripts" / "gpu-burn-candybar.py"
HEADER = "#Time         gpu         pid   type     sm    mem    enc    dec    jpg    ofa    command"
UNITS = "#HH:MM:SS     Idx           #    C/G      %      %      %      %      %      %    name"
LEADER = 4242  # the child: session leader of the invocation's process tree


def line(t: str, pid: int | str, sm: int | str, gpu: int = 0) -> str:
    return f" {t}       {gpu}    {pid}     C     {sm}      1      -      -      -      -    job"


def fake_proc(tmp_path: Path, sids: dict[int, int]) -> Path:
    """A /proc double: <pid>/stat with the given session id (comm holds ') ')."""
    root = tmp_path / "proc"
    for pid, sid in sids.items():
        (root / str(pid)).mkdir(parents=True)
        (root / str(pid) / "stat").write_text(f"{pid} (evil) name) S 1 {pid} {sid} 0 -1 4194560\n")
    return root


def attach(sampler: GpuSampler, pid: int = LEADER, scope: Path | None = None) -> GpuSession:
    """Register a session without spawning pmon (white-box parser tests)."""
    session = GpuSession(pid, scope, sampler)
    sampler._sessions[session] = None
    return session


def feed(sampler: GpuSampler, *lines: str) -> _PmonRun:
    pmon_run = _PmonRun()
    for raw in (HEADER, UNITS, *lines):
        sampler._feed(pmon_run, raw + "\n")
    return pmon_run


# ---------------------------------------------------------------------------
# Parser + attribution (matrix rows: GPU job, multi-PID/GPU, foreign, '-', none)
# ---------------------------------------------------------------------------


def test_parse_sid_survives_parens_in_comm() -> None:
    assert parse_sid("77 (a) b) S 1 77 4242 0 -1 0\n") == 4242
    assert parse_sid("garbage") is None


def test_gpu_job_averages_member_sm_over_samples(tmp_path: Path) -> None:
    sampler = GpuSampler("unused", proc_root=fake_proc(tmp_path, {101: LEADER}))
    session = attach(sampler)
    feed(sampler, line("10:00:01", 101, 40), line("10:00:02", 101, 60))
    assert session.finish() == 0.5  # (0.4 + 0.6) / 2 samples


def test_multi_pid_and_multi_gpu_sum_within_one_sample(tmp_path: Path) -> None:
    sampler = GpuSampler("unused", proc_root=fake_proc(tmp_path, {101: LEADER, 102: LEADER}))
    session = attach(sampler)
    feed(sampler, line("10:00:01", 101, 50), line("10:00:01", 102, 50))
    assert session.finish() == 1.0
    sampler = GpuSampler("unused", proc_root=fake_proc(tmp_path / "b", {101: LEADER}))
    session = attach(sampler)
    feed(sampler, line("10:00:01", 101, 50, gpu=0), line("10:00:01", 101, 50, gpu=1))
    assert session.finish() == 1.0  # one process on two GPUs = one busy GPU


def test_foreign_pid_is_not_attributed(tmp_path: Path) -> None:
    sampler = GpuSampler("unused", proc_root=fake_proc(tmp_path, {101: LEADER, 900: 1}))
    session = attach(sampler)
    feed(sampler, line("10:00:01", 900, 95), line("10:00:01", 101, 10), line("10:00:02", 900, 95))
    assert session.finish() == 0.05  # (0.10 + 0) / 2; the desktop's 95% never counts


def test_scope_membership_catches_a_setsid_escapee(tmp_path: Path) -> None:
    scope = tmp_path / "scope"
    scope.mkdir()
    (scope / "cgroup.procs").write_text(f"{LEADER}\n555\n")
    sampler = GpuSampler("unused", proc_root=fake_proc(tmp_path, {555: 555}))  # own session
    session = attach(sampler, scope=scope)
    feed(sampler, line("10:00:01", 555, 30))
    assert session.finish() == 0.3


def test_dash_and_empty_samples_count_as_zero(tmp_path: Path) -> None:
    sampler = GpuSampler("unused", proc_root=fake_proc(tmp_path, {101: LEADER}))
    session = attach(sampler)
    feed(
        sampler,
        line("10:00:01", 101, 100),
        line("10:00:02", 101, "-"),  # process present, no SM activity
        "    10:00:03       0          -     -      -      -      -      -      -      -    -",
    )
    assert session.finish() == round(1 / 3, 4)


def test_repeated_pid_within_one_second_splits_samples(tmp_path: Path) -> None:
    sampler = GpuSampler("unused", proc_root=fake_proc(tmp_path, {101: LEADER}))
    session = attach(sampler)
    feed(sampler, line("10:00:01", 101, 80), line("10:00:01", 101, 20))  # cadence drift
    assert session.finish() == 0.5  # two samples, not one at 1.0


def test_no_samples_is_zero(tmp_path: Path) -> None:
    sampler = GpuSampler("unused", proc_root=fake_proc(tmp_path, {}))
    assert attach(sampler).finish() == 0.0
    noop = GpuSession(LEADER, None, None)
    assert noop.finish() == 0.0


def test_lines_before_a_header_are_ignored(tmp_path: Path) -> None:
    sampler = GpuSampler("unused", proc_root=fake_proc(tmp_path, {101: LEADER}))
    session = attach(sampler)
    sampler._feed(_PmonRun(), line("10:00:01", 101, 90))  # no header seen yet
    assert session.finish() == 0.0


# ---------------------------------------------------------------------------
# GPU-less host (matrix row): pmon never spawned, 0, silent
# ---------------------------------------------------------------------------


def test_resolve_nvidia_smi(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv(gpu_mod.NVIDIA_SMI_ENV, "")
    assert resolve_nvidia_smi() is None
    monkeypatch.setenv(gpu_mod.NVIDIA_SMI_ENV, str(tmp_path / "absent"))
    assert resolve_nvidia_smi() is None
    monkeypatch.delenv(gpu_mod.NVIDIA_SMI_ENV)
    monkeypatch.setenv("PATH", str(tmp_path))  # a host without nvidia-smi
    assert resolve_nvidia_smi() is None


def test_gpu_less_host_meters_zero_silently(
    tmp_path: Path, monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    sampler = GpuSampler(None)
    entrypoint = make_script(tmp_path, "cpu.sh", "sleep 0.1")
    with caplog.at_level(logging.WARNING):
        outcome = run(run_invocation(**invoke_kwargs(entrypoint=entrypoint, gpu=sampler)))
    assert outcome.exit_code == 0 and outcome.gpu_load == 0.0
    assert sampler.pmon_starts == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_default_sampler_is_off_in_the_unit_suite(tmp_path: Path, monkeypatch: Any) -> None:
    """conftest pins CANDYBAR_NVIDIA_SMI="" so GPU hosts stay deterministic."""
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    outcome = run(run_invocation(**invoke_kwargs(entrypoint="/bin/true")))
    assert outcome.gpu_load == 0.0
    assert gpu_mod.GPU_SAMPLER.pmon_starts == 0


# ---------------------------------------------------------------------------
# Fake nvidia-smi: a real subprocess emitting pmon blocks every 0.1 s
# ---------------------------------------------------------------------------

FAKE_PMON = f"""#!{sys.executable}
import os, pathlib, sys, time
d = pathlib.Path(os.environ["FAKE_PMON_DIR"])
(d / "starts").open("a").write("x")
print({HEADER!r}, flush=True)
print({UNITS!r}, flush=True)
n = 0
while True:
    n += 1
    rows = []
    for f in sorted(d.glob("*.job")):
        parts = f.read_text().split()
        if len(parts) == 2:
            rows.append(f" t{{n}} 0 {{parts[0]}} C {{parts[1]}} 1 - - - - job")
    rows.append(f" t{{n}} 0 1 C 90 1 - - - - foreign")  # never a member (pid 1)
    print("\\n".join(rows), flush=True)
    time.sleep(0.1)
"""

# A job announcing its own pid + SM% to the fake pmon, then staying busy.
GPU_JOB = 'echo "$$ $SM" > "$FAKE_PMON_DIR/$CANDYBAR_INVOCATION_ID.job"\nsleep "${HOLD:-1}"\n'


@pytest.fixture
def fake_smi(tmp_path: Path, monkeypatch: Any) -> tuple[str, Path]:
    pmon_dir = tmp_path / "pmon"
    pmon_dir.mkdir()
    monkeypatch.setenv("FAKE_PMON_DIR", str(pmon_dir))
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    binary = tmp_path / "nvidia-smi"
    binary.write_text(FAKE_PMON)
    binary.chmod(0o755)
    return str(binary), pmon_dir


def _job(tmp_path: Path, name: str, sm: int, hold: float = 1.0) -> str:
    return make_script(tmp_path, name, f"SM={sm}\nHOLD={hold}\n{GPU_JOB}")


def test_fake_pmon_attributes_the_jobs_tree(fake_smi: tuple[str, Path], tmp_path: Path) -> None:
    binary, _ = fake_smi
    sampler = GpuSampler(binary)

    async def go() -> RunOutcome:
        outcome = await run_invocation(**invoke_kwargs(entrypoint=_job(tmp_path, "g.sh", 50), gpu=sampler))
        await sampler.aclose()
        return outcome

    outcome = run(go())
    # ~10 samples at 0.5; the first block can precede the job's announcement.
    assert 0.35 <= outcome.gpu_load <= 0.5, outcome.gpu_load
    assert sampler._run is None  # pmon stopped with the last session


def test_concurrent_invocations_share_one_pmon(fake_smi: tuple[str, Path], tmp_path: Path) -> None:
    from uuid import uuid4

    binary, pmon_dir = fake_smi
    sampler = GpuSampler(binary)

    async def go() -> tuple[RunOutcome, RunOutcome]:
        a, b = await asyncio.gather(
            run_invocation(**invoke_kwargs(entrypoint=_job(tmp_path, "a.sh", 30), gpu=sampler, invocation_id=uuid4())),
            run_invocation(**invoke_kwargs(entrypoint=_job(tmp_path, "b.sh", 80), gpu=sampler, invocation_id=uuid4())),
        )
        await sampler.aclose()
        return a, b

    a, b = run(go())
    assert sampler.pmon_starts == 1
    assert (pmon_dir / "starts").read_text() == "x"  # one pmon process, ever
    assert 0.2 <= a.gpu_load <= 0.3, a.gpu_load
    assert 0.55 <= b.gpu_load <= 0.8, b.gpu_load


def test_pmon_failure_disables_with_one_warning(
    tmp_path: Path, monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    broken = make_script(tmp_path, "nvidia-smi", 'echo "NVIDIA-SMI has failed: no driver" >&2\nexit 9')
    sampler = GpuSampler(broken)
    entrypoint = make_script(tmp_path, "job.sh", "sleep 0.5")

    async def go() -> list[RunOutcome]:
        outcomes = [await run_invocation(**invoke_kwargs(entrypoint=entrypoint, gpu=sampler)) for _ in range(3)]
        await sampler.aclose()
        return outcomes

    with caplog.at_level(logging.WARNING, logger=gpu_mod.__name__):
        outcomes = run(go())
    assert [o.gpu_load for o in outcomes] == [0.0, 0.0, 0.0]
    assert all(o.exit_code == 0 for o in outcomes)  # never fails the invocation
    assert sampler.disabled and sampler.pmon_starts == 1
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "disabled" in warnings[0].getMessage()


def test_pmon_dying_mid_run_warns_once_and_restarts(
    tmp_path: Path, monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A pmon that reports samples and then dies (driver reset, OOM kill)
    is restarted by the next invocation, and warns once per process only."""
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    flaky = make_script(tmp_path, "nvidia-smi", f"echo '{HEADER}'\necho '{line('t1', 1, 5)}'\nexit 1")
    sampler = GpuSampler(flaky)
    entrypoint = make_script(tmp_path, "job.sh", "sleep 0.3")

    async def go() -> list[RunOutcome]:
        outcomes = [await run_invocation(**invoke_kwargs(entrypoint=entrypoint, gpu=sampler)) for _ in range(3)]
        await sampler.aclose()
        return outcomes

    with caplog.at_level(logging.WARNING, logger=gpu_mod.__name__):
        outcomes = run(go())
    assert [o.gpu_load for o in outcomes] == [0.0, 0.0, 0.0]
    assert not sampler.disabled and sampler.pmon_starts == 3  # restarted each time
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


def test_unspawnable_binary_meters_zero(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    sampler = GpuSampler(str(tmp_path / "gone"))  # resolved, then deleted

    async def go() -> RunOutcome:
        outcome = await run_invocation(**invoke_kwargs(entrypoint=make_script(tmp_path, "j.sh", "sleep 0.2"), gpu=sampler))
        await sampler.aclose()
        return outcome

    assert run(go()).gpu_load == 0.0
    assert sampler.disabled


# ---------------------------------------------------------------------------
# Overhead budget (AC): sampling adds < GPU_SAMPLING_BUDGET_MS per invocation
# ---------------------------------------------------------------------------


def _median_runtime_ms(entrypoint: str, sampler: GpuSampler, n: int = 7) -> float:
    async def go() -> list[float]:
        walls = []
        for _ in range(n):
            t0 = time.perf_counter()
            await run_invocation(**invoke_kwargs(entrypoint=entrypoint, gpu=sampler))
            walls.append((time.perf_counter() - t0) * 1000)
        await sampler.aclose()
        return walls

    return statistics.median(run(go()))


def test_sampling_overhead_within_budget_fake_pmon(fake_smi: tuple[str, Path], tmp_path: Path) -> None:
    binary, _ = fake_smi
    entrypoint = make_script(tmp_path, "quick.sh", "sleep 0.2")
    baseline = _median_runtime_ms(entrypoint, GpuSampler(None))
    sampled = _median_runtime_ms(entrypoint, GpuSampler(binary))
    assert sampled - baseline < GPU_SAMPLING_BUDGET_MS, (baseline, sampled)


def test_track_and_finish_are_cheap(fake_smi: tuple[str, Path]) -> None:
    binary, _ = fake_smi
    sampler = GpuSampler(binary)

    async def go() -> float:
        t0 = time.perf_counter()
        for _ in range(100):
            sampler.track(os.getpid()).finish()
        elapsed = (time.perf_counter() - t0) * 1000 / 100
        await sampler.aclose()
        return elapsed

    assert run(go()) < GPU_SAMPLING_BUDGET_MS / 10


# ---------------------------------------------------------------------------
# Worker: gpu_load on row, event, histogram (+exemplar); republish from row
# ---------------------------------------------------------------------------


def _executor(store: FakeStore, bus: FakeBus, outcome: RunOutcome) -> tuple[Executor, ExecutorMetrics]:
    metrics = ExecutorMetrics(CollectorRegistry())

    async def fake_run(**_kwargs: Any) -> RunOutcome:
        return outcome

    executor = Executor(
        by_key=store.by_key,
        entrypoint=store.get_entrypoint,
        record=store.record,
        publish=bus.publish,
        run=fake_run,
        metrics=metrics,
    )
    return executor, metrics


def _sample(m: ExecutorMetrics, name: str, result: str) -> float | None:
    return m.registry.get_sample_value(name, {"candybar_id": str(CANDYBAR_ID), "result": result})


def test_gpu_load_lands_on_row_event_and_histogram() -> None:
    store, bus = FakeStore(), FakeBus()
    executor, m = _executor(store, bus, make_outcome(gpu_load=0.5))
    run(executor.handle_message(FakeMsg(make_command().model_dump_json().encode())))
    assert store.records[0].gpu_load == 0.5
    completed = bus.events[1]
    assert isinstance(completed, CandybarCompletedEvent)
    assert completed.data.metrics.gpu_load == 0.5
    assert _sample(m, f"{GPU_LOAD_METRIC}_sum", "success") == 0.5
    assert _sample(m, f"{GPU_LOAD_METRIC}_count", "fail") == 0  # zero-materialized

    families = text_string_to_metric_families(om_generate(m.registry).decode())
    (family,) = [f for f in families if f.name == GPU_LOAD_METRIC]
    exemplars = [s.exemplar for s in family.samples if s.exemplar is not None]
    assert exemplars and exemplars[0].labels == {EXEMPLAR_LABEL: str(INVOCATION_ID)}
    assert {frozenset(s.labels) - {"le"} for s in family.samples} == {frozenset({"candybar_id", "result"})}


def test_gpu_load_on_failed_event() -> None:
    store, bus = FakeStore(), FakeBus()
    executor, m = _executor(store, bus, make_outcome(exit_code=1, gpu_load=1.25))
    run(executor.handle_message(FakeMsg(make_command().model_dump_json().encode())))
    failed = bus.events[1]
    assert isinstance(failed, CandybarFailedEvent) and failed.data.metrics.gpu_load == 1.25
    assert _sample(m, f"{GPU_LOAD_METRIC}_sum", "fail") == 1.25


def test_redelivery_republishes_stored_gpu_load_without_recounting() -> None:
    store, bus = FakeStore(), FakeBus()
    cmd = make_command()
    store.rows[cmd.idempotency_key] = make_row(cmd.idempotency_key, gpu_load=0.75)
    executor, m = _executor(store, bus, make_outcome())
    run(executor.handle_message(FakeMsg(cmd.model_dump_json().encode())))
    (republished,) = bus.events
    assert republished.data.metrics.gpu_load == 0.75
    assert _sample(m, f"{GPU_LOAD_METRIC}_count", "success") is None  # never observed
    assert record_from_row(store.rows[cmd.idempotency_key]).gpu_load == 0.75


def test_child_cannot_self_report_gpu_load() -> None:
    from candybar.events.envelope import SelfReportedMetrics

    assert "gpu_load" not in SelfReportedMetrics.model_fields
    assert InvocationMetrics.model_fields["gpu_load"].default == 0.0
    rec = record_from_row(make_row("k"))
    assert terminal_event_for(make_command(), rec).data.metrics.gpu_load == 0.0


# ---------------------------------------------------------------------------
# Real nvidia-smi + CUDA burner (CANDYBAR_TEST_GPU=1, NVIDIA host)
# ---------------------------------------------------------------------------

real_gpu = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_GPU") != "1", reason="set CANDYBAR_TEST_GPU=1 on an NVIDIA host"
)


@real_gpu
def test_real_pmon_attributes_a_cuda_burner(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("GPU_BURN_SECONDS", "4")
    sampler = GpuSampler("nvidia-smi")

    async def go() -> RunOutcome:
        outcome = await run_invocation(**invoke_kwargs(entrypoint=str(GPU_BURN), gpu=sampler))
        await sampler.aclose()
        return outcome

    outcome = run(go())
    assert outcome.exit_code == 0, outcome.stderr_tail
    assert outcome.gpu_load > 0.3, outcome.gpu_load


@real_gpu
def test_real_pmon_never_attributes_other_gpu_processes(tmp_path: Path, monkeypatch: Any) -> None:
    """The desktop/other services keep the GPU busy; a CPU job must read 0."""
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    sampler = GpuSampler("nvidia-smi")

    async def go() -> RunOutcome:
        outcome = await run_invocation(**invoke_kwargs(entrypoint=make_script(tmp_path, "cpu.sh", "sleep 2.5"), gpu=sampler))
        await sampler.aclose()
        return outcome

    assert run(go()).gpu_load == 0.0


@real_gpu
def test_real_pmon_overhead_within_budget(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    entrypoint = make_script(tmp_path, "quick.sh", "sleep 0.2")
    baseline = _median_runtime_ms(entrypoint, GpuSampler(None))
    sampled = _median_runtime_ms(entrypoint, GpuSampler("nvidia-smi"))
    assert sampled - baseline < GPU_SAMPLING_BUDGET_MS, (baseline, sampled)
