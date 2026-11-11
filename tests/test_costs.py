"""The cost model (`candybar.costs`): tiers, projection, honesty about what was measured.

Pure: no database, no clock. The SQL that produces the totals is proven on
live Postgres in tests/test_cost_integration.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from candybar import costs
from candybar.costs import (
    DEFAULT_THRESHOLDS,
    CostConfigError,
    build_cost,
    format_figure,
    headline,
    parse_thresholds,
    sort_key,
    tier_of,
    what_ifs,
    window_days,
)

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def totals(**kw: Any) -> dict[str, Any]:
    """Window totals for 100 runs of 10 s that each used 2 core-seconds and 100 MB."""
    base = {
        "runs": 100,
        "failedRuns": 0,
        "runtimeMs": 100 * 10_000,
        "cpuRuns": 100,
        "cpuSeconds": 200.0,
        "gpuSeconds": 0.0,
        "memRuns": 100,
        "memPeakBytesSum": 100 * 100 * 1024 * 1024,
        "memPeakBytesMax": 100 * 1024 * 1024,
        "memGibSeconds": 100 * (100 / 1024) * 10,
        "tokenSum": 0,
        "tokenRuns": 0,
        "usdSum": 0,
        "usdRuns": 0,
    }
    return {**base, **kw}


def cost(t: dict[str, Any] | None, *, fires: float = 24.0, kind: str = "exec", **kw: Any) -> dict[str, Any]:
    return build_cost(
        t,
        window_days=7,
        computed_at=NOW,
        schedule="0 * * * *",
        timezone="UTC",
        runner_kind=kind,
        now=NOW,
        thresholds=DEFAULT_THRESHOLDS,
        fires=fires,
        **kw,
    )


# --- thresholds and tiers ---------------------------------------------------


def test_tier_of_is_inclusive_at_each_bar() -> None:
    assert tier_of(299.9, (300, 1800)) == "green"
    assert tier_of(300, (300, 1800)) == "yellow"
    assert tier_of(1799.9, (300, 1800)) == "yellow"
    assert tier_of(1800, (300, 1800)) == "red"


def test_thresholds_override_merges_over_the_defaults() -> None:
    merged = parse_thresholds('{"cpu": [10, 20]}')
    assert merged["cpu"] == (10.0, 20.0)
    assert merged["tokens"] == DEFAULT_THRESHOLDS["tokens"]
    assert parse_thresholds(None) == DEFAULT_THRESHOLDS
    assert parse_thresholds("  ") == DEFAULT_THRESHOLDS


@pytest.mark.parametrize(
    "raw",
    ["not json", "[1, 2]", '{"disk": [1, 2]}', '{"cpu": [5]}', '{"cpu": [20, 10]}', '{"cpu": [-1, 4]}',
     '{"cpu": ["a", "b"]}', '{"cpu": [true, 4]}'],
)
def test_bad_thresholds_are_refused_not_guessed(raw: str) -> None:
    with pytest.raises(CostConfigError):
        parse_thresholds(raw)


def test_a_bad_override_falls_back_to_defaults_and_says_why() -> None:
    loaded, error = costs.load_thresholds({costs.THRESHOLDS_ENV: "{oops"})
    assert loaded == DEFAULT_THRESHOLDS and error and "not JSON" in error
    assert costs.load_thresholds({}) == (DEFAULT_THRESHOLDS, None)


def test_window_days_reads_the_env_within_bounds() -> None:
    assert window_days({}) == 7
    assert window_days({costs.WINDOW_DAYS_ENV: "14"}) == 14
    for bad in ("0", "91", "x"):
        with pytest.raises(CostConfigError):
            window_days({costs.WINDOW_DAYS_ENV: bad})


# --- build_cost -------------------------------------------------------------


def test_per_run_and_projection_to_the_schedule() -> None:
    c = cost(totals(), fires=24.0)  # 2 core-s per run, 24 runs a day
    assert c["perRun"]["cpuS"] == 2.0 and c["perRun"]["runtimeS"] == 10.0
    assert c["perRun"]["memPeakMb"] == pytest.approx(100.0)
    assert c["perDay"]["cpuS"] == 48.0
    assert c["perDay"]["memGibH"] == pytest.approx(100 / 1024 * 10 / 3600 * 24, rel=1e-3)
    assert c["firesPerDay"] == 24.0 and c["firesPerDayBasis"] == "schedule"
    assert c["observedRunsPerDay"] == pytest.approx(100 / 7, abs=1e-3)
    assert c["tiers"]["cpu"] == "green" and c["tiers"]["memory"] == "green"
    assert c["tier"] == "green" and c["driver"] in {"cpu", "memory"}


def test_the_overall_tier_is_the_worst_measured_dimension_and_names_it() -> None:
    # 100 core-s a run at 24/day = 2400 core-s/day: red on CPU, everything else green.
    c = cost(totals(cpuSeconds=10_000.0), fires=24.0)
    assert c["tier"] == "red" and c["driver"] == "cpu"
    assert c["tiers"]["cpu"] == "red"
    # Yellow CPU (5 core-s x 24 = 120 is green; 15 x 24 = 360 is yellow) loses to a red token spend.
    c = cost(totals(cpuSeconds=1500.0, tokenSum=100 * 200_000, tokenRuns=100), fires=24.0)
    assert c["tiers"]["cpu"] == "yellow" and c["tiers"]["tokens"] == "red"
    assert c["tier"] == "red" and c["driver"] == "tokens"


def test_ties_pick_the_dimension_furthest_through_its_own_scale() -> None:
    # Both yellow at 24 runs a day: cpu 1500/day (83% of the way to red) beats tokens 300k/day (15%).
    c = cost(totals(cpuSeconds=6250.0, tokenSum=1_250_000, tokenRuns=100), fires=24.0)
    assert c["perDay"]["cpuS"] == 1500.0 and c["perDay"]["tokens"] == 300_000.0
    assert c["tiers"]["cpu"] == c["tiers"]["tokens"] == "yellow"
    assert c["tier"] == "yellow" and c["driver"] == "cpu"


def test_a_candybar_with_no_runs_is_unmeasured_not_green() -> None:
    c = cost(totals(runs=0, cpuRuns=0, memRuns=0, cpuSeconds=0, runtimeMs=0))
    assert c["tier"] == "unmeasured" and c["driver"] is None
    assert set(c["tiers"].values()) == {"unmeasured"}
    assert all(v is None for v in c["perRun"].values()) and all(v is None for v in c["perDay"].values())
    assert any("no runs in the last 7 days" in n for n in c["notes"])
    assert headline(c) == "unmeasured"


def test_a_candybar_never_aggregated_says_so() -> None:
    c = build_cost(
        None, window_days=7, computed_at=None, schedule="0 * * * *", timezone="UTC",
        runner_kind="exec", now=NOW, fires=24.0,
    )
    assert c["tier"] == "unmeasured" and c["computedAt"] is None
    assert any("candybar cost --refresh" in n for n in c["notes"])


def test_no_run_reported_tokens_is_unmeasured_never_green() -> None:
    c = cost(totals(tokenSum=0, tokenRuns=0))
    assert c["tiers"]["tokens"] == "unmeasured" and c["perDay"]["tokens"] is None
    assert c["measuredRuns"]["tokens"] == 0
    assert any("no run reported tokens" in n for n in c["notes"])
    assert c["tiers"]["cpu"] == "green"  # the other dimensions still count


def test_partial_token_reporting_is_disclosed_and_averaged_over_all_runs() -> None:
    # 10 of 100 runs reported 1M tokens each: per run 100k, per day (24 runs) 2.4M = red.
    c = cost(totals(tokenSum=10_000_000, tokenRuns=10), fires=24.0)
    assert c["perRun"]["tokens"] == 100_000.0 and c["perDay"]["tokens"] == 2_400_000.0
    assert c["tiers"]["tokens"] == "red"
    assert any("10 of 100 runs reported tokens" in n for n in c["notes"])


def test_usd_counts_only_when_a_run_reported_cost() -> None:
    assert cost(totals())["tiers"]["usd"] == "unmeasured"
    c = cost(totals(usdSum=5.0, usdRuns=100), fires=24.0)  # $0.05 a run, $1.20 a day
    assert c["tiers"]["usd"] == "red" and c["perDay"]["usd"] == pytest.approx(1.2)


def test_gpu_is_unmeasured_until_something_used_it() -> None:
    assert cost(totals())["tiers"]["gpu"] == "unmeasured"  # 0 may just mean no nvidia-smi
    c = cost(totals(gpuSeconds=1000.0), fires=24.0)  # 10 gpu-s a run, 240 a day
    assert c["tiers"]["gpu"] == "green" and c["perDay"]["gpuS"] == 240.0


def test_n8n_cpu_and_memory_are_never_counted() -> None:
    c = cost(totals(), kind="n8n")
    assert c["tiers"]["cpu"] == c["tiers"]["memory"] == "unmeasured"
    assert c["perRun"]["cpuS"] is None and c["perRun"]["memPeakMb"] is None
    assert any("n8n runs the workflow itself" in n for n in c["notes"])
    assert c["tier"] == "unmeasured"  # nothing else measured


def test_runs_the_runner_could_not_meter_leave_cpu_unmeasured() -> None:
    c = cost(totals(cpuRuns=0, cpuSeconds=0, memRuns=0, memPeakBytesSum=0, memGibSeconds=0))
    assert c["tiers"]["cpu"] == c["tiers"]["memory"] == "unmeasured"
    assert any("not metered" in n for n in c["notes"])


def test_a_schedule_that_never_fires_projects_from_observed_runs() -> None:
    c = cost(totals(), fires=0.0)
    assert c["firesPerDayBasis"] == "observed"
    assert c["firesPerDay"] == pytest.approx(100 / 7, abs=1e-3)
    assert c["perDay"]["cpuS"] == pytest.approx(2.0 * 100 / 7, abs=1e-2)
    idle = cost(totals(runs=0, cpuRuns=0, cpuSeconds=0), fires=0.0)
    assert idle["firesPerDayBasis"] == "none"


def test_a_projection_far_above_what_ran_is_flagged() -> None:
    c = cost(totals(runs=5, cpuRuns=5, cpuSeconds=10.0, memRuns=5), fires=16.0)
    assert any("runs/day observed" in n and "scheduled" in n for n in c["notes"])


# --- what-ifs and ranking ---------------------------------------------------


def test_what_ifs_replay_the_per_run_average_at_other_cadences() -> None:
    c = cost(totals(cpuSeconds=2000.0), fires=288.0)  # 20 core-s a run
    variants = {v["label"]: v for v in what_ifs(c, DEFAULT_THRESHOLDS)}
    assert variants["daily"]["perDay"]["cpuS"] == 20.0 and variants["daily"]["tier"] == "green"
    assert variants["hourly"]["perDay"]["cpuS"] == 480.0 and variants["hourly"]["tier"] == "yellow"
    assert variants["every 15 min"]["perDay"]["cpuS"] == 1920.0
    assert variants["every 15 min"]["tier"] == "red" and variants["every 15 min"]["driver"] == "cpu"


def test_what_ifs_of_an_unmeasured_candybar_stay_unmeasured() -> None:
    c = cost(totals(runs=0, cpuRuns=0, cpuSeconds=0))
    assert {v["tier"] for v in what_ifs(c, DEFAULT_THRESHOLDS)} == {"unmeasured"}


def test_sort_puts_red_before_yellow_before_green_before_unmeasured() -> None:
    red = cost(totals(cpuSeconds=10_000.0), fires=24.0)
    worse_red = cost(totals(cpuSeconds=50_000.0), fires=24.0)
    yellow = cost(totals(cpuSeconds=1500.0), fires=24.0)
    green = cost(totals(), fires=24.0)
    idle = cost(totals(runs=0, cpuRuns=0, cpuSeconds=0))
    order = sorted([idle, green, yellow, red, worse_red], key=lambda c: sort_key(c, DEFAULT_THRESHOLDS))
    assert order == [worse_red, red, yellow, green, idle]


# --- fires per day (the real fire-time source) -----------------------------


@pytest.mark.parametrize(
    ("schedule", "expected"),
    [("*/5 * * * *", 288.0), ("0 * * * *", 24.0), ("12 3 * * *", 1.0), ("0 9 * * 1", 52 / 364),
     ("0 3 1 * *", 12 / 364)],
)
def test_fires_per_day_counts_the_real_schedule(schedule: str, expected: float) -> None:
    assert costs.fires_per_day(schedule, "UTC", NOW) == pytest.approx(expected, rel=0.03)


def test_fires_per_day_follows_the_timezone_across_dst() -> None:
    # A fixed-hour 02:30 fires once a day in New York, even in the week the clocks went back
    # (2026-11-01, when 02:30 happens twice); a wildcard-hour job keeps its real-time cadence.
    went_back = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    assert costs.fires_per_day("30 2 * * *", "America/New_York", went_back) == pytest.approx(1.0)
    # 168 real hours, 168 hourly fires: the repeated local hour fires twice, so the cadence holds.
    assert costs.fires_per_day("0 * * * *", "America/New_York", went_back) == pytest.approx(24.0)


def test_a_cron_that_never_fires_or_is_malformed_counts_zero() -> None:
    assert costs.fires_per_day("0 0 31 2 *", "UTC", NOW) == 0.0
    assert costs.fires_per_day("not a cron", "UTC", NOW) == 0.0


# --- display ----------------------------------------------------------------


def test_figures_read_with_their_units() -> None:
    assert format_figure("cpu", 3768.2) == "3.8k core-s/day"
    assert format_figure("cpu", 4.123) == "4.12 core-s/day"
    assert format_figure("tokens", 10_000_000) == "10.0M tokens/day"
    assert format_figure("usd", 0.4231) == "$0.42/day"
    assert format_figure("memory", None) == "-"


def test_headline_is_the_driving_figure() -> None:
    c = cost(totals(cpuSeconds=10_000.0), fires=24.0)  # 100 core-s a run x 24
    assert headline(c) == "2.4k core-s/day"
