"""Per-candybar Prometheus series with per-invocation exemplars (AD-9).

Series are labeled `{candybar_id, result}` ONLY — a per-invocation label is a
cardinality bomb. Per-invocation identity rides as an `invocation_id`
exemplar on every observation; the full per-invoke record lives in the
`invocations` table and the completed/failed event data.

Story 1.7: wall-ms runtime histogram + invocation counter. Story 3.1 adds
memory and CPU-load families on the same label set:

- `candybar_invocation_memory_peak_bytes` (gauge): cgroup memory.peak of the
  LATEST invocation in the series — the spine's "memory max gauge";
  `max_over_time` over it finds memory hogs.
- `candybar_invocation_memory_bytes` (histogram): the distribution of
  per-invocation peaks. It exists because OpenMetrics (and prometheus-client)
  only allow exemplars on counters and histogram buckets — a gauge can never
  carry the `invocation_id` exemplar, its histogram twin does.
- `candybar_invocation_cpu_load_ratio` (histogram): average busy cores per
  invocation (cpu.stat usage delta / wall runtime). This is the spine's "CPU
  load summary": the client's Summary exposes only `_sum`/`_count` and cannot
  carry exemplars; a histogram is a strict superset that can.
"""

from __future__ import annotations

from uuid import UUID

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram

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

_MIB = 1024 * 1024
# Per-invocation peak memory, 1 MiB .. 32 GiB (+Inf).
MEMORY_BUCKETS_BYTES: tuple[float, ...] = tuple(
    float(n * _MIB)
    for n in (1, 8, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768)
)
# Average busy cores, idle .. 32 cores (+Inf); 1.0 = one core saturated.
CPU_LOAD_BUCKETS: tuple[float, ...] = (
    0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 4.0, 8.0, 16.0, 32.0
)

INVOCATIONS_METRIC = "candybar_invocations"  # exposed as candybar_invocations_total
RUNTIME_METRIC = "candybar_invocation_runtime_milliseconds"
MEMORY_PEAK_METRIC = "candybar_invocation_memory_peak_bytes"
MEMORY_METRIC = "candybar_invocation_memory_bytes"
CPU_LOAD_METRIC = "candybar_invocation_cpu_load_ratio"


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
        self.memory_peak = Gauge(
            MEMORY_PEAK_METRIC,
            "Peak memory (cgroup memory.peak) of the latest invocation, in bytes.",
            labelnames=SERIES_LABELS,
            registry=registry,
        )
        self.memory = Histogram(
            MEMORY_METRIC,
            "Per-invocation peak memory (cgroup memory.peak) distribution, in bytes.",
            labelnames=SERIES_LABELS,
            buckets=MEMORY_BUCKETS_BYTES,
            registry=registry,
        )
        self.cpu_load = Histogram(
            CPU_LOAD_METRIC,
            "Per-invocation average CPU load: busy cores over wall runtime (1.0 = one core).",
            labelnames=SERIES_LABELS,
            buckets=CPU_LOAD_BUCKETS,
            registry=registry,
        )

    def observe(
        self,
        *,
        candybar_id: UUID | str,
        result: str,
        invocation_id: UUID | str,
        runtime_ms: int,
        memory_peak_bytes: int = 0,
        cpu_load: float = 0.0,
    ) -> None:
        """Record one terminal invocation; called once per execution.

        Unmeasured memory/CPU arrive as 0 and are observed as 0 (spine rule).
        """
        if result not in RESULTS:
            raise ValueError(f"result must be one of {RESULTS}, got {result!r}")
        exemplar = {EXEMPLAR_LABEL: str(invocation_id)}
        cid = str(candybar_id)
        # Materialize both result series at zero on first sight (bounded: two
        # per candybar) so fail-ratio queries yield 0, not "no data", for a
        # candybar that has never failed.
        for other in RESULTS:
            for family in self._families():
                family.labels(candybar_id=cid, result=other)
        labels = {"candybar_id": cid, "result": result}
        memory = max(memory_peak_bytes, 0)
        load = max(cpu_load, 0.0)
        self.invocations.labels(**labels).inc(1, exemplar=exemplar)
        self.runtime_ms.labels(**labels).observe(max(runtime_ms, 0), exemplar=exemplar)
        self.memory_peak.labels(**labels).set(memory)  # gauges cannot carry exemplars
        self.memory.labels(**labels).observe(memory, exemplar=exemplar)
        self.cpu_load.labels(**labels).observe(load, exemplar=exemplar)

    def _families(self) -> tuple[Counter | Gauge | Histogram, ...]:
        return (self.invocations, self.runtime_ms, self.memory_peak, self.memory, self.cpu_load)


# Process-default instance: the api role's /metrics serves REGISTRY, so what
# the executor records in the same process (AD-2) is what Prometheus scrapes.
EXECUTOR_METRICS = ExecutorMetrics()

__all__ = [
    "CPU_LOAD_BUCKETS",
    "CPU_LOAD_METRIC",
    "EXECUTOR_METRICS",
    "EXEMPLAR_LABEL",
    "INVOCATIONS_METRIC",
    "MEMORY_BUCKETS_BYTES",
    "MEMORY_METRIC",
    "MEMORY_PEAK_METRIC",
    "RESULTS",
    "RUNTIME_BUCKETS_MS",
    "RUNTIME_METRIC",
    "SERIES_LABELS",
    "ExecutorMetrics",
]
