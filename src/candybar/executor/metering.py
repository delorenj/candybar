"""Per-invocation resource metering from the cgroup v2 scope (AD-9, Story 3.1).

The executor — never the child — measures memory and CPU:

- memory = `memory.peak` of the invocation's scope (bytes; needs the memory
  controller delegated to the scope's parent);
- CPU    = `cpu.stat` `usage_usec` delta over the run / wall runtime, i.e. the
  average number of busy cores (1.0 = one core). `cpu.stat` core fields exist
  on every cgroup v2 node, with or without the cpu controller.

Everything here is best-effort and never raises: a missing scope, an
undelegated controller, or an unreadable file meters as 0 — the spine's
"unmeasured metrics report zero" — so metering can never fail an invocation.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

CONTROLLERS: tuple[str, ...] = ("memory", "cpu")
CPU_LOAD_DECIMALS = 4

_enabled_roots: set[str] = set()
_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: object) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(message, *args)


def enable_controllers(root: Path) -> None:
    """Delegate memory + cpu to the scopes under `root` (once per root).

    Writes `+memory`, `+cpu` to `root/cgroup.subtree_control` one at a time
    (one unavailable controller must not block the other). Fails soft: the
    parent may not offer a controller, or `root` may hold processes.
    """
    key = str(root)
    if key in _enabled_roots:
        return
    _enabled_roots.add(key)
    control = root / "cgroup.subtree_control"
    for controller in CONTROLLERS:
        try:
            with control.open("a", encoding="ascii") as fh:
                fh.write(f"+{controller}")
        except OSError as exc:
            _warn_once(
                f"enable:{key}:{controller}",
                "cannot delegate %s controller under %s (%s); it will meter as 0",
                controller,
                root,
                exc,
            )


def read_memory_peak(scope: Path | None) -> int:
    """Peak bytes charged to the scope, or 0 when unmeasurable."""
    if scope is None:
        return 0
    try:
        raw = (scope / "memory.peak").read_text(encoding="ascii").strip()
        return max(int(raw), 0)
    except FileNotFoundError:
        _warn_once(
            f"peak:{scope.parent}",
            "no memory.peak under %s (memory controller not delegated); memory meters as 0",
            scope.parent,
        )
        return 0
    except (OSError, ValueError):
        return 0


def read_cpu_usage_usec(scope: Path | None) -> int:
    """`usage_usec` from the scope's cpu.stat, or 0 when unmeasurable."""
    if scope is None:
        return 0
    try:
        text = (scope / "cpu.stat").read_text(encoding="ascii")
    except OSError:
        return 0
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key == "usage_usec":
            try:
                return max(int(value.strip()), 0)
            except ValueError:
                return 0
    return 0


def cpu_load(usage_delta_usec: int, runtime_ms: int) -> float:
    """Average busy cores over the wall runtime; 0 for a 0 ms run or bad delta."""
    if runtime_ms <= 0 or usage_delta_usec <= 0:
        return 0.0
    return round(usage_delta_usec / (runtime_ms * 1000), CPU_LOAD_DECIMALS)


__all__ = [
    "CONTROLLERS",
    "cpu_load",
    "enable_controllers",
    "read_cpu_usage_usec",
    "read_memory_peak",
]
