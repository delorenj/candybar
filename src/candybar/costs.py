"""What a candybar costs to run: per-run averages, per-day projection and a tier.

Pure and clock-free, like `dispatcher/firetimes.py`: no SQL, no I/O. The store
holds the rolling-window TOTALS per candybar (`registry.store.refresh_costs`,
kept current event-driven by the executor), and everything derived from them is
computed here at read time. A changed schedule or threshold therefore never
needs a recompute.

Dimensions, all projected to one day at the candybar's CURRENT schedule
(per-run average x fires per day), so "what should the interval be" is a
number you can read off:

    cpu     core-seconds of CPU per day        (cgroup cpu.stat / systemd CPUUsageNSec)
    gpu     GPU-seconds per day                (nvidia-smi pmon, 1.0 = one GPU saturated)
    memory  GiB-hours per day                  (peak RSS x wall time, a footprint, not a bill)
    tokens  LLM tokens per day                 (what the run reported, see below)
    usd     dollars per day                    (what the run reported)

Every dimension is green, yellow, red, or `unmeasured` (grey). The overall
tier is the WORST measured dimension, and `driver` names the dimension that set
it. A dimension no run measured is `unmeasured`, never green, so a green tier
cannot be the absence of data:

* CPU, GPU and memory are metered by the executor itself. A run the executor
  could not meter (no cgroup scope, a legacy systemd row) is not counted, and
  an n8n candybar is never metered (the workflow runs inside n8n; the runner
  only sees its own webhook client).
* Tokens and USD are self-reported by the job (AD-8 result file) or the runner
  (Hermes). The SDK writes `token_spend: 0` when a job never set it, so 0 and
  "did not report" look the same in a row; a candybar counts as reporting
  tokens only when some run in the window reported more than 0.
* GPU reads 0 both when nothing used the GPU and when nvidia-smi is missing, so
  it is `unmeasured` until some run in the window used the GPU.

Thresholds are per day, per dimension, `[yellow, red]`; override with
`CANDYBAR_COST_THRESHOLDS='{"cpu": [300, 1800]}'` (any subset). The defaults
were set from the live 7-day distribution on 2026-11-11 (README, "Cost").
"""

from __future__ import annotations

import functools
import json
import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from .timezones import resolve_timezone

log = logging.getLogger(__name__)

WINDOW_DAYS_ENV = "CANDYBAR_COST_WINDOW_DAYS"
THRESHOLDS_ENV = "CANDYBAR_COST_THRESHOLDS"
DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 90

DIMENSIONS = ("cpu", "gpu", "memory", "tokens", "usd")
GREEN, YELLOW, RED, UNMEASURED = "green", "yellow", "red", "unmeasured"
TIER_RANK = {UNMEASURED: -1, GREEN: 0, YELLOW: 1, RED: 2}

# Per-day [yellow, red]. Calibrated on the live 7-day distribution: see the
# README ("Cost") for the jobs each bar catches today.
DEFAULT_THRESHOLDS: dict[str, tuple[float, float]] = {
    "cpu": (300.0, 1800.0),  # core-seconds/day: 5 core-minutes yellow, half a core all day red
    "gpu": (300.0, 1800.0),  # GPU-seconds/day (uncalibrated: nothing used the GPU yet)
    "memory": (0.5, 2.0),  # GiB-hours/day
    "tokens": (250_000.0, 2_000_000.0),  # tokens/day
    "usd": (0.10, 1.00),  # dollars/day: $1/day is the OpenRouter budget's order of magnitude
}

UNITS = {
    "cpu": "core-s/day",
    "gpu": "gpu-s/day",
    "memory": "GiB-h/day",
    "tokens": "tokens/day",
    "usd": "USD/day",
}

# "What if it ran at ...": the cadences `show` and the cost route compare.
WHAT_IF_CADENCES = (("every 15 min", 96.0), ("hourly", 24.0), ("daily", 1.0))

GIB = 1024**3
FALLBACK_FIRE_DAYS = 364  # a sparse schedule (weekly, monthly) is counted over a year


class CostConfigError(ValueError):
    """CANDYBAR_COST_THRESHOLDS / CANDYBAR_COST_WINDOW_DAYS did not parse."""


