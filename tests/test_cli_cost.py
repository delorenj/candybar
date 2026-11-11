"""`candybar ls`/`show`/`cost` print the cost: tier mark, driving figure, breakdown, ranking.

Same urlopen stand-in as tests/test_cli_control.py; the cost objects are the
real ones, built by `candybar.costs.build_cost`.
"""

from __future__ import annotations

import json
import re
from typing import Any

from typer.testing import CliRunner

from candybar.cli.main import app
from test_cli_control import (  # noqa: F401  (fixtures)
    ID_ALPHA,
    ID_BETA,
    ID_SMOKE,
    FakeApi,
    cb,
    cost_object,
    cost_route,
    fake,
    frozen_now,
    invoke_cli,
)

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def totals(cpu_seconds: float = 2000.0, runs: int = 100, **kw: Any) -> dict[str, Any]:
    return {
        "runs": runs, "failedRuns": 1, "runtimeMs": runs * 10_000,
        "cpuRuns": runs, "cpuSeconds": cpu_seconds, "gpuSeconds": 0.0,
        "memRuns": runs, "memPeakBytes": 0, "memPeakBytesSum": runs * 48 * 1024 * 1024,
        "memPeakBytesMax": 48 * 1024 * 1024, "memGibSeconds": runs * (48 / 1024) * 10,
        "tokenSum": 0, "tokenRuns": 0, "usdSum": 0, "usdRuns": 0, **kw,
    }


HOT = cb("hot-job", ID_ALPHA, cost=cost_object("*/5 * * * *", 288.0, **totals(2000.0)))  # 5760 core-s/day
WARM = cb("warm-job", ID_SMOKE, cost=cost_object("0 * * * *", 24.0, **totals(2000.0)))  # 480
COLD = cb("cold-job", ID_BETA, enabled=False, cost=cost_object("12 3 * * *", 1.0, **totals(100.0)))  # 1
IDLE_ID = "01900000-0000-7000-8000-0000000000dd"
IDLE = cb("idle-job", IDLE_ID, cost=cost_object("0 * * * *"))  # no runs yet


def ranking(*candybars: dict[str, Any]) -> dict[str, Any]:
    return {
        "windowDays": 7,
        "configError": None,
        "thresholds": cost_route("x", "x", HOT["cost"])["thresholds"],
        "candybars": [
            {
                "id": c["id"], "name": c["name"], "schedule": c["schedule"], "timezone": "UTC",
                "enabled": c["enabled"], "archived": False, "noise": c["noise"], "tags": c["tags"],
                "cost": c["cost"],
            }
            for c in candybars
        ],
    }


# --- ls ----------------------------------------------------------------------------


def test_ls_shows_a_cost_column_with_tier_tag_and_driving_figure(fake: FakeApi) -> None:  # noqa: F811
    fake.on("GET", "/candybars", [HOT, WARM, IDLE])
    out = invoke_cli(["ls"]).output
    lines = {ln.split()[0]: ln for ln in out.splitlines()[1:-1]}
    assert out.splitlines()[0].split()[:5] == ["NAME", "SCHEDULE", "TZ", "FLAGS", "COST"]
    assert "● RED" in lines["hot-job"] and "5.8k core-s/day" in lines["hot-job"]
    assert "● YEL" in lines["warm-job"] and "480 core-s/day" in lines["warm-job"]
    assert "○ unmeasured" in lines["idle-job"]


def test_ls_columns_stay_aligned_with_colour_on(fake: FakeApi) -> None:  # noqa: F811
    fake.on("GET", "/candybars", [HOT, WARM, IDLE])
    result = CliRunner().invoke(app, ["ls"], color=True)
    assert "\x1b[31m" in result.output  # red for the red tier
    plain = ANSI.sub("", result.output).splitlines()
    header, rows = plain[0], plain[1:-1]
    tags_at = header.index("TAGS")
    for row in rows:
        assert row[tags_at - 1] == " " and row[tags_at:].strip()  # TAGS/ID start in one column
    assert all(row.index(row.split()[-1]) == header.index("ID") for row in rows)


