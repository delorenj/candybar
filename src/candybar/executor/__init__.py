"""Executor role: invoke-command consumer — cgroup v2 scoped subprocess execution + metering (AD-7, AD-9).

Story 1.6: bare-executable execution, terminal registry record, lifecycle
events. Story 1.7: per-candybar runtime/result series with
invocation_id exemplars (metrics.py). Epic 3 adds memory/CPU metering from the cgroup scope (3.1) and GPU load from nvidia-smi pmon (3.2).
"""

from .gpu import GPU_SAMPLER, GpuSampler
from .metrics import EXECUTOR_METRICS, ExecutorMetrics
from .role import run_executor
from .runner import ProcessRegistry, ResultFile, RunOutcome, run_invocation
from .worker import Executor, resolve_outcome, terminal_event_for

__all__ = [
    "EXECUTOR_METRICS",
    "GPU_SAMPLER",
    "Executor",
    "ExecutorMetrics",
    "GpuSampler",
    "ProcessRegistry",
    "ResultFile",
    "RunOutcome",
    "resolve_outcome",
    "run_executor",
    "run_invocation",
    "terminal_event_for",
]