def window_days(environ: Mapping[str, str] | None = None) -> int:
    raw = (environ if environ is not None else os.environ).get(WINDOW_DAYS_ENV, "").strip()
    if not raw:
        return DEFAULT_WINDOW_DAYS
    try:
        days = int(raw)
    except ValueError as exc:
        raise CostConfigError(f"{WINDOW_DAYS_ENV}={raw!r} is not an integer") from exc
    if not 1 <= days <= MAX_WINDOW_DAYS:
        raise CostConfigError(f"{WINDOW_DAYS_ENV} must be within 1..{MAX_WINDOW_DAYS}, got {days}")
    return days


def parse_thresholds(raw: str | None) -> dict[str, tuple[float, float]]:
    """The default thresholds with `raw` (a JSON object) laid over them."""
    merged = dict(DEFAULT_THRESHOLDS)
    if not raw or not raw.strip():
        return merged
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CostConfigError(f"{THRESHOLDS_ENV} is not JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise CostConfigError(f'{THRESHOLDS_ENV} must be an object like {{"cpu": [300, 1800]}}')
    for key, pair in doc.items():
        if key not in DIMENSIONS:
            raise CostConfigError(f"{THRESHOLDS_ENV}: unknown dimension {key!r}")
        ok = (
            isinstance(pair, list)
            and len(pair) == 2
            and all(isinstance(n, int | float) and not isinstance(n, bool) for n in pair)
        )
        if not ok or not 0 <= pair[0] < pair[1]:
            raise CostConfigError(f"{THRESHOLDS_ENV}: {key} must be [yellow, red] with 0 <= yellow < red")
        merged[key] = (float(pair[0]), float(pair[1]))
    return merged


def load_thresholds(
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, tuple[float, float]], str | None]:
    """The effective thresholds and, when the env override is bad, why it was ignored.

    A typo must not take the control plane down, so a bad override falls back
    to the defaults; the error is returned (and logged) so it stays visible.
    """
    raw = (environ if environ is not None else os.environ).get(THRESHOLDS_ENV)
    try:
        return parse_thresholds(raw), None
    except CostConfigError as exc:
        log.error("%s; using the default cost thresholds", exc)
        return dict(DEFAULT_THRESHOLDS), str(exc)


def tier_of(value: float, bounds: tuple[float, float]) -> str:
    yellow, red = bounds
    if value >= red:
        return RED
    if value >= yellow:
        return YELLOW
    return GREEN


# ---------------------------------------------------------------------------
# Fires per day (the projection's multiplier)


@functools.lru_cache(maxsize=1024)
def _fires_per_day(schedule: str, timezone: str, day: str) -> float:
    """Fires per day of `schedule` in `timezone`, counted by the one fire-time source.

    `day` is only the cache key (the UTC date), so a long-lived API process
    recomputes at most once a day per distinct schedule.
    """
    # Imported here: `dispatcher` pulls in the registry, and the registry store
    # imports this module for the window setting.
    from .dispatcher.firetimes import fire_times

    tz = resolve_timezone(timezone)
    end = datetime.fromisoformat(day).replace(tzinfo=UTC)
    week = sum(1 for _ in fire_times(schedule, tz, end - timedelta(days=DEFAULT_WINDOW_DAYS), end))
    if week >= DEFAULT_WINDOW_DAYS:
        return week / DEFAULT_WINDOW_DAYS
    # Under a fire a day: a sparse schedule (weekly, monthly) is counted over a year.
    year = sum(1 for _ in fire_times(schedule, tz, end - timedelta(days=FALLBACK_FIRE_DAYS), end))
    return year / FALLBACK_FIRE_DAYS


def fires_per_day(schedule: str, timezone: str, now: datetime) -> float:
    """How many times a day the schedule fires (0 for a cron that never fires)."""
    try:
        return _fires_per_day(schedule, timezone, now.astimezone(UTC).date().isoformat())
    except ValueError:  # a malformed row; the cost surface must not 500 on it
        log.warning("cost: cannot count fires of %r in %r", schedule, timezone)
        return 0.0


# ---------------------------------------------------------------------------
# Derivation


def _num(totals: Mapping[str, Any], key: str) -> float:
    value = totals.get(key)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def _avg(total: float, runs: float) -> float | None:
    return total / runs if runs > 0 else None


def _times(value: float | None, factor: float) -> float | None:
    return None if value is None else value * factor


def _rounded(value: float | None, places: int) -> float | None:
    return None if value is None else round(value, places)


