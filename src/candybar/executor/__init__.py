"""Executor role: invoke-command consumer — cgroup v2 scoped subprocess execution + metering (AD-7, AD-9).

Story 1.6: bare-executable execution, terminal registry record, lifecycle
events. Epic 3 adds memory/CPU/GPU metering from the cgroup scope.
"""

from .role import run_executor
from .runner import ProcessRegistry, ResultFile, RunOutcome, run_invocation
from .worker import Executor, resolve_outcome, terminal_event_for

__all__ = [
    "Executor",
    "ProcessRegistry",
    "ResultFile",
    "RunOutcome",
    "resolve_outcome",
    "run_executor",
    "run_invocation",
    "terminal_event_for",
]
