"""Tests for the operator control CLI (`candybar ls|show|next|schedule|history|
pause|resume|skip|edit|tail`) and `candybar run --wait`.

Two layers, both stubbing the network at urllib.request.urlopen like
tests/test_cli_run.py:

* unit tests against a routing fake (`FakeApi`) with hand-made but
  real-shaped payloads, for every branch of the commands;
* contract tests (`test_contract_*`) that route the CLI's requests into the
  REAL FastAPI app (registry functions monkeypatched on candybar.api.app, the
  rig of tests/test_api_skips.py) and the REAL ScheduleStream hub, so field
  names are checked against the shapes the API actually produces.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import urllib.error
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import candybar.api.app as app_module
import candybar.api.stream as stream_module
from candybar.api.stream import ScheduleStream
from candybar.cli import control
from candybar.cli.main import app
from candybar.events import (
    build_candybar_completed,
    build_candybar_failed,
    build_candybar_invoked,
    build_candybar_skipped,
    build_candybar_updated,
)
from candybar.events.vocabulary import tick_id_for_minute
from candybar.migration.client import ApiError, CandybarApi
from test_api_definitions import Rig, rig  # noqa: F401  (rig is a fixture)
from test_api_skips import NOW, skips_rig  # noqa: F401
from test_api_skips import client as skips_client

BASE = "http://api.test:8700"

ID_ALPHA = "01900000-0000-7000-8000-00000000000a"
ID_BETA = "01900000-0000-7000-8000-00000000000b"
ID_SMOKE = "01900000-0000-7000-8000-00000000000c"
ID_MISSING = "01900000-0000-7000-8000-00000000dead"
INV_ID = "0b5c6d1e-1111-4222-8333-444455556666"

# "now" for the unit tests: Monday 2026-10-26 06:30:00 UTC.
UNIT_NOW = datetime(2026, 10, 26, 6, 30, tzinfo=UTC)


def z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Fixtures and fakes.


@pytest.fixture(autouse=True)
def api_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANDYBAR_API_URL", BASE)


@pytest.fixture(autouse=True)
def local_tz(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """The CLI prints/reads naive times in this machine's zone: pin it."""

    def use(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    use("UTC")
    yield use
    monkeypatch.undo()
    time.tzset()


@pytest.fixture()
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr(control, "_now", lambda: UNIT_NOW)
    return UNIT_NOW


class _Json:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class SseResponse:
    """A urlopen() result for the SSE stream: iterates bytes lines."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._lines)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def problem(status: int, title: str, detail: str) -> urllib.error.HTTPError:
    body = json.dumps({"type": "about:blank", "title": title, "status": status, "detail": detail})
    return urllib.error.HTTPError(
        url="http://x", code=status, msg=title, hdrs=None, fp=io.BytesIO(body.encode())
    )


class Seq:
    """Successive responses for one route; the last one repeats."""

    def __init__(self, *items: Any) -> None:
        self.items = list(items)

    def next(self) -> Any:
        return self.items.pop(0) if len(self.items) > 1 else self.items[0]


@dataclass
class Call:
    method: str
    path: str
    query: dict[str, list[str]]
    body: Any
    headers: dict[str, str]


class Unrouted(BaseException):  # BaseException: CliRunner must not swallow it
    pass


class FakeApi:
    """urlopen stand-in routing on (METHOD, path); records every request."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Any] = {}
        self.calls: list[Call] = []

    def on(self, method: str, path: str, response: Any) -> Self:
        self.routes[(method, path)] = response
        return self

    def __call__(self, request: Any, timeout: float = 0) -> Any:
        split = urlsplit(request.full_url)
        method = request.get_method()
        call = Call(
            method,
            split.path,
            parse_qs(split.query),
            json.loads(request.data) if request.data else None,
            {k.lower(): v for k, v in request.header_items()},
        )
        self.calls.append(call)
        if (method, split.path) not in self.routes:
            raise Unrouted(f"{method} {split.path}")
        response = self.routes[(method, split.path)]
        if isinstance(response, Seq):
            response = response.next()
        if callable(response):
            response = response(call)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, SseResponse):
            return response
        return _Json(response)

    def of(self, method: str, path: str) -> list[Call]:
        return [c for c in self.calls if (c.method, c.path) == (method, path)]

    def writes(self) -> list[Call]:
        return [c for c in self.calls if c.method != "GET"]


def cb(name: str, cid: str, **kw: Any) -> dict[str, Any]:
    """A candybar in the contract definition shape (GET /candybars items)."""
    return {
        "id": cid,
        "name": name,
        "description": kw.pop("description", ""),
        "tags": kw.pop("tags", []),
        "schedule": kw.pop("schedule", "*/5 * * * *"),
        "timezone": kw.pop("timezone", "UTC"),
        "noise": kw.pop("noise", False),
        "enabled": kw.pop("enabled", True),
        "createdAt": "2026-10-25T00:00:00+00:00",
        **kw,
    }


ALPHA = cb("alpha-report", ID_ALPHA, tags=["ops"], schedule="*/5 * * * *")
BETA = cb("beta-sync", ID_BETA, tags=["ops", "sync"], schedule="0 * * * *", enabled=False)
SMOKE = cb("smoke-ping", ID_SMOKE, tags=["smoke"], schedule="* * * * *", noise=True)
ALL = [ALPHA, BETA, SMOKE]


def inv(**kw: Any) -> dict[str, Any]:
    """An invocation row in invocation_row_json's shape."""
    return {
        "id": kw.pop("id", INV_ID),
        "candybarId": kw.pop("candybarId", ID_ALPHA),
        "trigger": kw.pop("trigger", "manual"),
        "tickId": None,
        "idempotencyKey": "k",
        "result": kw.pop("result", "success"),
        "exitCode": kw.pop("exitCode", 0),
        "errorMessage": kw.pop("errorMessage", None),
        "stderrTail": kw.pop("stderrTail", None),
        "runtimeMs": kw.pop("runtimeMs", 1500),
        "memoryPeakBytes": kw.pop("memoryPeakBytes", 2048),
        "cpuLoad": kw.pop("cpuLoad", 0.5),
        "gpuLoad": 0.0,
        "tokenSpend": kw.pop("tokenSpend", 0),
        "costUsd": None,
        "startedAt": kw.pop("startedAt", "2026-10-26T06:00:00+00:00"),
        "finishedAt": kw.pop("finishedAt", "2026-10-26T06:00:01+00:00"),
        **kw,
    }


def sched_row(cand: dict[str, Any], fire: datetime, state: str = "upcoming", **kw: Any) -> dict[str, Any]:
    """A /schedule row (lastResult omitted unless given)."""
    row: dict[str, Any] = {
        "candybarId": cand["id"],
        "name": cand["name"],
        "schedule": cand["schedule"],
        "fireTime": fire.astimezone(UTC).isoformat(),
        "state": state,
    }
    if "lastResult" in kw:
        row["lastResult"] = kw["lastResult"]
    row["skipped"] = kw.get("skipped", False)
    row["noise"] = cand["noise"]
    return row


def invoke_cli(args: list[str]) -> Any:
    return CliRunner().invoke(app, args)


@pytest.fixture()
def fake() -> Iterator[FakeApi]:
    api = FakeApi()
    with patch("urllib.request.urlopen", side_effect=api):
        yield api


@pytest.fixture()
def listing(fake: FakeApi) -> FakeApi:
    """The registry listing every name lookup reads."""
    return fake.on("GET", "/candybars", ALL)


def at_minute(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 26, hour, minute, tzinfo=UTC)


# ---------------------------------------------------------------------------
# ls


def test_ls_hides_paused_unless_all(listing: FakeApi) -> None:
    result = invoke_cli(["ls"])
    assert result.exit_code == 0, result.output
    assert "alpha-report" in result.output and "smoke-ping" in result.output
    assert "beta-sync" not in result.output
    assert "2 shown of 3 (1 paused hidden; --all to include)" in result.output
    assert ID_ALPHA in result.output  # the id column

    everything = invoke_cli(["ls", "--all"])
    assert "beta-sync" in everything.output and "paused" in everything.output
    assert "3 shown of 3" in everything.output and "hidden" not in everything.output
    assert "beta-sync" in invoke_cli(["ls", "-a"]).output


def test_ls_marks_noise_flag(listing: FakeApi) -> None:
    line = next(ln for ln in invoke_cli(["ls"]).output.splitlines() if "smoke-ping" in ln)
    assert "noise" in line


def test_ls_tag_filter(listing: FakeApi) -> None:
    result = invoke_cli(["ls", "--all", "--tag", "ops"])
    assert "alpha-report" in result.output and "beta-sync" in result.output
    assert "smoke-ping" not in result.output
    assert "2 shown of 3" in result.output
    only = invoke_cli(["ls", "-a", "-t", "sync"])
    assert "beta-sync" in only.output and "alpha-report" not in only.output


def test_ls_hide_noise(listing: FakeApi) -> None:
    result = invoke_cli(["ls", "--hide-noise"])
    assert "alpha-report" in result.output and "smoke-ping" not in result.output
    assert "1 shown of 3" in result.output


def test_ls_json_is_the_filtered_raw_api_json(listing: FakeApi) -> None:
    result = invoke_cli(["ls", "--json", "--hide-noise"])
    assert result.exit_code == 0
    assert json.loads(result.output) == [ALPHA]  # paused + noise filtered out
    everything = json.loads(invoke_cli(["ls", "--json", "--all"]).output)
    assert [c["id"] for c in everything] == [ID_ALPHA, ID_BETA, ID_SMOKE]


def test_ls_api_unreachable_exits_1(fake: FakeApi) -> None:
    fake.on("GET", "/candybars", urllib.error.URLError("Connection refused"))
    result = invoke_cli(["ls"])
    assert result.exit_code == 1
    assert "unreachable" in result.output


def test_ls_api_error_exits_1(fake: FakeApi) -> None:
    fake.on("GET", "/candybars", problem(500, "Internal error", "unexpected OSError"))
    result = invoke_cli(["ls"])
    assert result.exit_code == 1 and "500" in result.output


# ---------------------------------------------------------------------------
# name vs id resolution


def test_name_resolves_through_the_listing_then_reads_by_id(listing: FakeApi) -> None:
    listing.on("GET", f"/candybars/{ID_ALPHA}", {**ALPHA, "totalInvoked": 4, "totalSuccessful": 3})
    listing.on("GET", f"/candybars/{ID_ALPHA}/runner", {"runner": None, "entrypoint": "/bin/true"})
    listing.on("GET", f"/candybars/{ID_ALPHA}/invocations", {"candybarId": ID_ALPHA, "invocations": []})
    result = invoke_cli(["show", "alpha-report"])
    assert result.exit_code == 0, result.output
    assert [c.path for c in listing.calls][:2] == ["/candybars", f"/candybars/{ID_ALPHA}"]


