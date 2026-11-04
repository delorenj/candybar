"""Steward contract over the API: archive, profiles, steward tasks (steward-contract.md 1-3).

Store functions are monkeypatched at the app module with an in-memory
registry (Story 5.1's rig, tests/test_api_definitions.py, extended with
profiles and tasks); the live-Postgres round trip is
tests/test_steward_integration.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

import candybar.api.app as app_module
from candybar.registry import DuplicateActiveTaskError
from contract_shapes import SINGLE_KEYS, assert_contract_shape
from test_api_definitions import Rig, rig  # noqa: F401  (fixture)

PROBLEM = "application/problem+json"
UPDATED = "bloodbank.cron.candybar.updated"


def overview(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "summary": "Nightly restic snapshot of the vault.",
        "purpose": "Keeps an offsite history of the vault. The operator relies on it for restores.",
        "behavior": ["unlock the repo", "back up", "forget and prune"],
        "inputs": ["op://DeLoSecrets/x/password"],
        "outputs": ["a restic snapshot in gdrive:backups/delodocs"],
        "sideEffects": ["uploads to Google Drive"],
        "dependencies": ["restic", "rclone"],
        "failureModes": ["gdrive token expired: exit 1"],
        "verification": ["restic snapshots --latest 1"],
        "runbook": ["systemctl --user start restic-vault.service"],
        "source": {"repo": "delorenj/DeLoContainers", "paths": ["systemd/user/restic-vault.service"], "runner": "systemd"},
        "cadenceNote": "Daily fits a vault that changes daily.",
    }
    body.update(over)
    return body


def evaluation(**over: Any) -> dict[str, Any]:
    axis = {"rating": "good", "finding": "fine"}
    body: dict[str, Any] = {
        "outcome": "Nominal",
        "summary": "Works and is used.",
        "axes": {k: dict(axis) for k in ("efficacy", "cost", "value", "usage", "alternatives")},
        "recommendation": "Keep it.",
        "followUp": "none",
        "reportKey": "2026-11-03/candybar-evaluate-alpha.md",
        "evaluatedAt": "2026-11-03T12:00:00Z",
        "taskId": str(uuid4()),
    }
    body.update(over)
    return body


@dataclass
class Profile:
    candybar_id: UUID
    updated_at: datetime
    overview: dict | None = None
    template_version: int | None = None
    project: dict | None = None
    enriched_at: datetime | None = None
    enrich_task_id: UUID | None = None
    evaluation: dict | None = None


@dataclass
class Task:
    id: UUID
    candybar_id: UUID
    action: str
    model: str
    created_at: datetime
    status: str = "queued"
    parent_task_id: UUID | None = None
    session_id: str | None = None
    outcome: str | None = None
    summary: str | None = None
    report_key: str | None = None
    invocation_id: UUID | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cost_usd: float | None = None
    tokens: dict | None = None


@dataclass
class Store:
    profiles: dict[UUID, Profile] = field(default_factory=dict)
    tasks: dict[UUID, Task] = field(default_factory=dict)
    race: bool = False


@pytest.fixture()
def st(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> tuple[Rig, Store]:  # noqa: F811
    store = Store()

    async def get_profile(session: Any, candybar_id: UUID) -> Profile | None:
        return store.profiles.get(candybar_id)

    async def list_profiles(session: Any) -> list[Profile]:
        return sorted(store.profiles.values(), key=lambda p: p.candybar_id)

    async def upsert_profile(session: Any, candybar_id: UUID, changes: dict[str, Any]) -> Profile:
        row = store.profiles.setdefault(candybar_id, Profile(candybar_id, datetime.now(UTC)))
        for key, value in changes.items():
            setattr(row, key, value)
        row.updated_at = datetime.now(UTC)
        return row

    async def active_steward_task(session: Any, candybar_id: UUID, action: str) -> Task | None:
        for task in store.tasks.values():
            if task.candybar_id == candybar_id and task.action == action and task.status in ("queued", "running"):
                return task
        return None

    async def insert_steward_task(session: Any, **kw: Any) -> Task:
        if store.race:
            raise DuplicateActiveTaskError(kw["candybar_id"], kw["action"])
        task = Task(
            id=kw["task_id"], candybar_id=kw["candybar_id"], action=kw["action"], model=kw["model"],
            created_at=kw.get("created_at") or datetime.now(UTC), parent_task_id=kw.get("parent_task_id"),
        )
        store.tasks[task.id] = task
        return task

    async def list_steward_tasks(session: Any, *, candybar_id: Any = None, active: bool = False, limit: int = 100) -> list[Task]:
        rows = [
            t for t in store.tasks.values()
            if (candybar_id is None or t.candybar_id == candybar_id) and (not active or t.status in ("queued", "running"))
        ]
        return sorted(rows, key=lambda t: (t.created_at, t.id), reverse=True)[:limit]

    async def get_steward_task(session: Any, task_id: UUID, *, lock: bool = False) -> Task | None:
        return store.tasks.get(task_id)

    async def update_steward_task(session: Any, row: Task, changes: dict[str, Any]) -> Task:
        for key, value in changes.items():
            setattr(row, key, value)
        return row

    for name, fn in [
        ("get_profile", get_profile), ("list_profiles", list_profiles), ("upsert_profile", upsert_profile),
        ("active_steward_task", active_steward_task), ("insert_steward_task", insert_steward_task),
        ("list_steward_tasks", list_steward_tasks), ("get_steward_task", get_steward_task),
        ("update_steward_task", update_steward_task),
    ]:
        monkeypatch.setattr(app_module, name, fn)
    return rig, store


# ---------------------------------------------------------------------------
# Archive (section 1).


def test_archive_disables_in_one_fact_and_one_write(st: tuple[Rig, Store]) -> None:
    r, _ = st
    row = r.add("alpha")
    resp = r.client().post(f"/candybars/{row.id}/archived", json={"archived": True})
    assert resp.status_code == 200
    body = resp.json()
    assert_contract_shape(body, SINGLE_KEYS)
    assert body["archived"] is True and body["enabled"] is False
    assert r.writes == 1
    [fact] = r.sink.published
    assert fact.type == UPDATED and fact.data.changes == {"archived": True, "enabled": False}


def test_archive_of_a_paused_candybar_reports_only_archived(st: tuple[Rig, Store]) -> None:
    r, _ = st
    row = r.add("alpha", enabled=False)
    assert r.client().post(f"/candybars/{row.id}/archived", json={"archived": True}).status_code == 200
    assert [f.data.changes for f in r.sink.published] == [{"archived": True}]


def test_archive_twice_is_a_no_op(st: tuple[Rig, Store]) -> None:
    r, _ = st
    row = r.add("alpha", enabled=False, archived=True)
    resp = r.client().post(f"/candybars/{row.id}/archived", json={"archived": True})
    assert resp.status_code == 200 and r.sink.published == [] and r.writes == 0


def test_unarchive_leaves_it_disabled(st: tuple[Rig, Store]) -> None:
    r, _ = st
    row = r.add("alpha", enabled=False, archived=True)
    resp = r.client().post(f"/candybars/{row.id}/archived", json={"archived": False})
    assert resp.status_code == 200
    assert resp.json()["archived"] is False and resp.json()["enabled"] is False
    assert [f.data.changes for f in r.sink.published] == [{"archived": False}]


def test_enabling_an_archived_candybar_is_a_409(st: tuple[Rig, Store]) -> None:
    r, _ = st
    row = r.add("alpha", enabled=False, archived=True)
    resp = r.client().post(f"/candybars/{row.id}/enabled", json={"enabled": True})
    assert resp.status_code == 409 and resp.headers["content-type"] == PROBLEM
    assert "unarchive" in resp.json()["detail"]
    assert row.enabled is False and r.sink.published == []
    # Disabling (already disabled) stays a harmless no-op.
    assert r.client().post(f"/candybars/{row.id}/enabled", json={"enabled": False}).status_code == 200


def test_archive_bus_failure_changes_nothing(st: tuple[Rig, Store]) -> None:
    r, _ = st
    row = r.add("alpha")
    r.sink.fail = True
    resp = r.client().post(f"/candybars/{row.id}/archived", json={"archived": True})
    assert resp.status_code == 503 and "archived flag is unchanged" in resp.json()["detail"]
    assert row.archived is False and row.enabled is True and r.writes == 0


@pytest.mark.parametrize("body", [{}, {"archived": "nope"}, {"archived": None}, {"archived": True, "x": 1}])
def test_archive_body_is_the_wanted_state(st: tuple[Rig, Store], body: dict[str, Any]) -> None:
    r, _ = st
    row = r.add("alpha")
    assert r.client().post(f"/candybars/{row.id}/archived", json=body).status_code == 422


def test_archive_unknown_is_a_404(st: tuple[Rig, Store]) -> None:
    r, _ = st
    assert r.client().post(f"/candybars/{uuid4()}/archived", json={"archived": True}).status_code == 404


def test_list_carries_archived(st: tuple[Rig, Store]) -> None:
    r, _ = st
    r.add("alpha", archived=True, enabled=False)
    r.add("beta")
    listed = {c["name"]: c["archived"] for c in r.client().get("/candybars").json()}
    assert listed == {"alpha": True, "beta": False}


# ---------------------------------------------------------------------------
# Profiles (section 2).


def test_profile_is_empty_until_written(st: tuple[Rig, Store]) -> None:
    r, _ = st
    row = r.add("alpha")
    body = r.client().get(f"/candybars/{row.id}/profile").json()
    assert body == {
        "candybarId": str(row.id), "overview": None, "templateVersion": None, "project": None,
        "enrichedAt": None, "enrichTaskId": None, "evaluation": None, "updatedAt": None,
    }
    assert r.client().get(f"/candybars/{uuid4()}/profile").status_code == 404


def test_profile_put_merges_per_top_level_key(st: tuple[Rig, Store]) -> None:
    r, store = st
    row = r.add("alpha")
    client = r.client()
    task_id = str(uuid4())
    resp = client.put(
        f"/candybars/{row.id}/profile",
        json={"overview": overview(), "templateVersion": 1, "enrichedAt": "2026-11-03T12:00:00+01:00", "enrichTaskId": task_id},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enrichedAt"] == "2026-11-03T11:00:00+00:00"
    resp = client.put(
        f"/candybars/{row.id}/profile",
        json={"project": {"id": "docker", "name": "Docker", "confidence": 1, "method": "manual"}},
    )
    body = resp.json()
    assert body["overview"]["summary"].startswith("Nightly") and body["templateVersion"] == 1
    assert body["project"]["method"] == "manual" and body["enrichTaskId"] == task_id
    assert body["updatedAt"] is not None
    assert client.get("/profiles").json() == {"profiles": [body]}
    assert store.profiles[row.id].overview["source"]["runner"] == "systemd"


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"overview": overview(summary="x" * 201)},
        {"overview": {k: v for k, v in overview().items() if k != "runbook"}},
        {"overview": overview(extra="nope")},
        {"overview": overview(source={"repo": None, "paths": [], "runner": "cron"})},
        {"overview": overview(behavior=["back up", ""])},
        {"overview": overview(outputs=["   "])},
        {"overview": overview(source={"repo": None, "paths": [""], "runner": "systemd"})},
        {"evaluation": evaluation(followUpStatus="maybe")},
        {"evaluation": evaluation(outcome="Fine")},
        {"evaluation": evaluation(axes={"efficacy": {"rating": "good", "finding": "x"}})},
        {"evaluation": evaluation(evaluatedAt="2026-11-03T12:00:00")},
        {"project": {"id": "x", "name": "X", "confidence": 2, "method": "jev"}},
        {"unknown": 1},
    ],
)
def test_profile_put_validates_the_shapes(st: tuple[Rig, Store], patch: dict[str, Any]) -> None:
    r, store = st
    row = r.add("alpha")
    resp = r.client().put(f"/candybars/{row.id}/profile", json=patch)
    assert resp.status_code == 422 and resp.headers["content-type"] == PROBLEM
    assert store.profiles == {}


def test_profile_put_accepts_an_evaluation_and_null_clears(st: tuple[Rig, Store]) -> None:
    r, _ = st
    row = r.add("alpha")
    client = r.client()
    resp = client.put(f"/candybars/{row.id}/profile", json={"evaluation": evaluation(outcome="Drifted", followUp="fix")})
    assert resp.status_code == 200 and resp.json()["evaluation"]["outcome"] == "Drifted"
    assert resp.json()["evaluation"]["evaluatedAt"] == "2026-11-03T12:00:00Z"
    resp = client.put(f"/candybars/{row.id}/profile", json={"evaluation": None})
    assert resp.status_code == 200 and resp.json()["evaluation"] is None
    follow = str(uuid4())
    marked = evaluation(outcome="Drifted", followUp="fix", followUpStatus="queued", followUpTaskId=follow)
    resp = client.put(f"/candybars/{row.id}/profile", json={"evaluation": marked})
    assert resp.status_code == 200
    assert resp.json()["evaluation"]["followUpStatus"] == "queued" and resp.json()["evaluation"]["followUpTaskId"] == follow


def test_profile_put_unknown_candybar_is_a_404(st: tuple[Rig, Store]) -> None:
    r, _ = st
    assert r.client().put(f"/candybars/{uuid4()}/profile", json={"templateVersion": 1}).status_code == 404


# ---------------------------------------------------------------------------
# Steward tasks (section 3).


def test_steward_request_queues_a_task_command_first(st: tuple[Rig, Store]) -> None:
    r, store = st
    row = r.add("alpha")
    resp = r.client().post(f"/candybars/{row.id}/steward", json={"action": "enrich"})
    assert resp.status_code == 202, resp.text
    task = resp.json()
    assert set(task) == {
        "id", "candybarId", "action", "status", "model", "parentTaskId", "sessionId", "outcome",
        "summary", "reportKey", "invocationId", "error", "createdAt", "startedAt", "finishedAt",
        "costUsd", "tokens",
    }
    assert task["costUsd"] is None and task["tokens"] is None
    assert task["status"] == "queued" and task["model"] == "automaticai/auto/fast"
    assert task["candybarId"] == str(row.id) and task["action"] == "enrich"
    command, fact = r.sink.published
    assert command.type == "bloodbank.cron.candybar.steward"
    assert command.subject == "bloodbank.cmd.cron.candybar.steward"
    assert command.idempotency_key == task["id"] == str(command.id) == str(command.data.task_id)
    assert command.data.action == "enrich" and command.data.candybar_id == row.id
    assert fact.type == "bloodbank.cron.steward_task.queued" and fact.data.status == "queued"
    assert fact.data.name == "alpha" and str(fact.data.task_id) == task["id"]
    assert fact.data.created_at.isoformat() == task["createdAt"]
    assert UUID(task["id"]) in store.tasks


def test_evaluate_uses_the_auto_model(st: tuple[Rig, Store]) -> None:
    r, _ = st
    row = r.add("alpha")
    resp = r.client().post(f"/candybars/{row.id}/steward", json={"action": "evaluate"})
    assert resp.status_code == 202 and resp.json()["model"] == "automaticai/auto"


def test_a_second_active_task_is_a_409_with_its_id(st: tuple[Rig, Store]) -> None:
    r, _ = st
    row = r.add("alpha")
    client = r.client()
    first = client.post(f"/candybars/{row.id}/steward", json={"action": "fix"}).json()
    resp = client.post(f"/candybars/{row.id}/steward", json={"action": "fix"})
    assert resp.status_code == 409 and resp.json()["taskId"] == first["id"]
    # Another action is independent.
    assert client.post(f"/candybars/{row.id}/steward", json={"action": "enrich"}).status_code == 202


def test_a_fix_waits_for_an_active_refactor(st: tuple[Rig, Store]) -> None:
    r, store = st
    row = r.add("alpha")
    [refactor] = _seed(store, row.id, 1, status="running", action="refactor")
    client = r.client()
    resp = client.post(f"/candybars/{row.id}/steward", json={"action": "fix"})
    assert resp.status_code == 409 and resp.json()["taskId"] == str(refactor.id)
    assert "refactor" in resp.json()["detail"]
    # Read-only actions are not held up by a mutating one.
    assert client.post(f"/candybars/{row.id}/steward", json={"action": "evaluate"}).status_code == 202
    refactor.status = "succeeded"
    assert client.post(f"/candybars/{row.id}/steward", json={"action": "fix"}).status_code == 202


def test_insert_race_is_a_409(st: tuple[Rig, Store]) -> None:
    r, store = st
    row = r.add("alpha")
    store.race = True
    resp = r.client().post(f"/candybars/{row.id}/steward", json={"action": "enrich"})
    assert resp.status_code == 409 and resp.headers["content-type"] == PROBLEM


@pytest.mark.parametrize("body", [{"action": "refactor"}, {"action": "nope"}, {}, {"action": "fix", "x": 1}])
def test_refactor_and_junk_are_never_accepted(st: tuple[Rig, Store], body: dict[str, Any]) -> None:
    r, store = st
    row = r.add("alpha")
    assert r.client().post(f"/candybars/{row.id}/steward", json=body).status_code == 422
    assert store.tasks == {} and r.sink.published == []


def test_archived_may_be_enriched_and_evaluated_not_fixed(st: tuple[Rig, Store]) -> None:
    r, _ = st
    row = r.add("alpha", archived=True, enabled=False)
    client = r.client()
    assert client.post(f"/candybars/{row.id}/steward", json={"action": "fix"}).status_code == 409
    assert client.post(f"/candybars/{row.id}/steward", json={"action": "enrich"}).status_code == 202
    assert client.post(f"/candybars/{row.id}/steward", json={"action": "evaluate"}).status_code == 202


def test_steward_unknown_candybar_is_a_404(st: tuple[Rig, Store]) -> None:
    r, _ = st
    assert r.client().post(f"/candybars/{uuid4()}/steward", json={"action": "enrich"}).status_code == 404


def test_steward_bus_failure_queues_nothing(st: tuple[Rig, Store]) -> None:
    r, store = st
    row = r.add("alpha")
    r.sink.fail = True
    resp = r.client().post(f"/candybars/{row.id}/steward", json={"action": "enrich"})
    assert resp.status_code == 503 and store.tasks == {}


def _seed(store: Store, candybar_id: UUID, n: int, *, status: str = "succeeded", action: str = "enrich") -> list[Task]:
    base = datetime(2026, 11, 3, tzinfo=UTC)
    out = []
    for i in range(n):
        task = Task(id=uuid4(), candybar_id=candybar_id, action=action, model="m", created_at=base + timedelta(minutes=i), status=status)
        store.tasks[task.id] = task
        out.append(task)
    return out


def test_candybar_task_list_is_newest_first_and_bounded(st: tuple[Rig, Store]) -> None:
    r, store = st
    row = r.add("alpha")
    tasks = _seed(store, row.id, 25)
    client = r.client()
    body = client.get(f"/candybars/{row.id}/steward/tasks").json()
    assert body["candybarId"] == str(row.id)
    assert [t["id"] for t in body["tasks"]] == [str(t.id) for t in reversed(tasks)][:20]
    assert len(client.get(f"/candybars/{row.id}/steward/tasks?limit=3").json()["tasks"]) == 3
    assert client.get(f"/candybars/{row.id}/steward/tasks?limit=201").status_code == 422
    assert client.get(f"/candybars/{uuid4()}/steward/tasks").status_code == 404


def test_all_tasks_active_and_limit(st: tuple[Rig, Store]) -> None:
    r, store = st
    a, b = r.add("alpha"), r.add("beta")
    _seed(store, a.id, 3)
    [running] = _seed(store, b.id, 1, status="running", action="evaluate")
    client = r.client()
    assert [t["id"] for t in client.get("/steward/tasks?active=1").json()["tasks"]] == [str(running.id)]
    assert len(client.get("/steward/tasks").json()["tasks"]) == 4
    assert len(client.get("/steward/tasks?limit=2").json()["tasks"]) == 2
    assert client.get("/steward/tasks?limit=501").status_code == 422


def test_patch_status_moves_publish_their_fact_first(st: tuple[Rig, Store]) -> None:
    r, store = st
    row = r.add("alpha")
    [task] = _seed(store, row.id, 1, status="queued")
    client = r.client()
    resp = client.patch(f"/steward/tasks/{task.id}", json={"status": "running", "sessionId": "ses_abc"})
    assert resp.status_code == 200 and resp.json()["startedAt"] is not None
    [started] = r.sink.published
    assert started.type == "bloodbank.cron.steward_task.started"
    assert started.data.status == "running" and started.data.session_id == "ses_abc"
    resp = client.patch(f"/steward/tasks/{task.id}", json={"summary": "halfway"})
    assert resp.status_code == 200 and len(r.sink.published) == 1  # no status move, no fact
    resp = client.patch(f"/steward/tasks/{task.id}", json={"status": "succeeded", "outcome": "done"})
    assert resp.status_code == 200 and resp.json()["finishedAt"] is not None
    assert r.sink.published[-1].type == "bloodbank.cron.steward_task.completed"
    assert r.sink.published[-1].data.outcome == "done"
    resp = client.patch(f"/steward/tasks/{task.id}", json={"status": "failed"})
    assert resp.status_code == 409 and task.status == "succeeded"


def test_patch_refusals(st: tuple[Rig, Store]) -> None:
    r, store = st
    row = r.add("alpha")
    [task] = _seed(store, row.id, 1, status="queued")
    client = r.client()
    assert client.patch(f"/steward/tasks/{uuid4()}", json={"summary": "x"}).status_code == 404
    assert client.patch(f"/steward/tasks/{task.id}", json={}).status_code == 422
    assert client.patch(f"/steward/tasks/{task.id}", json={"status": None}).status_code == 422
    assert client.patch(f"/steward/tasks/{task.id}", json={"status": "succeeded"}).status_code == 409
    assert client.patch(f"/steward/tasks/{task.id}", json={"candybarId": str(uuid4())}).status_code == 422
    r.sink.fail = True
    resp = client.patch(f"/steward/tasks/{task.id}", json={"status": "failed", "error": "boom"})
    assert resp.status_code == 503 and task.status == "queued" and task.error is None


def test_cancel_fails_an_active_task_with_its_fact_first(st: tuple[Rig, Store]) -> None:
    r, store = st
    row = r.add("alpha")
    [task] = _seed(store, row.id, 1, status="running", action="fix")
    task.session_id = "ses_live"
    client = r.client()
    resp = client.post(f"/steward/tasks/{task.id}/cancel")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed" and body["error"] == "cancelled by operator"
    assert body["finishedAt"] is not None and body["sessionId"] == "ses_live"
    [fact] = r.sink.published
    assert fact.type == "bloodbank.cron.steward_task.failed" and fact.data.error == "cancelled by operator"
    # A finished task cannot be cancelled; an unknown one is a 404.
    assert client.post(f"/steward/tasks/{task.id}/cancel").status_code == 409
    assert client.post(f"/steward/tasks/{uuid4()}/cancel").status_code == 404


def test_cancel_bus_failure_changes_nothing(st: tuple[Rig, Store]) -> None:
    r, store = st
    row = r.add("alpha")
    [task] = _seed(store, row.id, 1, status="queued")
    r.sink.fail = True
    assert r.client().post(f"/steward/tasks/{task.id}/cancel").status_code == 503
    assert task.status == "queued" and task.error is None


def test_cancel_of_a_queued_follow_up_settles_its_evaluation(st: tuple[Rig, Store]) -> None:
    # Round 2, finding 5: a cancelled queued refactor never reads "queued" forever,
    # and its fact sits on the follow-up's chain (correlation = the parent).
    r, store = st
    row = r.add("alpha")
    [parent] = _seed(store, row.id, 1, status="succeeded", action="evaluate")
    [child] = _seed(store, row.id, 1, status="queued", action="refactor")
    child.parent_task_id = parent.id
    body = evaluation(outcome="Suboptimal", followUp="refactor", taskId=str(parent.id))
    body.update(followUpStatus="queued", followUpTaskId=str(child.id))
    store.profiles[row.id] = Profile(row.id, datetime.now(UTC), evaluation=body)
    resp = r.client().post(f"/steward/tasks/{child.id}/cancel")
    assert resp.status_code == 200, resp.text
    stored = store.profiles[row.id].evaluation
    assert stored["followUpStatus"] == "failed" and stored["followUpNote"] == "cancelled by operator"
    assert stored["followUpTaskId"] == str(child.id) and stored["outcome"] == "Suboptimal"
    [fact] = r.sink.published
    assert fact.correlationid == parent.id and fact.causationid == parent.id
    # The settled evaluation still validates as the contract shape.
    assert r.client().put(f"/candybars/{row.id}/profile", json={"evaluation": stored}).status_code == 200


def test_cancel_leaves_an_evaluation_that_points_elsewhere(st: tuple[Rig, Store]) -> None:
    r, store = st
    row = r.add("alpha")
    [task] = _seed(store, row.id, 1, status="queued", action="fix")
    body = evaluation(outcome="Drifted", followUp="fix", followUpStatus="queued", followUpTaskId=str(uuid4()))
    store.profiles[row.id] = Profile(row.id, datetime.now(UTC), evaluation=dict(body))
    assert r.client().post(f"/steward/tasks/{task.id}/cancel").status_code == 200
    assert store.profiles[row.id].evaluation == body
    [fact] = r.sink.published
    assert fact.correlationid == task.id and fact.causationid is None


def test_patch_records_spend(st: tuple[Rig, Store]) -> None:
    r, store = st
    row = r.add("alpha")
    [task] = _seed(store, row.id, 1, status="running")
    tokens = {"input": 10, "output": 2, "reasoning": 0, "cache": {"read": 1, "write": 0}}
    resp = r.client().patch(f"/steward/tasks/{task.id}", json={"costUsd": 0.5, "tokens": tokens})
    assert resp.status_code == 200 and resp.json()["costUsd"] == 0.5 and resp.json()["tokens"] == tokens
    assert r.client().patch(f"/steward/tasks/{task.id}", json={"costUsd": -1}).status_code == 422
