"""Story 3.3: token-spend counter and full exemplar coverage (AD-9).

Covers the spec's I/O matrix on the metrics side: the counter accumulates the
resolved self-reported spend on `{candybar_id, result}` (0 for bare/invalid
results, fail-side tokens still count), every exemplar-capable CAP-3 family
carries the `invocation_id` exemplar on every observation (parsed from a real
OpenMetrics scrape), and the once-per-execution rule holds for redelivery.
The API read surface is tests/test_api_invocations.py.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from prometheus_client import CollectorRegistry
from prometheus_client.openmetrics.exposition import generate_latest as om_generate
from prometheus_client.openmetrics.parser import text_string_to_metric_families

from candybar.events.envelope import SelfReportedMetrics
from candybar.executor import ExecutorMetrics
from candybar.executor.metrics import (
    CPU_LOAD_METRIC,
    EXEMPLAR_LABEL,
    EXEMPLAR_LESS_FAMILIES,
    GPU_LOAD_METRIC,
    INVOCATIONS_METRIC,
    MEMORY_METRIC,
    RUNTIME_METRIC,
    TOKEN_SPEND_METRIC,
)
from candybar.executor.runner import ResultFile, run_invocation
from candybar.executor.worker import Executor
from test_executor import (
    CANDYBAR_ID,
    FakeBus,
    FakeMsg,
    FakeStore,
    make_command,
    make_outcome,
    make_row,
)

# The spine's CAP-3 families (runtime, memory, token spend, CPU, GPU) plus the
# invocation counter — each must carry the exemplar.
CAP3_EXEMPLAR_FAMILIES = {
    INVOCATIONS_METRIC,
    RUNTIME_METRIC,
    MEMORY_METRIC,
    TOKEN_SPEND_METRIC,
    CPU_LOAD_METRIC,
    GPU_LOAD_METRIC,
}


def fresh() -> ExecutorMetrics:
    return ExecutorMetrics(CollectorRegistry())


def tokens(m: ExecutorMetrics, result: str, candybar_id: UUID | str = CANDYBAR_ID) -> float | None:
    return m.registry.get_sample_value(
        f"{TOKEN_SPEND_METRIC}_total", {"candybar_id": str(candybar_id), "result": result}
    )


def om_families(m: ExecutorMetrics) -> dict[str, Any]:
    text = om_generate(m.registry).decode()
    return {f.name: f for f in text_string_to_metric_families(text)}


def exemplar_ids(family: Any, labels: dict[str, str]) -> set[str]:
    """invocation_ids of the exemplars on one {candybar_id, result} series."""
    return {
        s.exemplar.labels[EXEMPLAR_LABEL]
        for s in family.samples
        if s.exemplar is not None
        and all(s.labels.get(k) == v for k, v in labels.items())
    }


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


def test_counter_accumulates_reported_spend_per_candybar_and_result() -> None:
    m = fresh()
    other = uuid4()
    m.observe(candybar_id=CANDYBAR_ID, result="success", invocation_id=uuid4(), runtime_ms=1, token_spend=1200)
    m.observe(candybar_id=CANDYBAR_ID, result="success", invocation_id=uuid4(), runtime_ms=1, token_spend=300)
    m.observe(candybar_id=CANDYBAR_ID, result="fail", invocation_id=uuid4(), runtime_ms=1, token_spend=500)
    m.observe(candybar_id=other, result="success", invocation_id=uuid4(), runtime_ms=1, token_spend=7)
    assert tokens(m, "success") == 1500
    assert tokens(m, "fail") == 500
    assert tokens(m, "success", other) == 7
    assert tokens(m, "fail", other) == 0  # materialized at zero, not absent


def test_default_and_negative_spend_count_zero() -> None:
    m = fresh()
    m.observe(candybar_id=CANDYBAR_ID, result="success", invocation_id=uuid4(), runtime_ms=1)
    m.observe(candybar_id=CANDYBAR_ID, result="success", invocation_id=uuid4(), runtime_ms=1, token_spend=-5)
    assert tokens(m, "success") == 0


def test_zero_spend_still_carries_the_exemplar() -> None:
    m = fresh()
    inv = uuid4()
    m.observe(candybar_id=CANDYBAR_ID, result="success", invocation_id=inv, runtime_ms=1)
    labels = {"candybar_id": str(CANDYBAR_ID), "result": "success"}
    assert exemplar_ids(om_families(m)[TOKEN_SPEND_METRIC], labels) == {str(inv)}


def test_token_exemplar_value_is_the_invocations_spend() -> None:
    m = fresh()
    inv = uuid4()
    m.observe(candybar_id=CANDYBAR_ID, result="success", invocation_id=inv, runtime_ms=1, token_spend=1200)
    (sample,) = [
        s for s in om_families(m)[TOKEN_SPEND_METRIC].samples
        if s.name == f"{TOKEN_SPEND_METRIC}_total" and s.exemplar is not None
    ]
    assert sample.exemplar.labels == {EXEMPLAR_LABEL: str(inv)}
    assert sample.exemplar.value == 1200


# ---------------------------------------------------------------------------
# AC: full exemplar coverage
# ---------------------------------------------------------------------------


def test_every_family_is_exemplar_covered_or_has_a_covered_twin() -> None:
    """No family can join ExecutorMetrics without exemplar coverage."""
    m = fresh()
    names = {f._name for f in m._families()}
    assert names == CAP3_EXEMPLAR_FAMILIES | set(EXEMPLAR_LESS_FAMILIES)
    for gauge, twin in EXEMPLAR_LESS_FAMILIES.items():
        assert twin in CAP3_EXEMPLAR_FAMILIES, (gauge, twin)
    # and the registry exposes nothing ExecutorMetrics doesn't know about
    m.observe(candybar_id=CANDYBAR_ID, result="success", invocation_id=uuid4(), runtime_ms=1)
    exposed = {f.name for f in m.registry.collect() if f.name.startswith("candybar_")}
    assert exposed == names


@pytest.mark.parametrize("family", sorted(CAP3_EXEMPLAR_FAMILIES))
def test_every_observation_carries_its_invocation_id_on_every_family(family: str) -> None:
    """Three invocations on three distinct series: each series' exemplar is its own id."""
    m = fresh()
    other = uuid4()
    observations = [
        (CANDYBAR_ID, "success", uuid4(), 40, 1 * 1024 * 1024, 0.5, 0.2, 1200),
        (CANDYBAR_ID, "fail", uuid4(), 4000, 300 * 1024 * 1024, 2.0, 0.0, 0),
        (other, "success", uuid4(), 0, 0, 0.0, 0.0, 0),  # a fully unmeasured run
    ]
    for cid, result, inv, runtime, mem, cpu, gpu, spend in observations:
        m.observe(
            candybar_id=cid, result=result, invocation_id=inv, runtime_ms=runtime,
            memory_peak_bytes=mem, cpu_load=cpu, gpu_load=gpu, token_spend=spend,
        )
    fam = om_families(m)[family]
    for cid, result, inv, *_ in observations:
        labels = {"candybar_id": str(cid), "result": result}
        assert exemplar_ids(fam, labels) == {str(inv)}, (family, labels)
    for s in fam.samples:
        if s.exemplar is not None:
            assert set(s.exemplar.labels) == {EXEMPLAR_LABEL}


