"""CLI for the steward contract: archive / unarchive, `ls --archived`, `steward ...`, `profile`.

Same urlopen-routing fake as tests/test_cli_control.py: every command is a
thin wrapper over the REST client, so these pin the requests it sends and
what it prints.
"""

from __future__ import annotations

import io
import json
import urllib.error
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

import candybar.cli.steward as steward_cli
from test_api_steward import evaluation, overview
from test_cli_control import (
    ALL,
    BASE,
    ID_ALPHA,
    ID_BETA,
    FakeApi,
    Seq,
    cb,
    invoke_cli,
)

ARCHIVED_ID = "01900000-0000-7000-8000-0000000000ad"
ARCHIVED = cb("old-job", ARCHIVED_ID, enabled=False, archived=True)
TASK_ID = "01a1a000-0000-7000-8000-000000000001"


@pytest.fixture(autouse=True)
def api_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANDYBAR_API_URL", BASE)


@pytest.fixture()
def fake() -> Iterator[FakeApi]:
    api = FakeApi()
    with patch("urllib.request.urlopen", side_effect=api):
        yield api


@pytest.fixture()
def listing(fake: FakeApi) -> FakeApi:
    return fake.on("GET", "/candybars", [*ALL, ARCHIVED])


@pytest.fixture()
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    real = steward_cli.wait_for_task

    def fast(*args: Any, **kwargs: Any) -> Any:
        kwargs["sleep"] = lambda s: None
        return real(*args, **kwargs)

    monkeypatch.setattr(steward_cli, "wait_for_task", fast)


def task(status: str, **kw: Any) -> dict[str, Any]:
    return {
        "id": TASK_ID, "candybarId": ID_ALPHA, "action": kw.pop("action", "enrich"), "status": status,
        "model": "automaticai/auto/fast", "parentTaskId": None, "sessionId": kw.pop("sessionId", None),
        "outcome": None, "summary": None, "reportKey": None, "invocationId": None, "error": None,
        "createdAt": "2026-11-03T12:00:00+00:00", "startedAt": None, "finishedAt": None, **kw,
    }


def conflict(task_id: str) -> urllib.error.HTTPError:
    body = json.dumps({"type": "about:blank", "title": "Steward task already active", "status": 409,
                       "detail": "already active", "taskId": task_id})
    return urllib.error.HTTPError(url="http://x", code=409, msg="conflict", hdrs=None, fp=io.BytesIO(body.encode()))


def test_ls_hides_archived_unless_asked(listing: FakeApi) -> None:
    out = invoke_cli(["ls", "--all"])
    assert out.exit_code == 0 and "old-job" not in out.output
    assert "1 archived hidden; --archived to include" in out.output
    out = invoke_cli(["ls", "--archived"])
    assert "old-job" in out.output and "archived" in out.output
    assert "beta-sync" not in out.output  # paused stays hidden without --all
    rows = json.loads(invoke_cli(["ls", "--archived", "--all", "--json"]).output)
    assert {r["name"] for r in rows} == {"alpha-report", "beta-sync", "smoke-ping", "old-job"}


def test_archive_and_unarchive(listing: FakeApi) -> None:
    listing.on("POST", f"/candybars/{ID_ALPHA}/archived", {**ALL[0], "archived": True, "enabled": False})
    out = invoke_cli(["archive", "alpha-report"])
    assert out.exit_code == 0 and "archived alpha-report" in out.output
    assert listing.of("POST", f"/candybars/{ID_ALPHA}/archived")[0].body == {"archived": True}
    listing.on("POST", f"/candybars/{ARCHIVED_ID}/archived", {**ARCHIVED, "archived": False})
    out = invoke_cli(["unarchive", "old-job"])
    assert out.exit_code == 0 and "still paused" in out.output and "candybar resume old-job" in out.output
    assert listing.of("POST", f"/candybars/{ARCHIVED_ID}/archived")[0].body == {"archived": False}
    out = invoke_cli(["archive", "old-job"])
    assert "already archived" in out.output


def test_steward_request_without_wait(listing: FakeApi) -> None:
    listing.on("POST", f"/candybars/{ID_ALPHA}/steward", task("queued"))
    out = invoke_cli(["steward", "enrich", "alpha-report"])
    assert out.exit_code == 0 and f"task {TASK_ID}" in out.output
    assert listing.of("POST", f"/candybars/{ID_ALPHA}/steward")[0].body == {"action": "enrich"}


