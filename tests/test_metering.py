"""Story 3.1: per-invocation memory peak + average CPU load from the cgroup scope.

Covers the spec's I/O matrix with plain-filesystem cgroup doubles (the child
writes the scope's `memory.peak` / `cpu.stat` itself, standing in for the
kernel). The real-cgroup canary lives in test_cgroup_metering_integration.py.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from prometheus_client import CollectorRegistry
from prometheus_client.openmetrics.exposition import generate_latest as om_generate
from prometheus_client.openmetrics.parser import text_string_to_metric_families

from candybar.events import build_candybar_failed, parse_envelope
from candybar.events.envelope import (
    CandybarCompletedEvent,
    CandybarFailedEvent,
    InvocationMetrics,
    SelfReportedMetrics,
)
from candybar.executor import metering
from candybar.executor.metrics import (
    CPU_LOAD_METRIC,
    EXEMPLAR_LABEL,
    MEMORY_METRIC,
    MEMORY_PEAK_METRIC,
    ExecutorMetrics,
)
from candybar.executor.runner import ResultFile, run_invocation
from candybar.executor.worker import Executor, record_from_row, terminal_event_for
from candybar.registry.store import TerminalInvocation
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
SCHEMAS = REPO / "schemas" / "cron"
MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def _reset_metering_caches() -> None:
    metering._enabled_roots.clear()
    metering._warned.clear()


# ---------------------------------------------------------------------------
# metering.py: pure readers
# ---------------------------------------------------------------------------


def test_read_memory_peak(tmp_path: Path) -> None:
    (tmp_path / "memory.peak").write_text("67108864\n")
    assert metering.read_memory_peak(tmp_path) == 64 * MIB


def test_memory_peak_unmeasurable_is_zero(tmp_path: Path) -> None:
    assert metering.read_memory_peak(None) == 0
    (tmp_path / "memory.peak").write_text("max\n")  # garbage, not a number
    assert metering.read_memory_peak(tmp_path) == 0


def test_missing_memory_peak_warns_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    scope_a, scope_b = tmp_path / "a", tmp_path / "b"
    scope_a.mkdir()
    scope_b.mkdir()
    with caplog.at_level(logging.WARNING, logger=metering.__name__):
        assert metering.read_memory_peak(scope_a) == 0
        assert metering.read_memory_peak(scope_b) == 0
    warnings = [r for r in caplog.records if "memory.peak" in r.getMessage()]
    assert len(warnings) == 1  # once per process (per root), not per invocation


def test_read_cpu_usage_usec(tmp_path: Path) -> None:
    (tmp_path / "cpu.stat").write_text(
        "usage_usec 1234567\nuser_usec 1000000\nsystem_usec 234567\nnr_periods 0\n"
    )
    assert metering.read_cpu_usage_usec(tmp_path) == 1234567


@pytest.mark.parametrize("content", [None, "user_usec 5\n", "usage_usec lots\n"])
def test_cpu_usage_unmeasurable_is_zero(tmp_path: Path, content: str | None) -> None:
    if content is not None:
        (tmp_path / "cpu.stat").write_text(content)
    assert metering.read_cpu_usage_usec(tmp_path) == 0
    assert metering.read_cpu_usage_usec(None) == 0


@pytest.mark.parametrize(
    ("delta_usec", "runtime_ms", "expected"),
    [
        (500_000, 1000, 0.5),  # half a core for one second
        (2_000_000, 1000, 2.0),  # two cores saturated
        (1_000, 0, 0.0),  # 0 ms run: no division by zero
        (-5, 100, 0.0),  # counter went backwards (never trust it)
        (0, 100, 0.0),
    ],
)
def test_cpu_load(delta_usec: int, runtime_ms: int, expected: float) -> None:
    assert metering.cpu_load(delta_usec, runtime_ms) == expected


def test_enable_controllers_writes_each_controller_once(tmp_path: Path) -> None:
    metering.enable_controllers(tmp_path)
    metering.enable_controllers(tmp_path)  # cached: no second write
    assert (tmp_path / "cgroup.subtree_control").read_text() == "+memory+cpu"


def test_enable_controllers_fails_soft(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    missing = tmp_path / "nope" / "deeper"  # parent dir absent: open() raises
    with caplog.at_level(logging.WARNING, logger=metering.__name__):
        metering.enable_controllers(missing)
    assert any("cannot delegate" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# runner: scope measured after exit (plain-fs double stands in for the kernel)
# ---------------------------------------------------------------------------

# The child writes its own scope's stat files. It sleeps first so the runner's
# post-spawn scope entry + cpu baseline read happen before the "kernel" counts.
SCOPE_WRITER = """sleep 0.3
scope="$CG_ROOT/$CANDYBAR_INVOCATION_ID"
printf '%s\\n' "$PEAK" > "$scope/memory.peak"
printf 'usage_usec %s\\nuser_usec 1\\nsystem_usec 1\\n' "$USAGE" > "$scope/cpu.stat"
exit "$CODE"
"""


def _scoped_run(tmp_path: Path, monkeypatch: Any, *, peak: int, usage: int, code: int = 0):
    root = tmp_path / "cg"
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("CG_ROOT", str(root))
    monkeypatch.setenv("PEAK", str(peak))
    monkeypatch.setenv("USAGE", str(usage))
    monkeypatch.setenv("CODE", str(code))
    entrypoint = make_script(tmp_path, "scoped-meter.sh", SCOPE_WRITER)
    return run(run_invocation(**invoke_kwargs(entrypoint=entrypoint, root=str(root))))


def test_scoped_success_reports_peak_and_cpu_load(tmp_path: Path, monkeypatch: Any) -> None:
    outcome = _scoped_run(tmp_path, monkeypatch, peak=64 * MIB, usage=150_000)
    assert outcome.scoped and outcome.exit_code == 0
    assert outcome.memory_peak_bytes == 64 * MIB
    assert outcome.runtime_ms > 0
    assert outcome.cpu_load == metering.cpu_load(150_000, outcome.runtime_ms)
    assert outcome.cpu_load > 0


def test_scoped_failure_still_measured(tmp_path: Path, monkeypatch: Any) -> None:
    outcome = _scoped_run(tmp_path, monkeypatch, peak=3 * MIB, usage=9_000, code=3)
    assert outcome.exit_code == 3
    assert outcome.memory_peak_bytes == 3 * MIB and outcome.cpu_load > 0


def test_unscoped_run_meters_zero(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    entrypoint = make_script(tmp_path, "plain.sh", "exit 0")
    outcome = run(run_invocation(**invoke_kwargs(entrypoint=entrypoint, root="")))
    assert not outcome.scoped
    assert outcome.memory_peak_bytes == 0 and outcome.cpu_load == 0.0


def test_spawn_failure_meters_zero(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    outcome = run(
        run_invocation(**invoke_kwargs(entrypoint=str(tmp_path / "missing"), root=str(tmp_path)))
    )
    assert outcome.spawn_error is not None
    assert outcome.memory_peak_bytes == 0 and outcome.cpu_load == 0.0


def test_child_cannot_self_report_measures(tmp_path: Path, monkeypatch: Any) -> None:
    """memory/CPU are executor-measured: a result file claiming them is invalid."""
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    body = json.dumps({"status": "success", "metrics": {"memory_peak_bytes": 1}})
    entrypoint = make_script(tmp_path, "liar.sh", f"echo '{body}' > \"$CANDYBAR_RESULT_FILE\"")
    outcome = run(run_invocation(**invoke_kwargs(entrypoint=entrypoint)))
    assert outcome.result is None and outcome.result_error is not None
    assert ResultFile.model_fields["metrics"].annotation is SelfReportedMetrics


# ---------------------------------------------------------------------------
# worker: measures land on row, event, and metrics — and survive republish
# ---------------------------------------------------------------------------


def _executor(store: FakeStore, bus: FakeBus, outcome: Any) -> tuple[Executor, ExecutorMetrics]:
    metrics = ExecutorMetrics(CollectorRegistry())

    async def fake_run(**_kwargs: Any):
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


def test_success_measures_on_row_event_and_series() -> None:
    store, bus = FakeStore(), FakeBus()
    outcome = make_outcome(runtime_ms=800, memory_peak_bytes=96 * MIB, cpu_load=0.75)
    executor, m = _executor(store, bus, outcome)
    run(executor.handle_message(FakeMsg(make_command().model_dump_json().encode())))

    rec = store.records[0]
    assert rec.memory_peak_bytes == 96 * MIB and rec.cpu_load == 0.75
    completed = bus.events[1]
    assert isinstance(completed, CandybarCompletedEvent)
    assert completed.data.metrics.memory_peak_bytes == 96 * MIB
    assert completed.data.metrics.cpu_load == 0.75
    assert _sample(m, MEMORY_PEAK_METRIC, "success") == 96 * MIB
    assert _sample(m, f"{MEMORY_METRIC}_sum", "success") == 96 * MIB
    assert _sample(m, f"{CPU_LOAD_METRIC}_sum", "success") == 0.75
    assert _sample(m, f"{CPU_LOAD_METRIC}_count", "success") == 1
    # The fail series of every new family materializes at zero.
    assert _sample(m, MEMORY_PEAK_METRIC, "fail") == 0
    assert _sample(m, f"{MEMORY_METRIC}_count", "fail") == 0
    assert _sample(m, f"{CPU_LOAD_METRIC}_count", "fail") == 0


def test_failure_measures_on_failed_event_and_fail_series() -> None:
    store, bus = FakeStore(), FakeBus()
    outcome = make_outcome(exit_code=2, memory_peak_bytes=5 * MIB, cpu_load=1.25)
    executor, m = _executor(store, bus, outcome)
    run(executor.handle_message(FakeMsg(make_command().model_dump_json().encode())))

    failed = bus.events[1]
    assert isinstance(failed, CandybarFailedEvent)
    assert failed.data.metrics.memory_peak_bytes == 5 * MIB
    assert failed.data.metrics.cpu_load == 1.25
    assert store.records[0].result == "fail" and store.records[0].cpu_load == 1.25
    assert _sample(m, MEMORY_PEAK_METRIC, "fail") == 5 * MIB
    assert _sample(m, f"{CPU_LOAD_METRIC}_sum", "fail") == 1.25


def test_unmeasured_run_observed_as_zero() -> None:
    store, bus = FakeStore(), FakeBus()
    executor, m = _executor(store, bus, make_outcome())  # defaults: unscoped
    run(executor.handle_message(FakeMsg(make_command().model_dump_json().encode())))
    assert bus.events[1].data.metrics.memory_peak_bytes == 0
    assert _sample(m, MEMORY_PEAK_METRIC, "success") == 0
    assert _sample(m, f"{MEMORY_METRIC}_count", "success") == 1  # observed, as zero
    assert _sample(m, f"{CPU_LOAD_METRIC}_sum", "success") == 0


def test_redelivery_republishes_stored_measures_without_recounting() -> None:
    store, bus = FakeStore(), FakeBus()
    cmd = make_command()
    store.rows[cmd.idempotency_key] = make_row(
        cmd.idempotency_key, memory_peak_bytes=7 * MIB, cpu_load=0.5
    )
    executor, m = _executor(store, bus, make_outcome())
    run(executor.handle_message(FakeMsg(cmd.model_dump_json().encode())))
    (republished,) = bus.events
    assert republished.data.metrics.memory_peak_bytes == 7 * MIB
    assert republished.data.metrics.cpu_load == 0.5
    assert _sample(m, MEMORY_PEAK_METRIC, "success") is None  # never observed


def test_republished_failed_event_carries_stored_measures() -> None:
    row = make_row("k", result="fail", memory_peak_bytes=2 * MIB, cpu_load=0.1)
    event = terminal_event_for(make_command(), record_from_row(row))
    assert isinstance(event, CandybarFailedEvent)
    assert event.data.metrics.memory_peak_bytes == 2 * MIB


def test_pre_migration_row_reads_as_unmeasured() -> None:
    """A row built without the 0002 columns (None on the ORM object) republishes as 0."""
    rec = record_from_row(make_row("k"))
    assert rec.memory_peak_bytes == 0 and rec.cpu_load == 0.0


# ---------------------------------------------------------------------------
# metrics exposition: exemplars on the histogram families
# ---------------------------------------------------------------------------


def test_memory_and_cpu_histograms_carry_invocation_exemplars() -> None:
    m = ExecutorMetrics(CollectorRegistry())
    a, b = uuid4(), uuid4()
    m.observe(
        candybar_id=CANDYBAR_ID, result="success", invocation_id=a, runtime_ms=10,
        memory_peak_bytes=40 * MIB, cpu_load=0.3,
    )
    m.observe(
        candybar_id=CANDYBAR_ID, result="fail", invocation_id=b, runtime_ms=10,
        memory_peak_bytes=600 * MIB, cpu_load=3.0,
    )
    fams = {f.name: f for f in text_string_to_metric_families(om_generate(m.registry).decode())}
    for family, values in ((MEMORY_METRIC, (40 * MIB, 600 * MIB)), (CPU_LOAD_METRIC, (0.3, 3.0))):
        buckets = [s for s in fams[family].samples if s.name.endswith("_bucket") and s.exemplar]
        by_id = {s.exemplar.labels[EXEMPLAR_LABEL]: s.exemplar.value for s in buckets}
        assert by_id == {str(a): values[0], str(b): values[1]}, family
    # The gauge holds the latest peak per series and (by format) no exemplar.
    gauge = {s.labels["result"]: s for s in fams[MEMORY_PEAK_METRIC].samples}
    assert gauge["success"].value == 40 * MIB and gauge["fail"].value == 600 * MIB
    assert all(s.exemplar is None for s in gauge.values())


def test_memory_gauge_tracks_latest_invocation() -> None:
    m = ExecutorMetrics(CollectorRegistry())
    for peak in (10 * MIB, 90 * MIB, 20 * MIB):
        m.observe(
            candybar_id=CANDYBAR_ID, result="success", invocation_id=uuid4(), runtime_ms=1,
            memory_peak_bytes=peak,
        )
    labels = {"candybar_id": str(CANDYBAR_ID), "result": "success"}
    assert m.registry.get_sample_value(MEMORY_PEAK_METRIC, labels) == 20 * MIB
    assert m.registry.get_sample_value(f"{MEMORY_METRIC}_count", labels) == 3


# ---------------------------------------------------------------------------
# events: payloads stay inside the (additionalProperties:false) schemas
# ---------------------------------------------------------------------------


def _data_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / name).read_text())["properties"]["data"]


def _assert_within(payload: dict[str, Any], schema: dict[str, Any]) -> None:
    props = schema["properties"]
    assert set(payload) <= set(props), set(payload) - set(props)
    assert set(schema.get("required", [])) <= set(payload)
    for key, value in payload.items():
        if isinstance(value, dict) and props[key].get("type") == "object":
            _assert_within(value, props[key])


def _rec(result: str) -> TerminalInvocation:
    row = make_row("k", result=result, memory_peak_bytes=MIB, cpu_load=0.2, token_spend=3)
    return record_from_row(row)


@pytest.mark.parametrize(
    ("result", "schema"), [("success", "candybar.completed.json"), ("fail", "candybar.failed.json")]
)
def test_terminal_payloads_fit_draft_schemas(result: str, schema: str) -> None:
    event = terminal_event_for(make_command(), _rec(result))
    data = event.model_dump(mode="json")["data"]
    _assert_within(data, _data_schema(schema))
    assert data["metrics"] == {
        "token_spend": 3,
        "cost_usd": None,
        "memory_peak_bytes": MIB,
        "cpu_load": 0.2,
    }


def test_measured_fields_are_optional_in_schema_revision_1() -> None:
    """Additive within v1: already-emitted events (no measures) stay valid."""
    completed = _data_schema("candybar.completed.json")
    assert "memory_peak_bytes" not in completed["properties"]["metrics"]["required"]
    assert "metrics" not in _data_schema("candybar.failed.json")["required"]


def test_failed_event_round_trips_and_legacy_payload_parses() -> None:
    event = build_candybar_failed(
        candybar_id=CANDYBAR_ID, name="unit-job", invocation_id=INVOCATION_ID,
        trigger="manual", tick_id=None, exit_code=1, runtime_ms=5,
        metrics=InvocationMetrics(memory_peak_bytes=MIB, cpu_load=0.9),
    )
    raw = event.model_dump(mode="json")
    assert parse_envelope(raw) == event
    del raw["data"]["metrics"]  # a pre-3.1 failed event
    legacy = parse_envelope(raw)
    assert legacy.data.metrics == InvocationMetrics()