def test_memory_gauge_carries_no_exemplar_but_its_twin_does() -> None:
    m = fresh()
    inv = uuid4()
    m.observe(
        candybar_id=CANDYBAR_ID, result="success", invocation_id=inv, runtime_ms=1,
        memory_peak_bytes=64 * 1024 * 1024,
    )
    fams = om_families(m)
    for gauge, twin in EXEMPLAR_LESS_FAMILIES.items():
        assert fams[gauge].type == "gauge"
        assert all(s.exemplar is None for s in fams[gauge].samples)
        labels = {"candybar_id": str(CANDYBAR_ID), "result": "success"}
        assert exemplar_ids(fams[twin], labels) == {str(inv)}


# ---------------------------------------------------------------------------
# Worker: matrix rows (resolved spend, once per execution)
# ---------------------------------------------------------------------------


def executor_with(m: ExecutorMetrics, store: FakeStore, bus: FakeBus, outcome: Any) -> Executor:
    async def fake_run(**_kwargs: Any) -> Any:
        return outcome

    return Executor(
        by_key=store.by_key, entrypoint=store.get_entrypoint, record=store.record,
        publish=bus.publish, run=fake_run, metrics=m,
    )


def handle(executor: Executor, cmd: Any) -> FakeMsg:
    msg = FakeMsg(cmd.model_dump_json().encode())
    asyncio.run(executor.handle_message(msg))
    return msg


