"""Story 1.7: per-candybar Prometheus series, invocation_id exemplars, /metrics.

Covers the spec's I/O matrix: recorder semantics, the cardinality guard (AD-9:
series labeled {candybar_id, result} only, never per invocation), exemplar
presence through a real OpenMetrics parse, once-per-execution recording in the
executor worker, /metrics content negotiation, and drift between the
promtool-proven PromQL and the exporter's real names.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from prometheus_client.openmetrics.exposition import generate_latest as om_generate
from prometheus_client.openmetrics.parser import text_string_to_metric_families

from candybar.api.app import create_app
from candybar.executor import EXECUTOR_METRICS, ExecutorMetrics
from candybar.executor.metrics import (
    EXEMPLAR_LABEL,
    INVOCATIONS_METRIC,
    RUNTIME_METRIC,
    SERIES_LABELS,
)
from candybar.executor.worker import Executor
from candybar.registry.models import Invocation
from test_executor import (
    CANDYBAR_ID,
    INVOCATION_ID,
    FakeBus,
    FakeMsg,
    FakeStore,
    make_command,
    make_outcome,
    make_row,
)

REPO = Path(__file__).resolve().parents[1]
PROMTOOL_TEST = REPO / "deploy" / "prometheus" / "candybar-queries.test.yml"


def fresh() -> ExecutorMetrics:
    return ExecutorMetrics(CollectorRegistry())


def counter(m: ExecutorMetrics, candybar_id: UUID | str, result: str) -> float:
    value = m.registry.get_sample_value(
        f"{INVOCATIONS_METRIC}_total", {"candybar_id": str(candybar_id), "result": result}
    )
    return value or 0.0


def hist_count(m: ExecutorMetrics, candybar_id: UUID | str, result: str) -> float:
    value = m.registry.get_sample_value(
        f"{RUNTIME_METRIC}_count", {"candybar_id": str(candybar_id), "result": result}
    )
    return value or 0.0


def om_families(m: ExecutorMetrics) -> dict[str, Any]:
    text = om_generate(m.registry).decode()
    return {f.name: f for f in text_string_to_metric_families(text)}


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


def test_success_observation_lands_on_both_families() -> None:
    m = fresh()
    m.observe(candybar_id=CANDYBAR_ID, result="success", invocation_id=INVOCATION_ID, runtime_ms=1234)
    assert counter(m, CANDYBAR_ID, "success") == 1
    assert hist_count(m, CANDYBAR_ID, "success") == 1
    labels = {"candybar_id": str(CANDYBAR_ID), "result": "success"}
    assert m.registry.get_sample_value(f"{RUNTIME_METRIC}_sum", labels) == 1234
    # 1234ms falls in le=2500, not le=1000
    assert m.registry.get_sample_value(f"{RUNTIME_METRIC}_bucket", {**labels, "le": "1000.0"}) == 0
    assert m.registry.get_sample_value(f"{RUNTIME_METRIC}_bucket", {**labels, "le": "2500.0"}) == 1


def test_failure_observation_counts_fail_and_runtime() -> None:
    m = fresh()
    m.observe(candybar_id=CANDYBAR_ID, result="fail", invocation_id=INVOCATION_ID, runtime_ms=7)
    assert counter(m, CANDYBAR_ID, "fail") == 1
    assert counter(m, CANDYBAR_ID, "success") == 0
    assert hist_count(m, CANDYBAR_ID, "fail") == 1


def test_both_result_series_materialize_at_zero() -> None:
    """A never-failed candybar still exposes result="fail" at 0, so the
    failure-ratio query yields 0 instead of no data."""
    m = fresh()
    m.observe(candybar_id=CANDYBAR_ID, result="success", invocation_id=INVOCATION_ID, runtime_ms=5)
    labels = {"candybar_id": str(CANDYBAR_ID), "result": "fail"}
    assert m.registry.get_sample_value(f"{INVOCATIONS_METRIC}_total", labels) == 0
    assert m.registry.get_sample_value(f"{RUNTIME_METRIC}_count", labels) == 0


def test_unknown_result_rejected() -> None:
    with pytest.raises(ValueError):
        fresh().observe(candybar_id=CANDYBAR_ID, result="ok", invocation_id=INVOCATION_ID, runtime_ms=1)


def test_negative_runtime_clamped() -> None:
    m = fresh()
    m.observe(candybar_id=CANDYBAR_ID, result="success", invocation_id=INVOCATION_ID, runtime_ms=-3)
    labels = {"candybar_id": str(CANDYBAR_ID), "result": "success"}
    assert m.registry.get_sample_value(f"{RUNTIME_METRIC}_sum", labels) == 0


# ---------------------------------------------------------------------------
# AC: cardinality guard + exemplars
# ---------------------------------------------------------------------------


def test_cardinality_guard_label_sets_are_exactly_candybar_id_and_result() -> None:
    m = fresh()
    candybars = [uuid4() for _ in range(3)]
    for i in range(30):
        m.observe(
            candybar_id=candybars[i % 3],
            result="success" if i % 4 else "fail",
            invocation_id=uuid4(),
            runtime_ms=i * 100,
        )
    families = [f for f in m.registry.collect() if f.name.startswith("candybar_")]
    assert {f.name for f in families} == {INVOCATIONS_METRIC, RUNTIME_METRIC}
    samples = [s for f in families for s in f.samples]
    assert samples
    for sample in samples:
        assert set(sample.labels) - {"le"} == set(SERIES_LABELS), sample
        assert EXEMPLAR_LABEL not in sample.labels
    # 30 invocations, 3 candybars: series count is bounded by candybars x results,
    # never by invocations.
    totals = [s for s in samples if s.name == f"{INVOCATIONS_METRIC}_total"]
    assert len(totals) == 3 * 2


def test_every_observation_carries_invocation_id_exemplar_in_openmetrics() -> None:
    m = fresh()
    a, b = uuid4(), uuid4()
    m.observe(candybar_id=CANDYBAR_ID, result="success", invocation_id=a, runtime_ms=40)
    m.observe(candybar_id=CANDYBAR_ID, result="fail", invocation_id=b, runtime_ms=4000)
    fams = om_families(m)

    totals = {
        s.labels["result"]: s
        for s in fams[INVOCATIONS_METRIC].samples
        if s.name == f"{INVOCATIONS_METRIC}_total"
    }
    assert totals["success"].exemplar.labels == {EXEMPLAR_LABEL: str(a)}
    assert totals["fail"].exemplar.labels == {EXEMPLAR_LABEL: str(b)}

    exemplar_buckets = [
        s for s in fams[RUNTIME_METRIC].samples if s.name.endswith("_bucket") and s.exemplar
    ]
    by_id = {s.exemplar.labels[EXEMPLAR_LABEL]: s for s in exemplar_buckets}
    assert set(by_id) == {str(a), str(b)}
    assert by_id[str(a)].labels["le"] == "50.0" and by_id[str(a)].exemplar.value == 40
    assert by_id[str(b)].labels["le"] == "5000.0" and by_id[str(b)].exemplar.value == 4000
    for s in exemplar_buckets:
        assert set(s.exemplar.labels) == {EXEMPLAR_LABEL}


def test_default_instance_lives_on_the_process_registry() -> None:
    from prometheus_client import REGISTRY

    assert EXECUTOR_METRICS.registry is REGISTRY


# ---------------------------------------------------------------------------
# Worker: recorded once per execution
# ---------------------------------------------------------------------------


def make_metered_executor(
    store: FakeStore, bus: FakeBus, metrics: Any, outcome: Any = None, by_key: Any = None
) -> tuple[Executor, list[dict[str, Any]]]:
    runs: list[dict[str, Any]] = []

    async def fake_run(**kwargs: Any) -> Any:
        runs.append(kwargs)
        return outcome if outcome is not None else make_outcome(runtime_ms=321)

    return (
        Executor(
            by_key=by_key or store.by_key,
            entrypoint=store.get_entrypoint,
            record=store.record,
            publish=bus.publish,
            run=fake_run,
            metrics=metrics,
        ),
        runs,
    )


def handle(executor: Executor, cmd: Any) -> FakeMsg:
    msg = FakeMsg(cmd.model_dump_json().encode())
    asyncio.run(executor.handle_message(msg))
    return msg


def test_worker_records_success_once_with_exemplar() -> None:
    m, store, bus = fresh(), FakeStore(), FakeBus()
    executor, _ = make_metered_executor(store, bus, m)
    msg = handle(executor, make_command())
    assert msg.acked == 1
    assert counter(m, CANDYBAR_ID, "success") == 1
    labels = {"candybar_id": str(CANDYBAR_ID), "result": "success"}
    assert m.registry.get_sample_value(f"{RUNTIME_METRIC}_sum", labels) == 321
    total = next(
        s
        for s in om_families(m)[INVOCATIONS_METRIC].samples
        if s.name.endswith("_total") and s.labels["result"] == "success"
    )
    assert total.exemplar.labels == {EXEMPLAR_LABEL: str(INVOCATION_ID)}


def test_worker_records_failure() -> None:
    m, store, bus = fresh(), FakeStore(), FakeBus()
    executor, _ = make_metered_executor(store, bus, m, make_outcome(exit_code=3, runtime_ms=9))
    handle(executor, make_command())
    assert counter(m, CANDYBAR_ID, "fail") == 1
    assert counter(m, CANDYBAR_ID, "success") == 0


def test_worker_records_spawn_failure_as_fail() -> None:
    m, store, bus = fresh(), FakeStore(), FakeBus()
    outcome = make_outcome(exit_code=None, spawn_error="PermissionError", runtime_ms=0)
    executor, _ = make_metered_executor(store, bus, m, outcome)
    handle(executor, make_command())
    assert counter(m, CANDYBAR_ID, "fail") == 1


def test_redelivery_does_not_recount() -> None:
    m, store, bus = fresh(), FakeStore(), FakeBus()
    cmd = make_command()
    store.rows[cmd.idempotency_key] = make_row(cmd.idempotency_key)
    executor, runs = make_metered_executor(store, bus, m)
    msg = handle(executor, cmd)
    assert runs == [] and msg.acked == 1
    assert counter(m, CANDYBAR_ID, "success") == 0
    assert hist_count(m, CANDYBAR_ID, "success") == 0


def test_lost_record_race_does_not_count() -> None:
    m, store, bus = fresh(), FakeStore(), FakeBus()
    store.record_result = False

    async def by_key_after_race(key: str) -> Invocation | None:
        return make_row(key) if store.records else None

    executor, runs = make_metered_executor(store, bus, m, by_key=by_key_after_race)
    msg = handle(executor, make_command())
    assert len(runs) == 1 and msg.acked == 1
    assert counter(m, CANDYBAR_ID, "success") == 0


def test_unknown_candybar_does_not_count() -> None:
    m, store, bus = fresh(), FakeStore(entrypoint=None), FakeBus()
    executor, _ = make_metered_executor(store, bus, m)
    msg = handle(executor, make_command())
    assert msg.termed == 1
    assert [f for f in m.registry.collect() if f.samples and f.name.startswith("candybar_")] == []


def test_record_crash_does_not_count_and_naks() -> None:
    m, store, bus = fresh(), FakeStore(), FakeBus()

    async def boom(rec: Any) -> bool:
        raise KeyError("candybar deleted")

    executor, _ = make_metered_executor(store, bus, m)
    executor._record = boom
    msg = handle(executor, make_command())
    assert msg.naks == [1] and msg.acked == 0
    assert counter(m, CANDYBAR_ID, "success") == 0


def test_terminal_publish_failure_counts_once_across_redelivery() -> None:
    """Row commits, publish fails (nak); the redelivery republishes from the row
    and must not observe again."""
    m, store, bus = fresh(), FakeStore(), FakeBus(fail_on={1})
    cmd = make_command()
    executor, runs = make_metered_executor(store, bus, m)
    first = handle(executor, cmd)
    assert first.naks == [1] and counter(m, CANDYBAR_ID, "success") == 1
    store.rows[cmd.idempotency_key] = make_row(cmd.idempotency_key)
    bus.fail_on = set()
    second = handle(executor, cmd)
    assert second.acked == 1 and len(runs) == 1
    assert counter(m, CANDYBAR_ID, "success") == 1


def test_recorder_error_never_fails_the_invocation(caplog: pytest.LogCaptureFixture) -> None:
    store, bus = FakeStore(), FakeBus()

    class Broken:
        def observe(self, **_: Any) -> None:
            raise RuntimeError("registry exploded")

    executor, _ = make_metered_executor(store, bus, Broken())
    msg = handle(executor, make_command())
    assert msg.acked == 1 and len(bus.events) == 2
    assert "metric recording failed" in caplog.text


# ---------------------------------------------------------------------------
# /metrics endpoint
# ---------------------------------------------------------------------------


def metrics_client(m: ExecutorMetrics) -> TestClient:
    @asynccontextmanager
    async def session_factory() -> AsyncIterator[Any]:
        yield object()

    return TestClient(
        create_app(session_factory=session_factory, sink=FakeBus(), metrics_registry=m.registry)
    )


def test_metrics_openmetrics_scrape_carries_exemplars() -> None:
    m = fresh()
    m.observe(candybar_id=CANDYBAR_ID, result="success", invocation_id=INVOCATION_ID, runtime_ms=80)
    resp = metrics_client(m).get(
        "/metrics", headers={"Accept": "application/openmetrics-text; version=1.0.0"}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/openmetrics-text")
    assert resp.text.rstrip().endswith("# EOF")
    assert f'# {{{EXEMPLAR_LABEL}="{INVOCATION_ID}"}}' in resp.text
    fams = {f.name: f for f in text_string_to_metric_families(resp.text)}
    assert INVOCATIONS_METRIC in fams and RUNTIME_METRIC in fams
    assert fams[RUNTIME_METRIC].type == "histogram"
    assert fams[INVOCATIONS_METRIC].type == "counter"


@pytest.mark.parametrize("accept", [None, "text/plain", "*/*"])
def test_metrics_plain_scrape_is_text_format(accept: str | None) -> None:
    m = fresh()
    m.observe(candybar_id=CANDYBAR_ID, result="fail", invocation_id=INVOCATION_ID, runtime_ms=80)
    headers = {"Accept": accept} if accept else {}
    resp = metrics_client(m).get("/metrics", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert f'{INVOCATIONS_METRIC}_total{{candybar_id="{CANDYBAR_ID}",result="fail"}} 1.0' in resp.text
    assert EXEMPLAR_LABEL not in resp.text  # text 0.0.4 has no exemplar syntax


def test_metrics_endpoint_defaults_to_process_registry() -> None:
    @asynccontextmanager
    async def session_factory() -> AsyncIterator[Any]:
        yield object()

    client = TestClient(create_app(session_factory=session_factory, sink=FakeBus()))
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert f"# TYPE {RUNTIME_METRIC} histogram" in resp.text


# ---------------------------------------------------------------------------
# Daemon: executor-only processes warn that /metrics is unexposed
# ---------------------------------------------------------------------------


def test_daemon_warns_when_executor_runs_without_api(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from candybar import daemon

    async def noop(stop: asyncio.Event) -> None:
        return None

    monkeypatch.setattr(daemon, "ROLES", {"api": noop, "executor": noop})
    asyncio.run(daemon.run_daemon(["executor"], asyncio.Event()))
    assert "/metrics is not exposed" in caplog.text
    caplog.clear()
    asyncio.run(daemon.run_daemon(["api", "executor"], asyncio.Event()))
    assert "/metrics is not exposed" not in caplog.text


# ---------------------------------------------------------------------------
# PromQL drift guard: promtool fixture uses the exporter's real shape
# ---------------------------------------------------------------------------


def test_promtool_series_match_exporter_names_and_labels() -> None:
    text = PROMTOOL_TEST.read_text()
    m = fresh()
    m.observe(candybar_id=CANDYBAR_ID, result="success", invocation_id=INVOCATION_ID, runtime_ms=1)
    exported = {s.name: set(s.labels) for f in m.registry.collect() for s in f.samples}

    series = re.findall(r"series: '([a-z_]+)\{([^}]*)\}'", text)
    assert series, "promtool fixture declares no input series"
    for name, raw_labels in series:
        assert name in exported, f"{name} is not exported by ExecutorMetrics"
        labels = {kv.split("=", 1)[0] for kv in raw_labels.split(",")}
        assert labels == exported[name], (name, labels, exported[name])
    for metric in set(re.findall(r"\b(candybar_[a-z_]+)", text)) - {"candybar_id"}:
        assert metric in exported, f"query references unknown metric {metric}"