def test_steward_wait_follows_to_the_outcome(listing: FakeApi, no_sleep: None) -> None:
    listing.on("POST", f"/candybars/{ID_ALPHA}/steward", task("queued", action="evaluate"))
    done = task("succeeded", action="evaluate", outcome="Nominal", summary="Fine.", reportKey="2026-11-03/candybar-evaluate-alpha-report.md", sessionId="ses_1")
    listing.on("GET", f"/candybars/{ID_ALPHA}/steward/tasks", Seq(
        {"candybarId": ID_ALPHA, "tasks": [task("queued", action="evaluate")]},
        {"candybarId": ID_ALPHA, "tasks": [task("running", action="evaluate", sessionId="ses_1")]},
        {"candybarId": ID_ALPHA, "tasks": [done]},
    ))
    out = invoke_cli(["steward", "evaluate", "alpha-report", "--wait"])
    assert out.exit_code == 0, out.output
    assert "running (session ses_1)" in out.output and "outcome  Nominal" in out.output
    assert "delo/daily-reports/2026-11-03/candybar-evaluate-alpha-report.md" in out.output


def test_steward_wait_on_a_failed_task_exits_1(listing: FakeApi, no_sleep: None) -> None:
    listing.on("POST", f"/candybars/{ID_ALPHA}/steward", task("queued"))
    listing.on("GET", f"/candybars/{ID_ALPHA}/steward/tasks", {"tasks": [task("failed", error="OpenCode timed out")]})
    out = invoke_cli(["steward", "enrich", "alpha-report", "--wait"])
    assert out.exit_code == 1 and "OpenCode timed out" in out.output


def test_steward_conflict_follows_the_active_task_with_wait(listing: FakeApi, no_sleep: None) -> None:
    listing.on("POST", f"/candybars/{ID_ALPHA}/steward", conflict(TASK_ID))
    listing.on("GET", f"/candybars/{ID_ALPHA}/steward/tasks", {"tasks": [task("succeeded", outcome="done")]})
    out = invoke_cli(["steward", "enrich", "alpha-report", "--wait"])
    assert out.exit_code == 0 and f"following task {TASK_ID}" in out.output
    listing.on("POST", f"/candybars/{ID_ALPHA}/steward", conflict(TASK_ID))
    assert invoke_cli(["steward", "enrich", "alpha-report"]).exit_code == 1


def test_steward_tasks_lists(listing: FakeApi) -> None:
    listing.on("GET", "/steward/tasks", {"tasks": [task("running", summary=None)]})
    out = invoke_cli(["steward", "tasks", "--active"])
    assert out.exit_code == 0 and "alpha-report" in out.output and "running" in out.output
    assert listing.of("GET", "/steward/tasks")[0].query == {"active": ["1"]}
    listing.on("GET", f"/candybars/{ID_BETA}/steward/tasks", {"tasks": []})
    out = invoke_cli(["steward", "tasks", "--candybar", "beta-sync"])
    assert "no steward tasks" in out.output


def test_profile_prints_overview_project_and_evaluation(listing: FakeApi) -> None:
    listing.on("GET", f"/candybars/{ID_ALPHA}/profile", {
        "candybarId": ID_ALPHA, "overview": overview(), "templateVersion": 1,
        "project": {"id": "docker", "name": "Docker", "confidence": 1.0, "method": "ledger"},
        "enrichedAt": "2026-11-03T12:00:00+00:00", "enrichTaskId": TASK_ID,
        "evaluation": evaluation(outcome="Suboptimal", followUp="refactor"), "updatedAt": "2026-11-03T12:00:00+00:00",
    })
    out = invoke_cli(["profile", "alpha-report"])
    assert out.exit_code == 0, out.output
    assert "project: Docker (docker, ledger, 1.00)" in out.output
    assert "Nightly restic snapshot" in out.output and "evaluation: Suboptimal" in out.output
    assert "follow-up: refactor" in out.output


def test_steward_cancel_marks_failed_and_interrupts_the_session(fake: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    import candybar.steward.opencode as oc

    interrupted: list[str] = []
    monkeypatch.setattr(oc.Endpoint, "discover", classmethod(lambda cls: oc.Endpoint("http://opencode.test", None)))
    monkeypatch.setattr(oc.OpenCodeClient, "interrupt", lambda self, endpoint, session: interrupted.append(session))
    cancelled = task("failed", action="fix", sessionId="ses_live", error="cancelled by operator")
    fake.on("POST", f"/steward/tasks/{TASK_ID}/cancel", cancelled)
    out = invoke_cli(["steward", "cancel", TASK_ID])
    assert out.exit_code == 0, out.output
    assert interrupted == ["ses_live"] and "cancelled by operator" in out.output
    assert "interrupted OpenCode session ses_live" in out.output


def test_steward_cancel_of_a_finished_task_fails(fake: FakeApi) -> None:
    body = json.dumps({"type": "about:blank", "title": "Steward task already finished", "status": 409,
                       "detail": f"task {TASK_ID} is already succeeded"})
    fake.on("POST", f"/steward/tasks/{TASK_ID}/cancel", urllib.error.HTTPError(
        url="http://x", code=409, msg="conflict", hdrs=None, fp=io.BytesIO(body.encode())))
    out = invoke_cli(["steward", "cancel", TASK_ID])
    assert out.exit_code == 1 and "already succeeded" in out.output