def test_llm_job_counts_spend_and_records_cost() -> None:
    m, store, bus = fresh(), FakeStore(), FakeBus()
    outcome = make_outcome(
        result=ResultFile(
            status="success", metrics=SelfReportedMetrics(token_spend=1200, cost_usd=0.03)
        )
    )
    msg = handle(executor_with(m, store, bus, outcome), make_command())
    assert msg.acked == 1
    assert tokens(m, "success") == 1200 and tokens(m, "fail") == 0
    rec = store.records[0]
    assert rec.token_spend == 1200 and rec.cost_usd == pytest.approx(0.03)
    labels = {"candybar_id": str(CANDYBAR_ID), "result": "success"}
    assert exemplar_ids(om_families(m)[TOKEN_SPEND_METRIC], labels) == {
        str(make_command().data.invocation_id)
    }


def test_bare_executable_counts_zero_with_exemplar() -> None:
    m, store, bus = fresh(), FakeStore(), FakeBus()
    handle(executor_with(m, store, bus, make_outcome()), make_command())
    assert tokens(m, "success") == 0
    labels = {"candybar_id": str(CANDYBAR_ID), "result": "success"}
    assert exemplar_ids(om_families(m)[TOKEN_SPEND_METRIC], labels) == {
        str(make_command().data.invocation_id)
    }


def test_failed_llm_job_still_counts_its_tokens_on_fail() -> None:
    m, store, bus = fresh(), FakeStore(), FakeBus()
    outcome = make_outcome(
        exit_code=1,
        result=ResultFile(status="fail", metrics=SelfReportedMetrics(token_spend=500)),
    )
    handle(executor_with(m, store, bus, outcome), make_command())
    assert tokens(m, "fail") == 500 and tokens(m, "success") == 0
    assert store.records[0].result == "fail"


def test_invalid_result_file_counts_zero_on_fail() -> None:
    m, store, bus = fresh(), FakeStore(), FakeBus()
    outcome = make_outcome(result_error="result file is not valid JSON")
    handle(executor_with(m, store, bus, outcome), make_command())
    assert tokens(m, "fail") == 0 and tokens(m, "success") == 0
    labels = {"candybar_id": str(CANDYBAR_ID), "result": "fail"}
    assert exemplar_ids(om_families(m)[TOKEN_SPEND_METRIC], labels) == {
        str(make_command().data.invocation_id)
    }


def test_redelivery_does_not_recount_tokens() -> None:
    m, store, bus = fresh(), FakeStore(), FakeBus()
    cmd = make_command()
    store.rows[cmd.idempotency_key] = make_row(cmd.idempotency_key, token_spend=1200)
    msg = handle(executor_with(m, store, bus, make_outcome()), cmd)
    assert msg.acked == 1
    assert tokens(m, "success") is None  # never observed at all
    # the republished fact still carries the stored spend
    assert bus.events[-1].data.metrics.token_spend == 1200


def test_sdk_authored_candybar_spend_reaches_the_counter(tmp_path: Path, monkeypatch: Any) -> None:
    """Real python SDK child, real runner: add_tokens/add_cost -> result JSON -> counter."""
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    script = tmp_path / "llm_job.py"
    script.write_text(
        f"#!{sys.executable}\n"
        "from candybar_sdk import candybar\n\n"
        "@candybar\n"
        "def main(ctx):\n"
        "    ctx.add_tokens(1000)\n"
        "    ctx.add_tokens(234)\n"
        "    ctx.add_cost(0.25)\n"
        "    return 'summarized'\n\n"
        "if __name__ == '__main__':\n"
        "    main.run()\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    m, store, bus = fresh(), FakeStore(entrypoint=str(script)), FakeBus()

    async def real_run(**kwargs: Any) -> Any:
        return await run_invocation(**kwargs, root="")

    executor = Executor(
        by_key=store.by_key, entrypoint=store.get_entrypoint, record=store.record,
        publish=bus.publish, run=real_run, metrics=m,
    )
    env_path = os.pathsep.join(p for p in sys.path if p)
    monkeypatch.setenv("PYTHONPATH", env_path)
    msg = handle(executor, make_command())
    assert msg.acked == 1, store.records
    rec = store.records[0]
    assert rec.result == "success"
    assert rec.token_spend == 1234
    assert Decimal(str(rec.cost_usd)) == Decimal("0.25")
    assert tokens(m, "success") == 1234
    completed = bus.events[-1]
    assert completed.data.metrics.token_spend == 1234 and completed.data.metrics.cost_usd == 0.25