def per_run_from_totals(totals: Mapping[str, Any], runner_kind: str) -> dict[str, float | None]:
    """Per-run averages; None for a dimension no run measured."""
    runs = _num(totals, "runs")
    metered = _num(totals, "cpuRuns") if runner_kind != "n8n" else 0.0
    mem_runs = _num(totals, "memRuns") if runner_kind != "n8n" else 0.0
    cpu_s = _avg(_num(totals, "cpuSeconds"), metered)
    gpu_total = _num(totals, "gpuSeconds")
    gpu_s = _avg(gpu_total, metered) if gpu_total > 0 else None
    mem_gib_h = _avg(_num(totals, "memGibSeconds") / 3600.0, mem_runs)
    mem_peak = _avg(_num(totals, "memPeakBytesSum"), mem_runs)
    tokens = _avg(_num(totals, "tokenSum"), runs) if _num(totals, "tokenRuns") > 0 else None
    usd = _avg(_num(totals, "usdSum"), runs) if _num(totals, "usdRuns") > 0 else None
    return {
        "runtimeS": _avg(_num(totals, "runtimeMs") / 1000.0, runs),
        "cpuS": cpu_s,
        "gpuS": gpu_s,
        "memPeakMb": _times(mem_peak, 1 / (1024 * 1024)),
        "memGibH": mem_gib_h,
        "tokens": tokens,
        "usd": usd,
    }


def per_day_from_per_run(per_run: Mapping[str, float | None], fires: float) -> dict[str, float | None]:
    return {
        "runtimeS": _times(per_run["runtimeS"], fires),
        "cpuS": _times(per_run["cpuS"], fires),
        "gpuS": _times(per_run["gpuS"], fires),
        "memGibH": _times(per_run["memGibH"], fires),
        "tokens": _times(per_run["tokens"], fires),
        "usd": _times(per_run["usd"], fires),
    }


_DAY_KEY = {"cpu": "cpuS", "gpu": "gpuS", "memory": "memGibH", "tokens": "tokens", "usd": "usd"}


def tiers_from_per_day(
    per_day: Mapping[str, float | None], thresholds: Mapping[str, tuple[float, float]]
) -> dict[str, str]:
    out: dict[str, str] = {}
    for dim in DIMENSIONS:
        value = per_day[_DAY_KEY[dim]]
        out[dim] = UNMEASURED if value is None else tier_of(value, thresholds[dim])
    return out


def overall(
    tiers: Mapping[str, str],
    per_day: Mapping[str, float | None],
    thresholds: Mapping[str, tuple[float, float]],
) -> tuple[str, str | None]:
    """The worst measured dimension's tier, and which dimension set it.

    Among dimensions sharing the worst tier, the one furthest through its own
    scale (value / red threshold) is the driver.
    """
    worst = max(tiers.values(), key=lambda t: TIER_RANK[t])
    if worst == UNMEASURED:
        return UNMEASURED, None
    tied = [d for d in DIMENSIONS if tiers[d] == worst]
    driver = max(
        tied, key=lambda d: (per_day[_DAY_KEY[d]] or 0.0) / thresholds[d][1]
    )
    return worst, driver