def test_id_resolves_without_the_listing(fake: FakeApi) -> None:
    fake.on("GET", f"/candybars/{ID_ALPHA}", {**ALPHA, "totalInvoked": 0, "totalSuccessful": 0})
    fake.on("GET", f"/candybars/{ID_ALPHA}/runner", {"runner": None, "entrypoint": "/bin/true"})
    result = invoke_cli(["show", ID_ALPHA, "--runs", "0"])
    assert result.exit_code == 0, result.output
    assert "/candybars" not in [c.path for c in fake.calls]


def test_unknown_id_exits_2(fake: FakeApi) -> None:
    fake.on("GET", f"/candybars/{ID_MISSING}", problem(404, "Candybar not found", "no candybar"))
    result = invoke_cli(["pause", ID_MISSING])
    assert result.exit_code == 2
    assert f"no candybar with id {ID_MISSING}" in result.output
    assert fake.writes() == []


def test_id_lookup_server_error_exits_1(fake: FakeApi) -> None:
    fake.on("GET", f"/candybars/{ID_ALPHA}", problem(500, "Internal error", "boom"))
    result = invoke_cli(["pause", ID_ALPHA])
    assert result.exit_code == 1 and "boom" in result.output


def test_unknown_name_exits_2_with_hint(listing: FakeApi) -> None:
    result = invoke_cli(["pause", "alpha"])
    assert result.exit_code == 2
    assert "no candybar named 'alpha'" in result.output
    assert "did you mean: alpha-report" in result.output
    assert listing.writes() == []


def test_unknown_name_without_near_matches_has_no_hint(listing: FakeApi) -> None:
    result = invoke_cli(["pause", "zzz"])
    assert result.exit_code == 2
    assert "no candybar named 'zzz'" in result.output and "did you mean" not in result.output


def test_unknown_name_hint_lists_sorted_near_matches(fake: FakeApi) -> None:
    fake.on("GET", "/candybars", [cb("job-b", "1"), cb("job-a", "2"), cb("other", "3")])
    result = invoke_cli(["pause", "job"])
    assert "did you mean: job-a, job-b" in result.output


def test_name_match_is_exact_not_substring(listing: FakeApi) -> None:
    assert invoke_cli(["history", "alpha-rep"]).exit_code == 2


@pytest.mark.parametrize(
    "args",
    [
        ["show", "nope"],
        ["history", "nope"],
        ["pause", "nope"],
        ["resume", "nope"],
        ["skip", "nope"],
        ["skip", "nope", "--today"],
        ["edit", "nope", "--name", "x"],
        ["schedule", "--candybar", "nope"],
        ["run", "nope"],
    ],
)
def test_every_command_exits_2_on_an_unknown_name(listing: FakeApi, args: list[str]) -> None:
    result = invoke_cli(args)
    assert result.exit_code == 2, result.output
    assert "no candybar named 'nope'" in result.output
    assert listing.writes() == []
    assert [c.path for c in listing.calls] == ["/candybars"]


def test_resolve_id_helper_returns_the_id(listing: FakeApi) -> None:
    assert control.resolve_id(CandybarApi(BASE), "smoke-ping") == ID_SMOKE


# ---------------------------------------------------------------------------
# show


def _show_routes(fake: FakeApi, detail: dict[str, Any], runner: dict[str, Any], runs: Any) -> None:
    fake.on("GET", f"/candybars/{detail['id']}", detail)
    fake.on("GET", f"/candybars/{detail['id']}/runner", runner)
    fake.on("GET", f"/candybars/{detail['id']}/invocations", {"candybarId": detail["id"], "invocations": runs})


