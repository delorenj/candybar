"""Per-candybar Prometheus series with per-invocation exemplars (AD-9).

Series are labeled `{candybar_id, result}` ONLY — a per-invocation label is a
cardinality bomb. Per-invocation identity rides as an `invocation_id`
exemplar on every observation; the full per-invoke record lives in the
`invocations` table and the completed/failed event data.

Story 1.7 scope: wall-ms runtime histogram + invocation counter. Epic 3 adds
memory/CPU/GPU/token families on the same label set.
"""

from __future__ import annotations

from uuid import UUID

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Histogram

SERIES_LABELS: tuple[str, ...] = ("candybar_id", "result")
EXEMPLAR_LABEL = "invocation_id"
RESULTS: tuple[str, ...] = ("success", "fail")

# Wall milliseconds, 10ms .. 1h (+Inf): candybars range from shell one-liners
# to hour-long batch jobs.
RUNTIME_BUCKETS_MS: tuple[float, ...] = (
    10,
    50,
    100,
    250,
    500,
    1_000,
    2_500,
    5_000,
    10_000,
    30_000,
    60_000,
    300_000,
    600_000,
    1_800_000,
    3_600_000,
)

INVOCATIONS_METRIC = "candybar_invocations"  # exposed as candybar_invocations_total
RUNTIME_METRIC = "candybar_invocation_runtime_milliseconds"


class ExecutorMetrics:
    """The executor's metric families, bound to one CollectorRegistry."""

    def __init__(self, registry: CollectorRegistry = REGISTRY) -> None:
        self.registry = registry
        self.invocations = Counter(
            INVOCATIONS_METRIC,
            "Candybar invocations by terminal result.",
            labelnames=SERIES_LABELS,
            registry=registry,
        )
        self.runtime_ms = Histogram(
            RUNTIME_METRIC,
            "Candybar invocation wall-clock runtime in milliseconds.",
            labelnames=SERIES_LABELS,
            buckets=RUNTIME_BUCKETS_MS,
            registry=registry,
        )

    def observe(
        self, *, candybar_id: UUID | str, result: str, invocation_id: UUID | str, runtime_ms: int
    ) -> None:
        """Record one terminal invocation; called once per execution."""
        if result not in RESULTS:
            raise ValueError(f"result must be one of {RESULTS}, got {result!r}")
        exemplar = {EXEMPLAR_LABEL: str(invocation_id)}
        cid = str(candybar_id)
        # Materialize both result series at zero on first sight (bounded: two
        # per candybar) so fail-ratio queries yield 0, not "no data", for a
        # candybar that has never failed.
        for other in RESULTS:
            self.invocations.labels(candybar_id=cid, result=other)
            self.runtime_ms.labels(candybar_id=cid, result=other)
        labels = {"candybar_id": cid, "result": result}
        self.invocations.labels(**labels).inc(1, exemplar=exemplar)
        self.runtime_ms.labels(**labels).observe(max(runtime_ms, 0), exemplar=exemplar)


# Process-default instance: the api role's /metrics serves REGISTRY, so what
# the executor records in the same process (AD-2) is what Prometheus scrapes.
EXECUTOR_METRICS = ExecutorMetrics()

__all__ = [
    "EXECUTOR_METRICS",
    "EXEMPLAR_LABEL",
    "INVOCATIONS_METRIC",
    "RESULTS",
    "RUNTIME_BUCKETS_MS",
    "RUNTIME_METRIC",
    "SERIES_LABELS",
    "ExecutorMetrics",
]