def build_cost(
    totals: Mapping[str, Any] | None,
    *,
    window_days: int,
    computed_at: datetime | None,
    schedule: str,
    timezone: str,
    runner_kind: str,
    now: datetime,
    thresholds: Mapping[str, tuple[float, float]] | None = None,
    fires: float | None = None,
) -> dict[str, Any]:
    """The `cost` object every candybar carries (never null).

    `totals` is None for a candybar the store has not aggregated yet (just
    registered, or before the first `candybar cost --refresh` after the 0010
    migration); it reads as `unmeasured` with a note, not as zero.
    """
    bounds = dict(DEFAULT_THRESHOLDS if thresholds is None else thresholds)
    totals = dict(totals or {})
    runs = int(_num(totals, "runs"))
    scheduled = fires_per_day(schedule, timezone, now) if fires is None else fires
    observed = runs / window_days if window_days else 0.0
    basis = "schedule"
    effective = scheduled
    if scheduled <= 0 and runs > 0:
        # A cron that never fires (manual-only): its runs are the only cost there is.
        effective, basis = observed, "observed"
    elif scheduled <= 0:
        basis = "none"

    per_run = per_run_from_totals(totals, runner_kind)
    per_day = per_day_from_per_run(per_run, effective)
    tiers = tiers_from_per_day(per_day, bounds)
    if runs == 0:
        tiers = {d: UNMEASURED for d in DIMENSIONS}
        per_day = dict.fromkeys(per_day)
        per_run = dict.fromkeys(per_run)
    tier, driver = overall(tiers, per_day, bounds)

    notes: list[str] = []
    if computed_at is None:
        notes.append("not aggregated yet; run `candybar cost --refresh`")
    elif runs == 0:
        notes.append(f"no runs in the last {window_days} days")
    else:
        if runner_kind == "n8n":
            notes.append("n8n runs the workflow itself: CPU, GPU and memory are not metered")
        elif _num(totals, "cpuRuns") == 0:
            notes.append("CPU/memory not metered for any run in the window")
        if _num(totals, "tokenRuns") == 0:
            notes.append("no run reported tokens (the token tier is unmeasured, not green)")
        elif _num(totals, "tokenRuns") < runs:
            notes.append(f"{int(_num(totals, 'tokenRuns'))} of {runs} runs reported tokens")
        if _num(totals, "usdRuns") == 0:
            notes.append("no run reported cost_usd")
        if (
            basis == "schedule"
            and effective > 0
            and observed < effective / 2
            and runs >= 3
            and window_days >= 2
        ):
            notes.append(
                f"{observed:.1f} runs/day observed vs {effective:.1f} scheduled "
                "(overlap skips, a paused spell, or a young schedule): projection is the schedule's"
            )
    return {
        "tier": tier,
        "driver": driver,
        "windowDays": window_days,
        "runs": runs,
        "failedRuns": int(_num(totals, "failedRuns")),
        "firesPerDay": _rounded(effective, 4),
        "firesPerDayBasis": basis,
        "observedRunsPerDay": _rounded(observed, 4),
        "computedAt": None if computed_at is None else computed_at.astimezone(UTC).isoformat(),
        "perRun": {k: _rounded(v, 6) for k, v in per_run.items()},
        "perDay": {k: _rounded(v, 6) for k, v in per_day.items()},
        "measuredRuns": {
            "cpu": int(_num(totals, "cpuRuns")) if runner_kind != "n8n" else 0,
            "gpu": int(_num(totals, "cpuRuns")) if _num(totals, "gpuSeconds") > 0 else 0,
            "memory": int(_num(totals, "memRuns")) if runner_kind != "n8n" else 0,
            "tokens": int(_num(totals, "tokenRuns")),
            "usd": int(_num(totals, "usdRuns")),
        },
        "tiers": tiers,
        "notes": notes,
    }


def what_ifs(
    cost: Mapping[str, Any], thresholds: Mapping[str, tuple[float, float]]
) -> list[dict[str, Any]]:
    """The same per-run averages at other cadences: how the tier would move."""
    per_run = cost["perRun"]
    out = []
    for label, fires in WHAT_IF_CADENCES:
        per_day = per_day_from_per_run(per_run, fires)
        tiers = tiers_from_per_day(per_day, thresholds)
        tier, driver = overall(tiers, per_day, thresholds)
        out.append(
            {
                "label": label,
                "firesPerDay": fires,
                "tier": tier,
                "driver": driver,
                "perDay": {k: _rounded(v, 6) for k, v in per_day.items()},
            }
        )
    return out


def sort_key(cost: Mapping[str, Any], thresholds: Mapping[str, tuple[float, float]]) -> tuple:
    """Most expensive first: tier, then how far through the driving dimension's red bar."""
    tier = cost["tier"]
    driver = cost["driver"]
    progress = 0.0
    if driver is not None:
        value = cost["perDay"][_DAY_KEY[driver]] or 0.0
        progress = value / thresholds[driver][1]
    return (-TIER_RANK[tier], -progress)


# ---------------------------------------------------------------------------
# Display (shared by the CLI and the docs' examples)


def format_number(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value / 1000:.0f}k"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    if value >= 100:
        return f"{value:.0f}"
    if value >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


def format_figure(dimension: str, value: float | None) -> str:
    """`3.8k core-s/day`, `$0.42/day`, ... or `-` for an unmeasured dimension."""
    if value is None:
        return "-"
    if dimension == "usd":
        return f"${value:.2f}/day"
    return f"{format_number(value)} {UNITS[dimension]}"


def headline(cost: Mapping[str, Any]) -> str:
    """The one figure the `ls` COST column shows next to the tier dot."""
    if cost["tier"] == UNMEASURED or cost["driver"] is None:
        return "unmeasured"
    driver = cost["driver"]
    return format_figure(driver, cost["perDay"][_DAY_KEY[driver]])