def test_ls_json_carries_the_cost_object(fake: FakeApi) -> None:  # noqa: F811
    fake.on("GET", "/candybars", [HOT])
    body = json.loads(invoke_cli(["ls", "--json"]).output)
    assert body[0]["cost"]["tier"] == "red" and body[0]["cost"]["driver"] == "cpu"


def test_ls_of_an_older_api_without_cost_still_lists(fake: FakeApi) -> None:  # noqa: F811
    old = {k: v for k, v in HOT.items() if k != "cost"}
    fake.on("GET", "/candybars", [old])
    result = invoke_cli(["ls"])
    assert result.exit_code == 0 and "hot-job" in result.output


# --- cost ranking ---------------------------------------------------------------------


def test_cost_lists_most_expensive_first_and_hides_paused(fake: FakeApi) -> None:  # noqa: F811
    fake.on("GET", "/costs", ranking(HOT, WARM, COLD, IDLE))
    result = invoke_cli(["cost"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].split()[:3] == ["COST", "NAME", "SCHEDULE"]
    names = [n for ln in lines[1:-1] for n in ("hot-job", "warm-job", "cold-job", "idle-job") if n in ln]
    assert names == ["hot-job", "warm-job", "idle-job"]  # cold-job is paused
    assert "● RED" in lines[1] and "● YEL" in lines[2]
    assert "5.8k" in lines[1]  # the per-dimension cpu column
    assert "3 shown of 3" in lines[-1] and "1 paused/archived hidden; --all to include" in lines[-1]
    everything = invoke_cli(["cost", "--all"]).output
    assert "cold-job" in everything and "4 shown of 4" in everything


def test_cost_limit(fake: FakeApi) -> None:  # noqa: F811
    fake.on("GET", "/costs", ranking(HOT, WARM, IDLE))
    out = invoke_cli(["cost", "--limit", "1"]).output
    assert "hot-job" in out and "warm-job" not in out and "1 shown of 3" in out
    assert "warm-job" in invoke_cli(["cost", "--limit", "0"]).output


def test_cost_json_is_the_view(fake: FakeApi) -> None:  # noqa: F811
    fake.on("GET", "/costs", ranking(HOT, WARM))
    body = json.loads(invoke_cli(["cost", "--json", "--limit", "1"]).output)
    assert [c["name"] for c in body["candybars"]] == ["hot-job"] and body["windowDays"] == 7


def test_cost_refresh_recomputes_first_then_reads(fake: FakeApi) -> None:  # noqa: F811
    fake.on("POST", "/costs/refresh", {"refreshed": 90, "windowDays": 7})
    fake.on("GET", "/costs", ranking(HOT))
    result = invoke_cli(["cost", "--refresh"])
    assert result.exit_code == 0, result.output
    assert "refreshed 90 candybar cost row(s) over 7 days" in result.output
    assert [(c.method, c.path) for c in fake.calls] == [("POST", "/costs/refresh"), ("GET", "/costs")]


def test_cost_refresh_json_keeps_stdout_parseable(fake: FakeApi) -> None:  # noqa: F811
    fake.on("POST", "/costs/refresh", {"refreshed": 1, "windowDays": 7})
    fake.on("GET", "/costs", ranking(HOT))
    result = invoke_cli(["cost", "--refresh", "--json"])
    assert json.loads(result.stdout)["candybars"][0]["name"] == "hot-job"


def test_cost_warns_about_a_bad_config(fake: FakeApi) -> None:  # noqa: F811
    view = {**ranking(HOT), "configError": "CANDYBAR_COST_THRESHOLDS is not JSON"}
    fake.on("GET", "/costs", view)
    assert "warning: CANDYBAR_COST_THRESHOLDS is not JSON" in invoke_cli(["cost"]).output


def test_cost_api_down_exits_1(fake: FakeApi) -> None:  # noqa: F811
    import urllib.error

    fake.on("GET", "/costs", urllib.error.URLError("refused"))
    result = invoke_cli(["cost"])
    assert result.exit_code == 1 and "unreachable" in result.output


# --- one candybar ------------------------------------------------------------------------


def test_cost_of_one_candybar_breaks_down_run_day_tiers_and_what_ifs(fake: FakeApi, frozen_now: Any) -> None:  # noqa: F811
    listing = fake.on("GET", "/candybars", [HOT])
    listing.on("GET", f"/candybars/{ID_ALPHA}/cost", cost_route("hot-job", ID_ALPHA, HOT["cost"]))
    result = invoke_cli(["cost", "hot-job"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "● RED 5.8k core-s/day (cpu)" in out and "100 runs" in out
    assert "per run" in out and "20.0 core-s" in out and "48 MB peak" in out
    assert "per day" in out and "288 runs/day scheduled" in out
    assert "tokens ○" in out  # no run reported tokens: hollow, never a green dot
    assert "if every 15 min" in out and "if hourly" in out and "if daily" in out
    daily = next(ln for ln in out.splitlines() if "if daily" in ln)
    assert "● GRN 20.0 core-s/day" in daily
    assert "no run reported tokens" in out
    assert "bars (yellow / red per day): cpu 300 / 1800 core-s/day" in out


def test_show_prints_the_cost_block(fake: FakeApi, frozen_now: Any) -> None:  # noqa: F811
    listing = fake.on("GET", "/candybars", [HOT])
    listing.on("GET", f"/candybars/{ID_ALPHA}", {**HOT, "totalInvoked": 100, "totalSuccessful": 99})
    listing.on("GET", f"/candybars/{ID_ALPHA}/runner", {"runner": None, "entrypoint": "/x"})
    listing.on("GET", f"/candybars/{ID_ALPHA}/cost", cost_route("hot-job", ID_ALPHA, HOT["cost"]))
    listing.on("GET", f"/candybars/{ID_ALPHA}/invocations", {"candybarId": ID_ALPHA, "invocations": []})
    out = invoke_cli(["show", "hot-job"]).output
    assert "cost  ● RED 5.8k core-s/day (cpu)" in out
    assert "7d window, 100 runs, 1 failed" in out
    assert "if hourly" in out


def test_show_json_adds_the_what_ifs(fake: FakeApi) -> None:  # noqa: F811
    listing = fake.on("GET", "/candybars", [HOT])
    listing.on("GET", f"/candybars/{ID_ALPHA}", {**HOT, "totalInvoked": 1, "totalSuccessful": 1})
    listing.on("GET", f"/candybars/{ID_ALPHA}/runner", {"runner": None, "entrypoint": "/x"})
    listing.on("GET", f"/candybars/{ID_ALPHA}/cost", cost_route("hot-job", ID_ALPHA, HOT["cost"]))
    listing.on("GET", f"/candybars/{ID_ALPHA}/invocations", {"candybarId": ID_ALPHA, "invocations": []})
    body = json.loads(invoke_cli(["show", "hot-job", "--json"]).output)
    assert body["cost"]["tier"] == "red"
    assert [v["label"] for v in body["whatIf"]] == ["every 15 min", "hourly", "daily"]
    assert body["costThresholds"]["cpu"]["red"] == 1800.0


def test_a_candybar_with_no_runs_shows_why_it_is_unmeasured(fake: FakeApi, frozen_now: Any) -> None:  # noqa: F811
    listing = fake.on("GET", "/candybars", [IDLE])
    listing.on("GET", f"/candybars/{IDLE_ID}/cost", cost_route("idle-job", IDLE_ID, IDLE["cost"]))
    out = invoke_cli(["cost", "idle-job"]).output
    assert "○ unmeasured" in out and "never aggregated" in out
    assert "per run" not in out and "if hourly" not in out  # nothing to project