def test_show_prints_definition_totals_and_runs(listing: FakeApi, frozen_now: datetime) -> None:
    detail = {**ALPHA, "description": "weekly numbers", "totalInvoked": 10, "totalSuccessful": 8}
    _show_routes(listing, detail, {"runner": None, "entrypoint": "/opt/run.sh"}, [
        inv(result="success", runtimeMs=1500),
        inv(result="fail", exitCode=2, errorMessage="boom\nsecond line", runtimeMs=90_000),
    ])
    result = invoke_cli(["show", "alpha-report", "--runs", "2"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "weekly numbers" in out
    assert "*/5 * * * *  (UTC)" in out
    assert "10 invoked, 8 succeeded, 2 failed" in out
    assert "enabled" in out and "entrypoint  /opt/run.sh" in out
    assert "1.5s" in out and "1m30s" in out
    assert "exit 2: boom" in out and "second line" not in out
    assert listing.of("GET", f"/candybars/{ID_ALPHA}/invocations")[0].query == {"limit": ["2"]}


def test_show_a_paused_noise_runner_candybar(listing: FakeApi, frozen_now: datetime) -> None:
    detail = {**BETA, "noise": True, "totalInvoked": 0, "totalSuccessful": 0}
    runner = {"runner": {"kind": "systemd", "unit": "x.service"}, "entrypoint": None}
    _show_routes(listing, detail, runner, [])
    result = invoke_cli(["show", "beta-sync"])
    assert "paused" in result.output and "noise  yes" in result.output
    assert '"kind": "systemd"' in result.output
    assert "STARTED" not in result.output  # no runs: no table


def test_show_runs_zero_skips_the_invocations_read(listing: FakeApi) -> None:
    listing.on("GET", f"/candybars/{ID_ALPHA}", {**ALPHA, "totalInvoked": 0, "totalSuccessful": 0})
    listing.on("GET", f"/candybars/{ID_ALPHA}/runner", {"runner": None, "entrypoint": "/x"})
    assert invoke_cli(["show", "alpha-report", "--runs", "0"]).exit_code == 0
    assert listing.of("GET", f"/candybars/{ID_ALPHA}/invocations") == []


def test_show_json_merges_definition_runner_and_runs(listing: FakeApi) -> None:
    detail = {**ALPHA, "totalInvoked": 1, "totalSuccessful": 1}
    runs = [inv()]
    _show_routes(listing, detail, {"runner": None, "entrypoint": "/x"}, runs)
    body = json.loads(invoke_cli(["show", "alpha-report", "--json"]).output)
    assert body["name"] == "alpha-report" and body["totalInvoked"] == 1
    assert body["entrypoint"] == "/x" and body["runner"] is None
    assert body["invocations"] == runs


def test_show_negative_runs_is_a_usage_error(listing: FakeApi) -> None:
    assert invoke_cli(["show", "alpha-report", "--runs", "-1"]).exit_code == 2


def test_show_api_failure_exits_1(listing: FakeApi) -> None:
    listing.on("GET", f"/candybars/{ID_ALPHA}", problem(500, "Internal error", "db gone"))
    listing.on("GET", f"/candybars/{ID_ALPHA}/runner", {"runner": None, "entrypoint": "/x"})
    result = invoke_cli(["show", "alpha-report"])
    assert result.exit_code == 1 and "db gone" in result.output


def test_show_with_an_in_flight_run_does_not_crash(listing: FakeApi, frozen_now: datetime) -> None:
    """The executor writes the row at claim time with result=null: `show` lists it."""
    _show_routes(listing, {**ALPHA, "totalInvoked": 1, "totalSuccessful": 0},
                 {"runner": None, "entrypoint": "/x"},
                 [inv(result=None, exitCode=None, runtimeMs=None, finishedAt=None)])
    result = invoke_cli(["show", "alpha-report"])
    assert result.exit_code == 0, repr(result.exception)


# ---------------------------------------------------------------------------
# next


def test_next_prints_the_next_fire(fake: FakeApi, frozen_now: datetime) -> None:
    fake.on("GET", "/schedule/next", sched_row(ALPHA, at_minute(6, 35), "current"))
    result = invoke_cli(["next"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "alpha-report  Mon 10-26 06:35 (in 5m)  */5 * * * *"


def test_next_flags_noise(fake: FakeApi, frozen_now: datetime) -> None:
    fake.on("GET", "/schedule/next", sched_row(SMOKE, at_minute(6, 31), "current"))
    assert invoke_cli(["next"]).output.rstrip().endswith("[noise]")


def test_next_json(fake: FakeApi) -> None:
    row = sched_row(ALPHA, at_minute(6, 35), "current")
    fake.on("GET", "/schedule/next", row)
    assert json.loads(invoke_cli(["next", "--json"]).output) == row


def test_next_with_nothing_scheduled_exits_1(fake: FakeApi) -> None:
    fake.on("GET", "/schedule/next", problem(404, "No upcoming fire", "none"))
    result = invoke_cli(["next"])
    assert result.exit_code == 1 and "nothing is scheduled to fire" in result.output


def test_next_server_error_exits_1(fake: FakeApi) -> None:
    fake.on("GET", "/schedule/next", problem(500, "Internal error", "x"))
    assert invoke_cli(["next"]).exit_code == 1


# ---------------------------------------------------------------------------
# schedule


def test_schedule_default_window_is_now_to_twelve_hours(fake: FakeApi, frozen_now: datetime) -> None:
    fake.on("GET", "/schedule", [])
    result = invoke_cli(["schedule"])
    assert result.exit_code == 0, result.output
    query = fake.calls[0].query
    assert query == {"from": ["2026-10-26T06:30:00Z"], "to": ["2026-10-26T18:30:00Z"]}
    assert "0 fires" in result.output


def test_schedule_back_and_ahead_shape_the_window(fake: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(control, "_now", lambda: UNIT_NOW + timedelta(seconds=42))
    fake.on("GET", "/schedule", [])
    assert invoke_cli(["schedule", "--back", "2", "--ahead", "0.5"]).exit_code == 0
    # `from` is floored to the minute, `to` is exact.
    assert fake.calls[0].query == {"from": ["2026-10-26T04:30:00Z"], "to": ["2026-10-26T07:00:42Z"]}


def test_schedule_table_marks_current_and_states(fake: FakeApi, frozen_now: datetime) -> None:
    fake.on("GET", "/schedule", [
        sched_row(ALPHA, at_minute(6, 10), "past"),
        sched_row(ALPHA, at_minute(6, 25), "past", lastResult="success"),
        sched_row(ALPHA, at_minute(6, 27), "past"),
        sched_row(ALPHA, at_minute(6, 35), "current"),
        sched_row(ALPHA, at_minute(6, 40), "upcoming", skipped=True),
        sched_row(SMOKE, at_minute(6, 41), "upcoming"),
    ])
    lines = invoke_cli(["schedule", "--back", "1"]).output.splitlines()
    by_time = {ln.split("06:")[1][:2]: ln for ln in lines if "06:" in ln}
    assert "success" in by_time["25"]
    assert "no-run" in by_time["10"]  # long past and never finished
    assert "pending" in by_time["27"]  # recent: may still be running
    assert by_time["35"].startswith(">") and "current" in by_time["35"]
    assert "skipped" in by_time["40"]
    assert "upcoming" in by_time["41"]
    assert lines[-1] == "6 fires"


def test_schedule_candybar_filter_by_name(listing: FakeApi, frozen_now: datetime) -> None:
    listing.on("GET", "/schedule", [
        sched_row(ALPHA, at_minute(6, 35), "current"),
        sched_row(SMOKE, at_minute(6, 31)),
    ])
    out = invoke_cli(["schedule", "-c", "alpha-report"]).output
    assert "alpha-report" in out and "smoke-ping" not in out and "1 fires" in out


def test_schedule_hide_noise(fake: FakeApi, frozen_now: datetime) -> None:
    fake.on("GET", "/schedule", [
        sched_row(ALPHA, at_minute(6, 35), "current"),
        sched_row(SMOKE, at_minute(6, 31)),
    ])
    out = invoke_cli(["schedule", "--hide-noise"]).output
    assert "smoke-ping" not in out and "alpha-report" in out and "1 fires" in out


def test_schedule_json_prints_filtered_rows(fake: FakeApi, frozen_now: datetime) -> None:
    keep = sched_row(ALPHA, at_minute(6, 35), "current")
    fake.on("GET", "/schedule", [keep, sched_row(SMOKE, at_minute(6, 31))])
    assert json.loads(invoke_cli(["schedule", "--json", "--hide-noise"]).output) == [keep]


@pytest.mark.parametrize(
    "args",
    [["--ahead", "-1"], ["--back", "-1"], ["--ahead", "0"], ["--ahead", "0", "--back", "0"]],
)
def test_schedule_rejects_an_empty_or_negative_window(fake: FakeApi, args: list[str]) -> None:
    result = invoke_cli(["schedule", *args])
    assert result.exit_code == 2
    assert fake.calls == []


def test_schedule_too_many_fires_exits_1(fake: FakeApi, frozen_now: datetime) -> None:
    fake.on("GET", "/schedule", problem(422, "Invalid schedule window", "the window has more than 20000 fires; narrow it"))
    result = invoke_cli(["schedule"])
    assert result.exit_code == 1 and "narrow it" in result.output


# ---------------------------------------------------------------------------
# history


def test_history_table_and_limit(listing: FakeApi, frozen_now: datetime) -> None:
    listing.on("GET", f"/candybars/{ID_ALPHA}/invocations", {
        "candybarId": ID_ALPHA,
        "invocations": [
            inv(result="success", trigger="scheduled", runtimeMs=800, memoryPeakBytes=3 * 1024 * 1024,
                tokenSpend=120, cpuLoad=1.234),
            inv(result="fail", exitCode=1, errorMessage="it broke", runtimeMs=12_000),
        ],
    })
    result = invoke_cli(["history", "alpha-report", "-n", "2"])
    assert result.exit_code == 0, result.output
    assert listing.of("GET", f"/candybars/{ID_ALPHA}/invocations")[0].query == {"limit": ["2"]}
    out = result.output
    assert "scheduled" in out and "800ms" in out and "3.0M" in out and "1.23" in out and "120" in out
    assert "12.0s" in out and "exit 1: it broke" in out


def test_history_default_limit_is_20(listing: FakeApi) -> None:
    listing.on("GET", f"/candybars/{ID_ALPHA}/invocations", {"candybarId": ID_ALPHA, "invocations": []})
    invoke_cli(["history", "alpha-report"])
    assert listing.of("GET", f"/candybars/{ID_ALPHA}/invocations")[0].query == {"limit": ["20"]}


def test_history_empty(listing: FakeApi) -> None:
    listing.on("GET", f"/candybars/{ID_ALPHA}/invocations", {"candybarId": ID_ALPHA, "invocations": []})
    assert "no recorded runs" in invoke_cli(["history", "alpha-report"]).output


def test_history_failed_filters_to_failures(listing: FakeApi, frozen_now: datetime) -> None:
    rows = [inv(id="a", result="success"), inv(id="b", result="fail", exitCode=1)]
    listing.on("GET", f"/candybars/{ID_ALPHA}/invocations", {"candybarId": ID_ALPHA, "invocations": rows})
    got = json.loads(invoke_cli(["history", "alpha-report", "--failed", "--json"]).output)
    assert [r["id"] for r in got] == ["b"]
    text = invoke_cli(["history", "alpha-report", "--failed"]).output
    assert text.count("fail") == 1 and "success" not in text


def test_history_json_is_the_raw_rows(listing: FakeApi) -> None:
    rows = [inv()]
    listing.on("GET", f"/candybars/{ID_ALPHA}/invocations", {"candybarId": ID_ALPHA, "invocations": rows})
    assert json.loads(invoke_cli(["history", "alpha-report", "--json"]).output) == rows


def test_history_limit_below_one_is_a_usage_error(listing: FakeApi) -> None:
    assert invoke_cli(["history", "alpha-report", "-n", "0"]).exit_code == 2


def test_history_with_an_in_flight_run_does_not_crash(listing: FakeApi, frozen_now: datetime) -> None:
    listing.on("GET", f"/candybars/{ID_ALPHA}/invocations", {
        "candybarId": ID_ALPHA,
        "invocations": [inv(result=None, exitCode=None, runtimeMs=None, finishedAt=None)],
    })
    result = invoke_cli(["history", "alpha-report"])
    assert result.exit_code == 0, repr(result.exception)


def test_history_failed_does_not_list_a_run_still_in_flight(listing: FakeApi) -> None:
    rows = [inv(id="running", result=None, exitCode=None), inv(id="bad", result="fail", exitCode=1)]
    listing.on("GET", f"/candybars/{ID_ALPHA}/invocations", {"candybarId": ID_ALPHA, "invocations": rows})
    got = json.loads(invoke_cli(["history", "alpha-report", "--failed", "--json"]).output)
    assert [r["id"] for r in got] == ["bad"]


# ---------------------------------------------------------------------------
# pause / resume


def test_pause_posts_enabled_false(listing: FakeApi) -> None:
    listing.on("POST", f"/candybars/{ID_ALPHA}/enabled", {**ALPHA, "enabled": False})
    result = invoke_cli(["pause", "alpha-report"])
    assert result.exit_code == 0, result.output
    assert "paused alpha-report" in result.output
    [write] = listing.writes()
    assert write.body == {"enabled": False}
    assert write.headers["content-type"] == "application/json"


def test_resume_posts_enabled_true(listing: FakeApi) -> None:
    listing.on("POST", f"/candybars/{ID_BETA}/enabled", {**BETA, "enabled": True})
    result = invoke_cli(["resume", "beta-sync"])
    assert result.exit_code == 0 and "resumed beta-sync" in result.output
    assert [w.body for w in listing.writes()] == [{"enabled": True}]


def test_pause_is_idempotent_and_does_not_write(listing: FakeApi) -> None:
    result = invoke_cli(["pause", "beta-sync"])  # already paused
    assert result.exit_code == 0
    assert "beta-sync already paused" in result.output
    assert listing.writes() == []


def test_resume_is_idempotent_and_does_not_write(listing: FakeApi) -> None:
    result = invoke_cli(["resume", "alpha-report"])  # already enabled
    assert result.exit_code == 0
    assert "alpha-report already resumed" in result.output
    assert listing.writes() == []


def test_pause_by_id_uses_the_id_lookup_state(fake: FakeApi) -> None:
    fake.on("GET", f"/candybars/{ID_BETA}", {**BETA, "totalInvoked": 0, "totalSuccessful": 0})
    result = invoke_cli(["pause", ID_BETA])
    assert "already paused" in result.output and fake.writes() == []


def test_pause_treats_a_missing_enabled_flag_as_enabled(fake: FakeApi) -> None:
    legacy = {k: v for k, v in ALPHA.items() if k != "enabled"}
    fake.on("GET", "/candybars", [legacy])
    fake.on("POST", f"/candybars/{ID_ALPHA}/enabled", {**legacy, "enabled": False})
    assert "paused alpha-report" in invoke_cli(["pause", "alpha-report"]).output
    assert "already resumed" in invoke_cli(["resume", "alpha-report"]).output


def test_pause_api_refusal_exits_1(listing: FakeApi) -> None:
    listing.on("POST", f"/candybars/{ID_ALPHA}/enabled", problem(503, "Event bus unavailable", "bus down"))
    result = invoke_cli(["pause", "alpha-report"])
    assert result.exit_code == 1 and "bus down" in result.output


# ---------------------------------------------------------------------------
# skip


def _skip_ok(fake: FakeApi, cid: str = ID_ALPHA) -> None:
    def respond(call: Call) -> dict[str, Any]:
        return {"skipped": True, "fireTime": call.body["fireTime"].replace("Z", "+00:00")}

    fake.on("POST", f"/candybars/{cid}/skip", respond)


def _one_minute(call: Call) -> tuple[str, str]:
    return call.query["from"][0], call.query["to"][0]


def test_skip_default_skips_the_next_unskipped_fire(listing: FakeApi, frozen_now: datetime) -> None:
    listing.on("GET", "/schedule", [
        sched_row(SMOKE, at_minute(6, 35)),  # another candybar, same minute
        sched_row(ALPHA, at_minute(6, 35), "current", skipped=True),  # already skipped
        sched_row(ALPHA, at_minute(6, 40), "upcoming"),  # the one
    ])
    _skip_ok(listing)
    result = invoke_cli(["skip", "alpha-report"])
    assert result.exit_code == 0, result.output
    assert "skipped alpha-report at Mon 10-26 06:40" in result.output
    [write] = listing.writes()
    assert write.body == {"fireTime": "2026-10-26T06:40:00Z"}
    # Candidates come from the cron itself; each is a single-minute read.
    assert [_one_minute(c) for c in listing.of("GET", "/schedule")] == [
        ("2026-10-26T06:35:00Z", "2026-10-26T06:36:00Z"),
        ("2026-10-26T06:40:00Z", "2026-10-26T06:41:00Z"),
    ]


def test_skip_default_stops_at_the_first_hit(listing: FakeApi, frozen_now: datetime) -> None:
    listing.on("GET", "/schedule", [sched_row(ALPHA, at_minute(6, 35), "current")])
    _skip_ok(listing)
    assert invoke_cli(["skip", "alpha-report"]).exit_code == 0
    assert len(listing.of("GET", "/schedule")) == 1
    assert listing.writes()[0].body == {"fireTime": "2026-10-26T06:35:00Z"}


def test_skip_default_with_every_fire_skipped_gives_up(listing: FakeApi, frozen_now: datetime) -> None:
    def all_skipped(call: Call) -> list[dict[str, Any]]:
        return [sched_row(ALPHA, datetime.fromisoformat(call.query["from"][0]), skipped=True)]

    listing.on("GET", "/schedule", all_skipped)
    result = invoke_cli(["skip", "alpha-report"])
    assert result.exit_code == 1
    assert "no unskipped fire among its next 50; pass --at" in result.output
    assert len(listing.of("GET", "/schedule")) == control.NEXT_FIRE_CANDIDATES
    assert listing.writes() == []


def test_skip_default_never_reads_a_window_wider_than_a_minute(listing: FakeApi, frozen_now: datetime) -> None:
    """Minutely jobs push a multi-day /schedule window past the API's 20,000-fire
    cap (422), so the search reads one minute at a time, even for a sparse cron."""
    weekly = cb("weekly-prune", "01900000-0000-7000-8000-0000000000ee", schedule="7 5 * * 0")
    listing.on("GET", "/candybars", [ALPHA, BETA, SMOKE, weekly])
    listing.on("GET", "/schedule", [])
    _skip_ok(listing, weekly["id"])
    result = invoke_cli(["skip", "weekly-prune"])
    assert result.exit_code == 0, result.output
    [call] = listing.of("GET", "/schedule")
    assert _one_minute(call) == ("2026-11-01T05:07:00Z", "2026-11-01T05:08:00Z")


def test_skip_default_other_api_error_is_reported(listing: FakeApi, frozen_now: datetime) -> None:
    listing.on("GET", "/schedule", problem(500, "Internal error", "db gone"))
    result = invoke_cli(["skip", "alpha-report"])
    assert result.exit_code == 1 and "db gone" in result.output
    assert len(listing.of("GET", "/schedule")) == 1


def test_skip_default_for_a_paused_candybar_targets_its_next_cron_fire(
    listing: FakeApi, frozen_now: datetime
) -> None:
    """A paused candybar has no upcoming rows, but its cron still names the fire
    a skip would cover once it is resumed."""
    listing.on("GET", "/schedule", [])
    _skip_ok(listing, ID_BETA)
    assert invoke_cli(["skip", "beta-sync"]).exit_code == 0
    assert listing.writes()[0].body == {"fireTime": "2026-10-26T07:00:00Z"}


def test_skip_write_refusal_exits_1(listing: FakeApi, frozen_now: datetime) -> None:
    listing.on("GET", "/schedule", [sched_row(ALPHA, at_minute(6, 35), "current")])
    listing.on("POST", f"/candybars/{ID_ALPHA}/skip", problem(503, "Event bus unavailable", "retry"))
    result = invoke_cli(["skip", "alpha-report"])
    assert result.exit_code == 1 and "retry" in result.output


def test_skip_at_aware_time_is_sent_in_utc(listing: FakeApi, frozen_now: datetime) -> None:
    _skip_ok(listing)
    assert invoke_cli(["skip", "alpha-report", "--at", "2026-10-26T09:05:00+02:00"]).exit_code == 0
    assert listing.writes()[0].body == {"fireTime": "2026-10-26T07:05:00Z"}
    assert listing.of("GET", "/schedule") == []  # --at never searches


def test_skip_at_naive_time_is_this_machines_local_time(
    listing: FakeApi, frozen_now: datetime, local_tz: Callable[[str], None]
) -> None:
    local_tz("America/New_York")  # EDT, UTC-4, on 2026-10-26
    _skip_ok(listing)
    assert invoke_cli(["skip", "alpha-report", "--at", "2026-10-26T08:05"]).exit_code == 0
    assert listing.writes()[0].body == {"fireTime": "2026-10-26T12:05:00Z"}


def test_skip_at_garbage_exits_2(listing: FakeApi) -> None:
    result = invoke_cli(["skip", "alpha-report", "--at", "tomorrow-ish"])
    assert result.exit_code == 2 and "--at must be an ISO time" in result.output
    assert listing.writes() == []


def test_skip_at_unknown_fire_is_the_apis_404(listing: FakeApi, frozen_now: datetime) -> None:
    listing.on("POST", f"/candybars/{ID_ALPHA}/skip", problem(404, "No such fire", "never fires at 07:03"))
    result = invoke_cli(["skip", "alpha-report", "--at", "2026-10-26T07:03:00Z"])
    assert result.exit_code == 1 and "never fires at 07:03" in result.output


def test_skip_today_uses_the_candybars_timezone(fake: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    # 03:00 UTC on the 26th is still the 25th in Los Angeles ...
    monkeypatch.setattr(control, "_now", lambda: datetime(2026, 10, 26, 3, 0, tzinfo=UTC))
    la = cb("la-job", ID_ALPHA, timezone="America/Los_Angeles")
    fake.on("GET", "/candybars", [la])
    fake.on("POST", f"/candybars/{ID_ALPHA}/skip-rest-of-day", lambda c: {"skipped": True, "date": c.body["date"]})
    result = invoke_cli(["skip", "la-job", "--today"])
    assert result.exit_code == 0, result.output
    assert fake.writes()[0].body == {"date": "2026-10-25"}
    assert "skipped la-job for 2026-10-25 (America/Los_Angeles)" in result.output
    assert fake.of("GET", "/schedule") == []


def test_skip_today_in_a_zone_ahead_of_utc(fake: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    # ... and 20:00 UTC on the 26th is already the 27th in Tokyo.
    monkeypatch.setattr(control, "_now", lambda: datetime(2026, 10, 26, 20, 0, tzinfo=UTC))
    tokyo = cb("tokyo-job", ID_ALPHA, timezone="Asia/Tokyo")
    fake.on("GET", "/candybars", [tokyo])
    fake.on("POST", f"/candybars/{ID_ALPHA}/skip-rest-of-day", {"skipped": True, "date": "2026-10-27"})
    assert invoke_cli(["skip", "tokyo-job", "--today"]).exit_code == 0
    assert fake.writes()[0].body == {"date": "2026-10-27"}


def test_skip_today_defaults_to_utc_without_a_timezone(fake: FakeApi, frozen_now: datetime) -> None:
    legacy = {k: v for k, v in ALPHA.items() if k != "timezone"}
    fake.on("GET", "/candybars", [legacy])
    fake.on("POST", f"/candybars/{ID_ALPHA}/skip-rest-of-day", {"skipped": True, "date": "2026-10-26"})
    result = invoke_cli(["skip", "alpha-report", "--today"])
    assert fake.writes()[0].body == {"date": "2026-10-26"} and "(UTC)" in result.output


def test_skip_date_sends_that_day(listing: FakeApi, frozen_now: datetime) -> None:
    listing.on("POST", f"/candybars/{ID_ALPHA}/skip-rest-of-day", {"skipped": True, "date": "2026-12-25"})
    result = invoke_cli(["skip", "alpha-report", "--date", "2026-12-25"])
    assert result.exit_code == 0 and "skipped alpha-report for 2026-12-25 (UTC)" in result.output
    assert listing.writes()[0].body == {"date": "2026-12-25"}


@pytest.mark.parametrize("bad", ["2026-13-40", "tomorrow", "10/26/2026"])
def test_skip_date_garbage_exits_2(listing: FakeApi, bad: str) -> None:
    result = invoke_cli(["skip", "alpha-report", "--date", bad])
    assert result.exit_code == 2 and "--date must be YYYY-MM-DD" in result.output
    assert listing.writes() == []


def test_skip_date_with_no_fires_is_the_apis_404(listing: FakeApi) -> None:
    listing.on("POST", f"/candybars/{ID_ALPHA}/skip-rest-of-day", problem(404, "No such fire", "never fires on that day"))
    result = invoke_cli(["skip", "alpha-report", "--date", "2026-12-25"])
    assert result.exit_code == 1 and "never fires on that day" in result.output


@pytest.mark.parametrize(
    "flags",
    [
        ["--at", "2026-10-26T09:00", "--today"],
        ["--at", "2026-10-26T09:00", "--date", "2026-10-27"],
        ["--today", "--date", "2026-10-27"],
        ["--at", "2026-10-26T09:00", "--today", "--date", "2026-10-27"],
    ],
)
def test_skip_conflicting_flags_exit_2_before_any_request(fake: FakeApi, flags: list[str]) -> None:
    result = invoke_cli(["skip", "alpha-report", *flags])
    assert result.exit_code == 2
    assert "pick at most one of --at, --today, --date" in result.output
    assert fake.calls == []


# ---------------------------------------------------------------------------
# edit


def _patch_ok(fake: FakeApi, **overrides: Any) -> None:
    fake.on("PATCH", f"/candybars/{ID_ALPHA}", lambda c: {**ALPHA, **c.body, **overrides, "totalInvoked": 0, "totalSuccessful": 0})


def test_edit_sends_only_the_passed_fields(listing: FakeApi) -> None:
    _patch_ok(listing)
    result = invoke_cli(["edit", "alpha-report", "--schedule", "0 3 * * *"])
    assert result.exit_code == 0, result.output
    [write] = listing.writes()
    assert write.method == "PATCH" and write.body == {"schedule": "0 3 * * *"}
    assert "updated alpha-report" in result.output
    assert "schedule: '*/5 * * * *' -> '0 3 * * *'" in result.output


def test_edit_every_field(listing: FakeApi) -> None:
    _patch_ok(listing)
    result = invoke_cli([
        "edit", "alpha-report", "--schedule", "1 2 * * *", "--timezone", "Asia/Tokyo",
        "--name", "alpha-v2", "--description", "new words", "--tags", "a, b ,,c", "--noise",
    ])
    assert result.exit_code == 0, result.output
    assert listing.writes()[0].body == {
        "schedule": "1 2 * * *", "timezone": "Asia/Tokyo", "name": "alpha-v2",
        "description": "new words", "noise": True, "tags": ["a", "b", "c"],
    }
    assert "updated alpha-v2" in result.output
    assert "name: 'alpha-report' -> 'alpha-v2'" in result.output
    assert "tags: ['ops'] -> ['a', 'b', 'c']" in result.output
    assert "noise: False -> True" in result.output


def test_edit_empty_tags_clears_them(listing: FakeApi) -> None:
    _patch_ok(listing)
    assert invoke_cli(["edit", "alpha-report", "--tags", ""]).exit_code == 0
    assert listing.writes()[0].body == {"tags": []}


def test_edit_empty_description_is_a_change_not_an_omission(listing: FakeApi) -> None:
    _patch_ok(listing)
    assert invoke_cli(["edit", "alpha-report", "--description", ""]).exit_code == 0
    assert listing.writes()[0].body == {"description": ""}


def test_edit_no_noise_sends_false(listing: FakeApi) -> None:
    listing.on("GET", "/candybars", [SMOKE])
    listing.on("PATCH", f"/candybars/{ID_SMOKE}", {**SMOKE, "noise": False, "totalInvoked": 0, "totalSuccessful": 0})
    result = invoke_cli(["edit", "smoke-ping", "--no-noise"])
    assert result.exit_code == 0, result.output
    assert listing.writes()[0].body == {"noise": False}
    assert "noise: True -> False" in result.output


def test_edit_with_nothing_to_change_exits_2_without_requests(fake: FakeApi) -> None:
    result = invoke_cli(["edit", "alpha-report"])
    assert result.exit_code == 2
    assert "nothing to change: pass at least one option" in result.output
    assert fake.calls == []


def test_edit_that_changes_nothing_says_so(listing: FakeApi) -> None:
    listing.on("PATCH", f"/candybars/{ID_ALPHA}", {**ALPHA, "totalInvoked": 0, "totalSuccessful": 0})
    result = invoke_cli(["edit", "alpha-report", "--schedule", "*/5 * * * *"])
    assert result.exit_code == 0 and "updated alpha-report (no change)" in result.output


def test_edit_api_rejection_exits_1(listing: FakeApi) -> None:
    listing.on("PATCH", f"/candybars/{ID_ALPHA}", problem(422, "Invalid candybar definition", "bad cron"))
    result = invoke_cli(["edit", "alpha-report", "--schedule", "nope"])
    assert result.exit_code == 1 and "bad cron" in result.output


def test_edit_rename_conflict_exits_1(listing: FakeApi) -> None:
    listing.on("PATCH", f"/candybars/{ID_ALPHA}", problem(409, "Candybar name already registered", "name taken"))
    result = invoke_cli(["edit", "alpha-report", "--name", "beta-sync"])
    assert result.exit_code == 1 and "name taken" in result.output


# ---------------------------------------------------------------------------
# run --wait


ACCEPTED = {
    "candybarId": ID_ALPHA,
    "name": "alpha-report",
    "invocationId": INV_ID,
    "commandId": "9f8e7d6c-1111-4222-8333-444455556666",
    "idempotencyKey": f"candybar:{ID_ALPHA}:manual:9f8e7d6c-1111-4222-8333-444455556666",
    "trigger": "manual",
}


class Clock:
    """A fake monotonic clock plus a sleep that advances it."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Make `run --wait` poll against a fake clock (main.py calls wait_for_invocation by attribute)."""
    fake_clock = Clock()
    real = control.wait_for_invocation

    def wrapped(api: CandybarApi, invocation_id: str, timeout_s: float, **_: Any) -> Any:
        return real(api, invocation_id, timeout_s, sleep=fake_clock.sleep, clock=fake_clock.now)

    monkeypatch.setattr(control, "wait_for_invocation", wrapped)
    return fake_clock


def _run_routes(fake: FakeApi, polls: Any) -> None:
    fake.on("POST", f"/candybars/{ID_ALPHA}/invoke", ACCEPTED)
    fake.on("GET", f"/invocations/{INV_ID}", polls)


def test_run_wait_polls_through_404_and_null_result_to_the_result(fake: FakeApi, clock: Clock) -> None:
    _run_routes(fake, Seq(
        problem(404, "Invocation not found", "not yet"),
        inv(result=None, exitCode=None, runtimeMs=None, finishedAt=None),
        inv(result="success", runtimeMs=2300, memoryPeakBytes=5 * 1024 * 1024, tokenSpend=42),
    ))
    result = invoke_cli(["run", ID_ALPHA, "--wait"])
    assert result.exit_code == 0, result.output
    assert "invoked alpha-report" in result.output
    assert "success in 2.3s (exit 0, mem 5.0M, tokens 42)" in result.output
    assert len(fake.of("GET", f"/invocations/{INV_ID}")) == 3
    assert clock.sleeps == [0.5, 1.0]  # exponential backoff between polls


def test_run_without_wait_never_polls(fake: FakeApi) -> None:
    _run_routes(fake, inv())
    assert invoke_cli(["run", ID_ALPHA]).exit_code == 0
    assert fake.of("GET", f"/invocations/{INV_ID}") == []


def test_run_wait_by_name_resolves_invokes_and_polls(listing: FakeApi, clock: Clock) -> None:
    _run_routes(listing, inv(result="success"))
    result = invoke_cli(["run", "alpha-report", "-w"])
    assert result.exit_code == 0, result.output
    assert [c.path for c in listing.calls] == [
        "/candybars", f"/candybars/{ID_ALPHA}/invoke", f"/invocations/{INV_ID}",
    ]


def test_run_wait_failed_run_exits_1_with_error_and_stderr_tail(fake: FakeApi, clock: Clock) -> None:
    _run_routes(fake, inv(
        result="fail", exitCode=3, errorMessage="exited with code 3",
        stderrTail="Traceback...\nValueError: nope\n", runtimeMs=700,
    ))
    result = invoke_cli(["run", ID_ALPHA, "--wait"])
    assert result.exit_code == 1
    assert "fail in 700ms (exit 3" in result.output
    assert "error: exited with code 3" in result.output
    assert "ValueError: nope" in result.output


def test_run_wait_failed_run_without_detail_still_exits_1(fake: FakeApi, clock: Clock) -> None:
    _run_routes(fake, inv(result="fail", exitCode=None, errorMessage=None, stderrTail=None))
    result = invoke_cli(["run", ID_ALPHA, "--wait"])
    assert result.exit_code == 1 and "error:" not in result.output


def test_run_wait_timeout_exits_3_and_points_at_history(fake: FakeApi, clock: Clock) -> None:
    _run_routes(fake, inv(result=None, exitCode=None))  # forever in flight
    result = invoke_cli(["run", ID_ALPHA, "--wait", "--timeout", "5"])
    assert result.exit_code == 3
    assert "still running after 5s; check `candybar history alpha-report`" in result.output
    assert clock.t >= 5
    assert sum(clock.sleeps) <= 5 + 5  # never sleeps a whole extra cycle beyond the cap


def test_run_wait_5xx_while_polling_keeps_waiting_then_exits_3(fake: FakeApi, clock: Clock) -> None:
    """A poll blip says nothing about the run: keep polling, and a deadline is exit 3, not 1."""
    _run_routes(fake, problem(500, "Internal error", "db gone"))
    result = invoke_cli(["run", ID_ALPHA, "--wait", "--timeout", "30"])
    assert result.exit_code == 3 and "still running" in result.output


def test_run_wait_unreachable_while_polling_keeps_waiting_then_exits_3(fake: FakeApi, clock: Clock) -> None:
    _run_routes(fake, urllib.error.URLError("Connection refused"))
    result = invoke_cli(["run", ID_ALPHA, "--wait", "--timeout", "30"])
    assert result.exit_code == 3 and "still running" in result.output


def test_run_wait_4xx_while_polling_exits_1(fake: FakeApi, clock: Clock) -> None:
    _run_routes(fake, problem(422, "Invalid id", "bad"))
    result = invoke_cli(["run", ID_ALPHA, "--wait"])
    assert result.exit_code == 1 and "bad" in result.output


def test_run_wait_does_not_poll_when_the_invoke_is_refused(fake: FakeApi, clock: Clock) -> None:
    fake.on("POST", f"/candybars/{ID_ALPHA}/invoke", problem(503, "Event bus unavailable", "bus"))
    assert invoke_cli(["run", ID_ALPHA, "--wait"]).exit_code == 1
    assert fake.of("GET", f"/invocations/{INV_ID}") == []


def test_run_wait_default_timeout_is_an_hour(monkeypatch: pytest.MonkeyPatch, fake: FakeApi) -> None:
    seen: list[float] = []

    def spy(api: CandybarApi, invocation_id: str, timeout_s: float) -> Any:
        seen.append(timeout_s)
        return inv()

    monkeypatch.setattr(control, "wait_for_invocation", spy)
    _run_routes(fake, inv())
    assert invoke_cli(["run", ID_ALPHA, "--wait"]).exit_code == 0
    assert seen == [3600]


# --- wait_for_invocation / print_run directly -------------------------------


def test_wait_returns_immediately_on_a_finished_row() -> None:
    clock = Clock()
    api = FakeApi().on("GET", f"/invocations/{INV_ID}", inv(result="fail"))
    with patch("urllib.request.urlopen", side_effect=api):
        row = control.wait_for_invocation(CandybarApi(BASE), INV_ID, 10, sleep=clock.sleep, clock=clock.now)
    assert row is not None and row["result"] == "fail" and clock.sleeps == []


def test_wait_backoff_doubles_to_a_five_second_cap_then_times_out() -> None:
    clock = Clock()
    api = FakeApi().on("GET", f"/invocations/{INV_ID}", problem(404, "Invocation not found", "x"))
    with patch("urllib.request.urlopen", side_effect=api):
        row = control.wait_for_invocation(CandybarApi(BASE), INV_ID, 20, sleep=clock.sleep, clock=clock.now)
    assert row is None
    assert clock.sleeps[:6] == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0]
    assert clock.t >= 20


def test_wait_with_zero_timeout_polls_once() -> None:
    clock = Clock()
    api = FakeApi().on("GET", f"/invocations/{INV_ID}", inv(result=None))
    with patch("urllib.request.urlopen", side_effect=api):
        assert control.wait_for_invocation(CandybarApi(BASE), INV_ID, 0, sleep=clock.sleep, clock=clock.now) is None
    assert len(api.calls) == 1 and clock.sleeps == []


def test_wait_rides_out_a_5xx_blip() -> None:
    api = FakeApi().on(
        "GET", f"/invocations/{INV_ID}", Seq(problem(502, "Bad gateway", "x"), inv(result="success"))
    )
    with patch("urllib.request.urlopen", side_effect=api):
        row = control.wait_for_invocation(CandybarApi(BASE), INV_ID, 5, sleep=lambda s: None, clock=lambda: 0.0)
    assert row is not None and row["result"] == "success"


def test_wait_propagates_a_4xx_api_error() -> None:
    api = FakeApi().on("GET", f"/invocations/{INV_ID}", problem(400, "Bad request", "x"))
    with patch("urllib.request.urlopen", side_effect=api), pytest.raises(ApiError):
        control.wait_for_invocation(CandybarApi(BASE), INV_ID, 5, sleep=lambda s: None, clock=lambda: 0.0)


def test_print_run_formats(capsys: pytest.CaptureFixture[str]) -> None:
    control.print_run(inv(result="success", runtimeMs=61_000, memoryPeakBytes=0, tokenSpend=None))
    assert capsys.readouterr().out.strip() == "success in 1m01s (exit 0, mem 0B, tokens 0)"


# ---------------------------------------------------------------------------
# tail (SSE)


def frame(event: str | None, data: Any = None, fid: str | None = None, *, eol: bytes = b"\n") -> list[bytes]:
    lines = []
    if fid is not None:
        lines.append(f"id: {fid}".encode() + eol)
    if event is not None:
        lines.append(f"event: {event}".encode() + eol)
    if data is not None:
        lines.append(b"data: " + (data if isinstance(data, bytes) else json.dumps(data).encode()) + eol)
    return [*lines, eol]


def stream_lines(*frames: list[bytes]) -> list[bytes]:
    return [line for f in frames for line in f]


@pytest.fixture()
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """time.sleep ends the tail loop on its Nth call with KeyboardInterrupt."""
    calls: list[float] = []
    budget = {"left": 1}

    def fake_sleep(seconds: float) -> None:
        calls.append(seconds)
        budget["left"] -= 1
        if budget["left"] <= 0:
            raise KeyboardInterrupt

    fake_sleep.budget = budget  # type: ignore[attr-defined]
    monkeypatch.setattr(control.time, "sleep", fake_sleep)
    return calls


def tail_api(listing: FakeApi, *streams: Any) -> FakeApi:
    listing.on("GET", "/schedule/stream", Seq(*streams))
    return listing


def _invoked(cid: str, trigger: str = "scheduled", row: Any = "auto") -> dict[str, Any]:
    cand = {ID_ALPHA: ALPHA, ID_SMOKE: SMOKE}[cid]
    body = {
        "type": "invocation.started", "seq": 1, "key": f"{cid}@x", "at": "2026-10-26T06:35:00+00:00",
        "candybarId": cid, "invocationId": INV_ID, "trigger": trigger,
        "row": sched_row(cand, at_minute(6, 35), "current") if row == "auto" else row,
    }
    return body


def test_tail_prints_one_line_per_event_and_exits_0_on_ctrl_c(listing: FakeApi, sleeps: list[float]) -> None:
    done = {**_invoked(ID_ALPHA), "type": "invocation.completed", "result": "success"}
    bad = {**_invoked(ID_ALPHA), "type": "invocation.completed", "result": "fail"}
    skip = {"candybarId": ID_ALPHA, "reason": "occurrence", "row": sched_row(ALPHA, at_minute(6, 40), skipped=True)}
    tail_api(listing, SseResponse(stream_lines(
        [b"retry: 3000\n", b"\n"],
        frame("ready", {"type": "ready", "cut": "x", "current": None}, "boot-0"),
        [b": keepalive\n", b"\n"],
        frame("invocation.started", _invoked(ID_ALPHA), "boot-1"),
        frame("invocation.completed", done, "boot-2"),
        frame("invocation.completed", bad, "boot-3"),
        frame("skip.applied", skip, "boot-4"),
        frame("reset", {"reason": "unresumable"}, "boot-5"),
    )))
    result = invoke_cli(["tail"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert any("▶ alpha-report  started (scheduled)" in ln for ln in lines)
    assert any("✓ alpha-report  success (scheduled)" in ln for ln in lines)
    assert any("✗ alpha-report  fail (scheduled)" in ln for ln in lines)
    assert any("⤼ alpha-report  skipped (occurrence)" in ln for ln in lines)
    assert any("stream reset" in ln for ln in lines)
    assert not any("ready" in ln or "keepalive" in ln for ln in lines)
    assert len(lines) == 5
    assert sleeps == [control.STREAM_RETRY_S]  # the clean close reconnects after the retry delay


def test_tail_sends_sse_headers_and_hits_the_stream_route(listing: FakeApi, sleeps: list[float]) -> None:
    tail_api(listing, SseResponse([]))
    assert invoke_cli(["tail"]).exit_code == 0
    [call] = listing.of("GET", "/schedule/stream")
    assert call.headers["accept"] == "text/event-stream" and "last-event-id" not in call.headers


def test_tail_current_advanced_only_with_ticks(listing: FakeApi, sleeps: list[float], frozen_now: datetime) -> None:
    advanced = {"current": sched_row(ALPHA, at_minute(6, 40), "current"), "cut": "x"}
    lines = stream_lines(frame("current.advanced", advanced, "b-1"), frame("current.advanced", {"current": None}, "b-2"))
    tail_api(listing, SseResponse(lines))
    assert "next:" not in invoke_cli(["tail"]).output
    tail_api(listing, SseResponse(lines))
    out = invoke_cli(["tail", "--ticks"]).output
    assert "next: alpha-report Mon 10-26 06:40 (in 10m)" in out
    assert "nothing scheduled" in out


def test_tail_definition_changed_line(listing: FakeApi, sleeps: list[float]) -> None:
    changed = {
        "candybarId": ID_ALPHA, "change": "updated", "changes": {"schedule": "0 * * * *"},
        "definition": {**ALPHA, "name": "alpha-v2", "schedule": "0 * * * *"},
    }
    created = {"candybarId": ID_BETA, "change": "created", "definition": BETA}
    tail_api(listing, SseResponse(stream_lines(
        frame("definition.changed", changed, "b-1"), frame("definition.changed", created, "b-2"),
    )))
    out = invoke_cli(["tail"]).output
    assert 'alpha-v2  updated {"schedule": "0 * * * *"}' in out
    assert "beta-sync  created" in out and "created {" not in out


def test_tail_learns_renames_for_later_rows_without_a_name(listing: FakeApi, sleeps: list[float]) -> None:
    rename = {"candybarId": ID_ALPHA, "change": "updated", "changes": {"name": "alpha-v2"},
              "definition": {**ALPHA, "name": "alpha-v2"}}
    manual = {**_invoked(ID_ALPHA, "manual", row=None)}
    tail_api(listing, SseResponse(stream_lines(
        frame("definition.changed", rename, "b-1"), frame("invocation.started", manual, "b-2"),
    )))
    out = invoke_cli(["tail"]).output
    assert "▶ alpha-v2  started (manual)" in out


def test_tail_manual_run_with_null_row_falls_back_to_the_registry_name(listing: FakeApi, sleeps: list[float]) -> None:
    manual = {**_invoked(ID_ALPHA, "manual", row=None), "type": "invocation.completed", "result": "success"}
    tail_api(listing, SseResponse(stream_lines(frame("invocation.completed", manual, "b-1"))))
    out = invoke_cli(["tail"]).output
    assert "✓ alpha-report  success (manual)" in out


def test_tail_null_row_for_an_unregistered_candybar_shows_its_id(listing: FakeApi, sleeps: list[float]) -> None:
    ghost = {**_invoked(ID_ALPHA, "manual", row=None), "candybarId": ID_MISSING}
    tail_api(listing, SseResponse(stream_lines(frame("invocation.started", ghost, "b-1"))))
    assert f"▶ {ID_MISSING}  started (manual)" in invoke_cli(["tail"]).output


def test_tail_hide_noise_drops_noisy_candybars(listing: FakeApi, sleeps: list[float]) -> None:
    lines = stream_lines(
        frame("invocation.started", _invoked(ID_SMOKE), "b-1"),
        frame("invocation.started", _invoked(ID_ALPHA), "b-2"),
        frame("invocation.started", _invoked(ID_SMOKE, "manual", row=None), "b-3"),  # row-less, still noise
    )
    tail_api(listing, SseResponse(lines))
    shown = invoke_cli(["tail"]).output
    assert shown.count("smoke-ping") == 2 and shown.count("alpha-report") == 1
    tail_api(listing, SseResponse(lines))
    hidden = invoke_cli(["tail", "--hide-noise"]).output
    assert "smoke-ping" not in hidden and "alpha-report" in hidden


def test_tail_hide_noise_follows_definition_changes_live(listing: FakeApi, sleeps: list[float]) -> None:
    def changed(cid: str, cand: dict[str, Any], noise: bool) -> dict[str, Any]:
        return {"candybarId": cid, "change": "updated", "changes": {"noise": noise},
                "definition": {**cand, "noise": noise}}

    tail_api(listing, SseResponse(stream_lines(
        frame("invocation.started", _invoked(ID_ALPHA), "b-1"),  # shown
        frame("definition.changed", changed(ID_ALPHA, ALPHA, True), "b-2"),  # alpha turns noisy
        frame("invocation.started", _invoked(ID_ALPHA), "b-3"),  # hidden
        frame("definition.changed", changed(ID_SMOKE, SMOKE, False), "b-4"),  # smoke turns loud
        frame("invocation.started", _invoked(ID_SMOKE), "b-5"),  # shown
    )))
    out = invoke_cli(["tail", "--hide-noise"]).output
    started = [ln for ln in out.splitlines() if "▶" in ln]
    assert [("alpha-report" in ln, "smoke-ping" in ln) for ln in started] == [(True, False), (False, True)]


def test_tail_json_prints_the_event_name_and_payload(listing: FakeApi, sleeps: list[float]) -> None:
    body = _invoked(ID_ALPHA)
    tail_api(listing, SseResponse(stream_lines(
        frame("invocation.started", body, "b-1"),
        frame("current.advanced", {"current": None}, "b-2"),  # still gated by --ticks
    )))
    lines = [json.loads(ln) for ln in invoke_cli(["tail", "--json"]).output.splitlines()]
    assert lines == [{"event": "invocation.started", **body}]


def test_tail_skips_malformed_json_and_keeps_going(listing: FakeApi, sleeps: list[float]) -> None:
    tail_api(listing, SseResponse(stream_lines(
        frame("invocation.started", b"{not json", "b-1"),
        frame("invocation.started", _invoked(ID_ALPHA), "b-2"),
    )))
    result = invoke_cli(["tail"])
    assert result.exit_code == 0 and result.output.count("▶") == 1


def test_tail_parses_crlf_multiline_data_and_ignores_unknown_fields(listing: FakeApi, sleeps: list[float]) -> None:
    payload = json.dumps(_invoked(ID_ALPHA), indent=1).encode().split(b"\n")
    raw = [b"id: b-1\r\n", b"event: invocation.started\r\n", b"weird: field\r\n"]
    raw += [b"data: " + part + b"\r\n" for part in payload] + [b"\r\n"]
    tail_api(listing, SseResponse(raw))
    assert "▶ alpha-report  started (scheduled)" in invoke_cli(["tail"]).output


def test_tail_ignores_an_event_that_is_not_worth_a_line(listing: FakeApi, sleeps: list[float]) -> None:
    tail_api(listing, SseResponse(stream_lines(frame("ready", {"cut": "x", "current": None}, "b-0"))))
    assert invoke_cli(["tail"]).output.strip() == ""


def test_tail_reconnects_with_last_event_id_after_a_drop(
    listing: FakeApi, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    control.time.sleep.budget["left"] = 2  # type: ignore[attr-defined]
    tail_api(listing,
             SseResponse(stream_lines(frame("invocation.started", _invoked(ID_ALPHA), "boot-7"),
                                      frame(None, None, "boot-8"))),  # an id-only (window-filtered) frame
             urllib.error.URLError("Connection refused"))
    result = invoke_cli(["tail"])
    assert result.exit_code == 0, result.output
    first, second = listing.of("GET", "/schedule/stream")
    assert "last-event-id" not in first.headers
    assert second.headers["last-event-id"] == "boot-8"
    assert "stream dropped" in result.stderr and "reconnecting in 3s" in result.stderr
    assert sleeps == [3.0, 3.0]


def test_tail_listing_failure_exits_1(fake: FakeApi) -> None:
    fake.on("GET", "/candybars", urllib.error.URLError("Connection refused"))
    result = invoke_cli(["tail"])
    assert result.exit_code == 1 and "unreachable" in result.output


# ---------------------------------------------------------------------------
# Contract tests: the CLI against the REAL FastAPI app and the REAL stream hub.
# Field names are checked against what the API actually returns; the registry
# is the in-memory rig of tests/test_api_skips.py (monkeypatched on
# candybar.api.app), and the API's clock and the CLI's `_now` are both NOW.


def _invocation_row(candybar_id: UUID, **kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid4(), "candybar_id": candybar_id, "trigger": "scheduled", "tick_id": None,
        "idempotency_key": "k", "result": "success", "exit_code": 0, "error_message": None,
        "stderr_tail": None, "runtime_ms": 1500, "memory_peak_bytes": 4096, "cpu_load": 0.25,
        "gpu_load": 0.0, "token_spend": 7, "cost_usd": None,
        "started_at": NOW - timedelta(minutes=10), "finished_at": NOW - timedelta(minutes=9),
    }
    return SimpleNamespace(**{**base, **kw})


class Live:
    """Routes the CLI's urlopen into the real app over the skips rig."""

    def __init__(self, registry: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
        self.rig = registry
        self.client: TestClient = skips_client(registry)
        self.runs: dict[UUID, list[SimpleNamespace]] = {}
        self.get_invocation: Callable[[UUID], Any] = lambda invocation_id: None
        self.alpha = registry.add("alpha-report", tags=["ops"], schedule="*/5 * * * *")
        self.beta = registry.add("beta-sync", tags=["ops", "sync"], schedule="0 * * * *", enabled=False)
        self.smoke = registry.add("smoke-ping", tags=["smoke"], schedule="* * * * *", noise=True)

        async def list_invocations(session: Any, candybar_id: UUID, limit: int = 50) -> list[Any]:
            return self.runs.get(candybar_id, [])[:limit]

        async def get_invocation(session: Any, invocation_id: UUID) -> Any:
            return self.get_invocation(invocation_id)

        monkeypatch.setattr(app_module, "list_invocations", list_invocations)
        monkeypatch.setattr(app_module, "get_invocation", get_invocation)
        monkeypatch.setattr(control, "_now", lambda: NOW)

    def via_app(self, request: Any, timeout: float = 0) -> Any:
        split = urlsplit(request.full_url)
        target = split.path + (f"?{split.query}" if split.query else "")
        resp = self.client.request(
            request.get_method(), target, content=request.data,
            headers={k: v for k, v in request.header_items()},
        )
        if resp.status_code >= 400:
            raise urllib.error.HTTPError(
                request.full_url, resp.status_code, "x", hdrs=None, fp=io.BytesIO(resp.content)
            )
        return _Json(resp.json())

    def cli(self, *args: str) -> Any:
        with patch("urllib.request.urlopen", side_effect=self.via_app):
            return invoke_cli(list(args))

    def facts(self, type_suffix: str) -> list[Any]:
        return [e for e in self.rig.sink.published if e.type.endswith(type_suffix)]


@pytest.fixture()
def live(skips_rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Live:  # noqa: F811
    return Live(skips_rig, monkeypatch)


def test_contract_ls_and_show_read_the_real_definition_shapes(live: Live) -> None:
    raw = live.client.get("/candybars").json()
    listed = live.cli("ls", "--all", "--json")
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output) == raw  # same keys, nothing renamed or dropped

    table = live.cli("ls")
    assert "alpha-report" in table.output and "beta-sync" not in table.output
    assert "2 shown of 3 (1 paused hidden" in table.output
    assert str(live.alpha.id) in table.output
    assert [json.loads(live.cli("ls", flag, "--json").output) for flag in ("--hide-noise",)] == [
        [c for c in raw if c["enabled"] and not c["noise"]]
    ]
    assert [c["name"] for c in json.loads(live.cli("ls", "-a", "-t", "sync", "--json").output)] == ["beta-sync"]

    live.alpha.total_invoked, live.alpha.total_successful = 6, 5
    live.runs[live.alpha.id] = [
        _invocation_row(live.alpha.id, tick_id="tick:1"),
        _invocation_row(live.alpha.id, result="fail", exit_code=2, error_message="boom", stderr_tail="trace"),
    ]
    shown = live.cli("show", "alpha-report")
    assert shown.exit_code == 0, shown.output
    assert "6 invoked, 5 succeeded, 1 failed" in shown.output
    assert "entrypoint  /bin/true" in shown.output
    assert "exit 2: boom" in shown.output and "1.5s" in shown.output and "4.0K" in shown.output
    merged = json.loads(live.cli("show", "alpha-report", "--json").output)
    assert merged["totalInvoked"] == 6 and merged["entrypoint"] == "/bin/true" and merged["runner"] is None
    assert [r["result"] for r in merged["invocations"]] == ["success", "fail"]


def test_contract_show_renders_a_runner_candybar(live: Live) -> None:
    live.alpha.runner = {"kind": "exec", "argv": ["/bin/echo", "hi"]}
    out = live.cli("show", str(live.alpha.id), "--runs", "0")
    assert out.exit_code == 0, out.output
    assert '"argv"' in out.output and "entrypoint" not in out.output


def test_contract_history_reads_the_real_invocation_rows(live: Live) -> None:
    live.runs[live.alpha.id] = [
        _invocation_row(live.alpha.id, trigger="manual"),
        _invocation_row(live.alpha.id, result="fail", exit_code=1, error_message="exit 1"),
    ]
    out = live.cli("history", "alpha-report", "-n", "5")
    assert out.exit_code == 0, out.output
    assert "manual" in out.output and "exit 1: exit 1" in out.output
    failed = json.loads(live.cli("history", "alpha-report", "--failed", "--json").output)
    assert [r["result"] for r in failed] == ["fail"]
    assert {"id", "candybarId", "result", "exitCode", "runtimeMs", "startedAt", "stderrTail"} <= set(failed[0])
    assert live.cli("history", "beta-sync").output.strip() == "no recorded runs"


def test_contract_schedule_and_next_read_the_real_rows(live: Live) -> None:
    rows = json.loads(live.cli("schedule", "--ahead", "0.25", "--json").output)
    assert rows, "the real /schedule answered no rows"
    assert {"candybarId", "name", "schedule", "fireTime", "state", "skipped", "noise"} <= set(rows[0])
    assert {r["name"] for r in rows} == {"alpha-report", "smoke-ping"}  # paused beta has no upcoming rows
    assert [r for r in rows if r["state"] == "current"], "exactly one current row"

    quiet = json.loads(live.cli("schedule", "--ahead", "0.25", "--hide-noise", "--json").output)
    assert {r["name"] for r in quiet} == {"alpha-report"}
    only = json.loads(live.cli("schedule", "-c", "alpha-report", "--ahead", "0.25", "--json").output)
    assert only == quiet

    table = live.cli("schedule", "--ahead", "0.25")
    assert table.exit_code == 0 and any(ln.startswith(">") for ln in table.output.splitlines())

    nxt = live.cli("next")
    assert nxt.exit_code == 0, nxt.output
    assert nxt.output.startswith("smoke-ping  ") and "[noise]" in nxt.output  # 06:31, the minutely job
    assert json.loads(live.cli("next", "--json").output)["state"] == "current"


def test_contract_next_with_nothing_scheduled(live: Live) -> None:
    for row in live.rig.rows.values():
        row.enabled = False
    result = live.cli("next")
    assert result.exit_code == 1 and "nothing is scheduled to fire" in result.output


def test_contract_pause_and_resume_write_the_enabled_flag(live: Live) -> None:
    paused = live.cli("pause", "alpha-report")
    assert paused.exit_code == 0, paused.output
    assert live.alpha.enabled is False
    [fact] = live.facts("candybar.updated")
    assert fact.data.changes == {"enabled": False}

    again = live.cli("pause", "alpha-report")  # the list now says paused: no write
    assert "already paused" in again.output
    assert len(live.facts("candybar.updated")) == 1 and live.rig.writes == 1

    resumed = live.cli("resume", "alpha-report")
    assert "resumed alpha-report" in resumed.output and live.alpha.enabled is True
    assert [f.data.changes for f in live.facts("candybar.updated")] == [{"enabled": False}, {"enabled": True}]


def test_contract_edit_patches_only_the_passed_fields(live: Live) -> None:
    result = live.cli("edit", "alpha-report", "--schedule", "0 3 * * *", "--tags", "x, y", "--no-noise")
    assert result.exit_code == 0, result.output
    assert live.alpha.schedule == "0 3 * * *" and live.alpha.tags == ["x", "y"]
    [fact] = live.facts("candybar.updated")
    assert fact.data.changes == {"schedule": "0 3 * * *", "tags": ["x", "y"]}  # noise unchanged: not a change
    assert "schedule: '*/5 * * * *' -> '0 3 * * *'" in result.output
    assert "tags: ['ops'] -> ['x', 'y']" in result.output

    renamed = live.cli("edit", "alpha-report", "--name", "alpha-v2", "--noise")
    assert renamed.exit_code == 0 and live.alpha.name == "alpha-v2" and live.alpha.noise is True
    same = live.cli("edit", "alpha-v2", "--schedule", "0 3 * * *")
    assert "(no change)" in same.output


def test_contract_edit_rejections_come_from_the_real_validators(live: Live) -> None:
    bad = live.cli("edit", "alpha-report", "--schedule", "not a cron")
    assert bad.exit_code == 1 and "422" in bad.output
    clash = live.cli("edit", "alpha-report", "--name", "beta-sync")
    assert clash.exit_code == 1 and "409" in clash.output
    assert live.rig.sink.published == []


def test_contract_skip_default_walks_the_real_schedule_one_fire_at_a_time(live: Live) -> None:
    first = live.cli("skip", "alpha-report")
    assert first.exit_code == 0, first.output
    assert "skipped alpha-report at" in first.output and "06:35" in first.output
    second = live.cli("skip", "alpha-report")  # the real row now says skipped: true -> next fire
    assert second.exit_code == 0, second.output
    assert "06:40" in second.output
    stored = sorted(e.fire_time for e in live.rig.stored.values())  # type: ignore[attr-defined]
    assert stored == [datetime(2026, 10, 26, 6, 35, tzinfo=UTC), datetime(2026, 10, 26, 6, 40, tzinfo=UTC)]
    assert [f.data.reason for f in live.facts("candybar.skipped")] == ["occurrence", "occurrence"]
    assert [r["skipped"] for r in json.loads(live.cli("schedule", "-c", "alpha-report", "--ahead", "0.2", "--json").output)] == [
        False, True, True,  # 06:30 already fired (past), then the two skipped fires
    ]


def test_contract_skip_default_for_a_paused_candybar_stores_its_next_cron_fire(live: Live) -> None:
    """The real API stores a skip for a paused candybar's cron fire; it covers
    that fire if the candybar is resumed before it."""
    result = live.cli("skip", "beta-sync")
    assert result.exit_code == 0, result.output
    stored = [e.fire_time for e in live.rig.stored.values()]  # type: ignore[attr-defined]
    assert stored == [datetime(2026, 10, 26, 7, 0, tzinfo=UTC)]


def test_contract_skip_at_today_and_date(live: Live) -> None:
    at = live.cli("skip", "alpha-report", "--at", "2026-10-26T07:05:00Z")
    assert at.exit_code == 0, at.output
    off_grid = live.cli("skip", "alpha-report", "--at", "2026-10-26T07:03:00Z")
    assert off_grid.exit_code == 1 and "never fires at" in off_grid.output

    today = live.cli("skip", "alpha-report", "--today")
    assert today.exit_code == 0 and "for 2026-10-26 (UTC)" in today.output
    other = live.cli("skip", "alpha-report", "--date", "2026-10-28")
    assert other.exit_code == 0 and "for 2026-10-28" in other.output
    days = sorted(e.day for e in live.rig.stored.values() if e.day)  # type: ignore[attr-defined]
    assert [d.isoformat() for d in days] == ["2026-10-26", "2026-10-28"]
    assert [f.data.reason for f in live.facts("candybar.skipped")] == [
        "occurrence", "rest_of_day", "rest_of_day",
    ]
    repeat = live.cli("skip", "alpha-report", "--today")  # the API answers 200 for a repeat
    assert repeat.exit_code == 0 and len(live.facts("candybar.skipped")) == 3


def test_contract_skip_today_follows_the_candybars_timezone(live: Live) -> None:
    live.alpha.timezone = "Pacific/Kiritimati"  # UTC+14: 06:30 UTC on the 26th is 20:30 on the 26th there
    live.beta.timezone = "Pacific/Pago_Pago"  # UTC-11: still the 25th
    live.beta.enabled = True
    live.beta.created_at = datetime(2026, 10, 1, tzinfo=UTC)  # registered before the 25th
    live.beta.schedule = "0 12 * * *"
    plus = live.cli("skip", "alpha-report", "--today")
    assert plus.exit_code == 0, plus.output
    minus = live.cli("skip", "beta-sync", "--today")
    assert minus.exit_code == 0, minus.output
    assert "for 2026-10-26 (Pacific/Kiritimati)" in plus.output
    assert "for 2026-10-25 (Pacific/Pago_Pago)" in minus.output


def test_contract_run_wait_follows_the_real_invoke_and_invocation_routes(live: Live) -> None:
    polls: list[UUID] = []
    clock = Clock()

    def script(invocation_id: UUID) -> Any:
        polls.append(invocation_id)
        if len(polls) == 1:
            return None  # the real route's 404: the executor has not claimed it yet
        row = _invocation_row(live.alpha.id, id=invocation_id, trigger="manual", result=None,
                              exit_code=None, runtime_ms=None, finished_at=None)
        if len(polls) >= 3:
            row.result, row.exit_code, row.runtime_ms = "success", 0, 900
        return row

    live.get_invocation = script
    real = control.wait_for_invocation
    with patch.object(control, "wait_for_invocation",
                      lambda api, i, t: real(api, i, t, sleep=clock.sleep, clock=clock.now)):
        result = live.cli("run", "alpha-report", "--wait")
    assert result.exit_code == 0, result.output
    [command] = [e for e in live.rig.sink.published if e.type.endswith("invoke")]
    assert str(command.data.invocation_id) in result.output
    assert set(polls) == {command.data.invocation_id} and len(polls) == 3
    assert "success in 900ms (exit 0, mem 4.0K, tokens 7)" in result.output


def test_contract_run_wait_reports_a_failed_run(live: Live) -> None:
    live.get_invocation = lambda i: _invocation_row(
        live.alpha.id, id=i, trigger="manual", result="fail", exit_code=1,
        error_message="exited with code 1", stderr_tail="kaboom\n",
    )
    result = live.cli("run", str(live.alpha.id), "--wait")
    assert result.exit_code == 1
    assert "error: exited with code 1" in result.output and "kaboom" in result.output


def test_contract_run_wait_times_out_with_exit_3(live: Live) -> None:
    clock = Clock()
    live.get_invocation = lambda i: _invocation_row(live.alpha.id, id=i, result=None, exit_code=None)
    real = control.wait_for_invocation
    with patch.object(control, "wait_for_invocation",
                      lambda api, i, t: real(api, i, t, sleep=clock.sleep, clock=clock.now)):
        result = live.cli("run", "alpha-report", "--wait", "--timeout", "3")
    assert result.exit_code == 3 and "still running after 3s" in result.output


def test_contract_history_with_an_in_flight_row_from_the_real_api(live: Live) -> None:
    """The real invocation_row_json emits result=null for a claimed, unfinished run."""
    live.runs[live.alpha.id] = [_invocation_row(live.alpha.id, result=None, exit_code=None,
                                                runtime_ms=None, finished_at=None)]
    assert live.cli("history", "alpha-report", "--json").exit_code == 0  # the API side is fine
    result = live.cli("history", "alpha-report")
    assert result.exit_code == 0, repr(result.exception)


# --- tail against the real hub ----------------------------------------------


def _real_stream(live: Live, monkeypatch: pytest.MonkeyPatch, facts: list[Any]) -> list[bytes]:
    """Feed facts to a real ScheduleStream and return its replayed SSE bytes."""
    for name in ("list_candybars", "get_candybar", "list_skips", "fire_results"):
        monkeypatch.setattr(stream_module, name, getattr(app_module, name))

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def session() -> Any:
        yield object()

    async def go() -> str:
        hub = ScheduleStream(session_factory=session, now=lambda: NOW)
        for fact in facts:
            await hub.handle(fact)
        gen = hub.frames(last_event_id=f"{hub.boot}-0")  # replay everything buffered
        text = [await gen.__anext__()]  # the retry preamble
        text += [await gen.__anext__() for _ in range(hub._seq)]
        await gen.aclose()
        return "".join(text)

    return asyncio.run(go()).encode().splitlines(keepends=True)


def _tid(fire: datetime) -> str:
    return tick_id_for_minute(int(fire.timestamp()) // 60)


def test_contract_tail_renders_the_real_stream_events(live: Live, monkeypatch: pytest.MonkeyPatch) -> None:
    a, s = live.alpha, live.smoke
    fire, smoke_fire = at_minute(6, 35), at_minute(6, 31)
    facts = [
        build_candybar_invoked(candybar_id=a.id, name=a.name, invocation_id=uuid4(), trigger="scheduled", tick_id=_tid(fire)),
        build_candybar_completed(candybar_id=a.id, name=a.name, invocation_id=uuid4(), trigger="scheduled", tick_id=_tid(fire), exit_code=0, runtime_ms=5),
        build_candybar_failed(candybar_id=a.id, name=a.name, invocation_id=uuid4(), trigger="scheduled", tick_id=_tid(at_minute(6, 30)), exit_code=1, runtime_ms=5, stderr="x"),
        build_candybar_skipped(candybar_id=a.id, name=a.name, tick_id=_tid(at_minute(6, 40)), reason="occurrence"),
        build_candybar_invoked(candybar_id=s.id, name=s.name, invocation_id=uuid4(), trigger="scheduled", tick_id=_tid(smoke_fire)),
        # a manual run: the real hub sends row: null, so the CLI must name it from the registry
        build_candybar_completed(candybar_id=a.id, name=a.name, invocation_id=uuid4(), trigger="manual", tick_id=None, exit_code=0, runtime_ms=5),
        build_candybar_updated(candybar_id=a.id, name="alpha-v2", changes={"name": "alpha-v2", "noise": True}),
    ]
    lines = _real_stream(live, monkeypatch, facts)

    def run_tail(*flags: str) -> str:
        calls = {"n": 0}

        def stop(_: float) -> None:
            raise KeyboardInterrupt

        api = FakeApi().on("GET", "/candybars", live.client.get("/candybars").json())
        api.on("GET", "/schedule/stream", SseResponse(lines))
        with patch("urllib.request.urlopen", side_effect=api), patch.object(control.time, "sleep", stop):
            result = invoke_cli(["tail", *flags])
        assert result.exit_code == 0, result.output
        del calls
        return result.output

    out = run_tail()
    assert "▶ alpha-report  started (scheduled)" in out
    assert "✓ alpha-report  success (scheduled)" in out
    assert "✗ alpha-report  fail (scheduled)" in out
    assert "⤼ alpha-report  skipped (occurrence)" in out
    assert "▶ smoke-ping  started (scheduled)" in out
    assert "✓ alpha-report  success (manual)" in out  # name fallback for row: null
    assert 'alpha-v2  updated {"name": "alpha-v2", "noise": true}' in out

    quiet = run_tail("--hide-noise")
    assert "smoke-ping" not in quiet and "alpha-report  success (scheduled)" in quiet

    events = [json.loads(ln) for ln in run_tail("--json").splitlines()]
    assert [e["event"] for e in events].count("invocation.started") == 2
    assert all("candybarId" in e for e in events if e["event"] != "current.advanced")
