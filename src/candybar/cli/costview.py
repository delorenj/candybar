"""How `candybar ls`, `show` and `cost` print a candybar's cost.

Pure formatting over the API's `cost` object (`candybar.costs` derives it):
no I/O, no dependency on `control.py` (which imports this). Tiers read as a
coloured dot AND a three-letter tag, so a piped or colour-blind read keeps the
tier: `● RED`, `● YEL`, `● GRN`, and a hollow `○` for unmeasured (grey).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import typer

from candybar.costs import DIMENSIONS, format_figure, format_number, headline

# tier -> (dot, tag, colour)
TIER_STYLE = {
    "green": ("●", "GRN", "green"),
    "yellow": ("●", "YEL", "yellow"),
    "red": ("●", "RED", "red"),
    "unmeasured": ("○", "", "bright_black"),
}
# cost-object key of each dimension's per-run / per-day figure
_DAY_KEY = {"cpu": "cpuS", "gpu": "gpuS", "memory": "memGibH", "tokens": "tokens", "usd": "usd"}
DIMENSION_LABEL = {"cpu": "cpu", "gpu": "gpu", "memory": "mem", "tokens": "tokens", "usd": "usd"}


def tier_mark(tier: str) -> str:
    """`● RED` in the tier's colour (`○` for unmeasured)."""
    dot, tag, colour = TIER_STYLE.get(tier, TIER_STYLE["unmeasured"])
    return typer.style(f"{dot} {tag}".rstrip(), fg=colour)


def cost_cell(cost: Mapping[str, Any] | None) -> str:
    """The `ls` COST column: the tier mark and the figure that drove it."""
    if not cost:
        return "-"
    return f"{tier_mark(cost['tier'])} {headline(cost)}"


def dimension_cell(cost: Mapping[str, Any], dimension: str) -> str:
    """One per-day figure, coloured by its own tier (`-` when unmeasured)."""
    value = cost["perDay"][_DAY_KEY[dimension]]
    if value is None:
        return typer.style("-", fg="bright_black")
    text = f"${value:.2f}" if dimension == "usd" else format_number(value)
    colour = TIER_STYLE[cost["tiers"][dimension]][2]
    return typer.style(text, fg=colour)


def _seconds(value: float | None) -> str:
    if value is None:
        return "-"
    if value < 60:
        return f"{value:.1f}s"
    return f"{int(value) // 60}m{int(value) % 60:02d}s"


def _per_run(cost: Mapping[str, Any]) -> str:
    run = cost["perRun"]
    parts = [
        f"{_seconds(run['runtimeS'])} runtime",
        "cpu -" if run["cpuS"] is None else f"{format_number(run['cpuS'])} core-s",
        "mem -" if run["memPeakMb"] is None else f"{run['memPeakMb']:.0f} MB peak",
    ]
    if run["gpuS"] is not None:
        parts.append(f"{format_number(run['gpuS'])} gpu-s")
    parts.append("tokens -" if run["tokens"] is None else f"{format_number(run['tokens'])} tokens")
    parts.append("usd -" if run["usd"] is None else f"${run['usd']:.4f}")
    return " · ".join(parts)


def _per_day(cost: Mapping[str, Any]) -> str:
    basis = {"schedule": "scheduled", "observed": "observed (never scheduled)", "none": "no fires"}
    prefix = f"{format_number(cost['firesPerDay'])} runs/day {basis[cost['firesPerDayBasis']]}: "
    figures = [
        f"{DIMENSION_LABEL[d]} {format_figure(d, cost['perDay'][_DAY_KEY[d]]).removesuffix('/day')}"
        for d in DIMENSIONS
        if cost["perDay"][_DAY_KEY[d]] is not None
    ]
    return prefix + (" · ".join(figures) if figures else "nothing measured")


def _tiers(cost: Mapping[str, Any]) -> str:
    return " · ".join(
        f"{DIMENSION_LABEL[d]} {tier_mark(cost['tiers'][d])}" for d in DIMENSIONS
    )


def _computed(cost: Mapping[str, Any], now: datetime) -> str:
    at = cost.get("computedAt")
    if not at:
        return "never aggregated"
    seconds = int((now - datetime.fromisoformat(at).astimezone(UTC)).total_seconds())
    if seconds < 90:
        return "updated just now"
    if seconds < 5400:
        return f"updated {seconds // 60}m ago"
    return f"updated {seconds // 3600}h ago"


def cost_what_if_cell(variant: Mapping[str, Any]) -> str:
    return f"{tier_mark(variant['tier'])} {headline(variant)}"


def cost_lines(
    cost: Mapping[str, Any] | None,
    what_if: list[Mapping[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> list[tuple[str, str]]:
    """The key/value lines `show` prints for the cost, run and day breakdowns and what-ifs."""
    if not cost:
        return [("cost", "-")]
    now = now or datetime.now(UTC)
    driver = f" ({cost['driver']})" if cost["driver"] else ""
    window = (
        f"[{cost['windowDays']}d window, {cost['runs']} runs, "
        f"{cost['failedRuns']} failed, {_computed(cost, now)}]"
    )
    lines = [("cost", f"{cost_cell(cost)}{driver}  {window}")]
    if cost["runs"]:
        lines += [
            ("per run", _per_run(cost)),
            ("per day", _per_day(cost)),
            ("tiers", _tiers(cost)),
        ]
    for variant in what_if or []:
        if cost["runs"] == 0:
            break
        lines.append((f"if {variant['label']}", cost_what_if_cell(variant)))
    for note in cost.get("notes", []):
        lines.append(("note", note))
    return lines


__all__ = [
    "cost_cell",
    "cost_lines",
    "dimension_cell",
    "tier_mark",
]
