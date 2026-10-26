"""Story 7.3 (AD-18): runner definitions, their registration and their storage rules.

Pure validation (no DB, no bus, no host): the runner models, the manifest's
`entrypoint` xor `runner`, the canonical stored form, and the API's register /
PATCH / read behaviour with the in-memory registry rig of test_api_register.py.
The live-Postgres round trip is tests/test_registry_integration.py.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import candybar.api.app as app_module
from candybar.api.app import create_app
from candybar.events.parse import parse_envelope
from candybar.registry import CandybarManifest, ManifestError, load_manifest
from candybar.runners import (
    RunnerError,
    canonical_runner,
    parse_runner,
    runner_label,
)

EXEC = {"kind": "exec", "argv": ["/usr/bin/true"]}
SYSTEMD = {"kind": "systemd", "unit": "cache-prune.service"}


# ---------------------------------------------------------------------------
# The models.


def test_exec_runner_full_shape_round_trips() -> None:
    raw = {
        "kind": "exec",
        "argv": ["/usr/bin/restic", "backup", "--quiet"],
        "workdir": "/srv/vault",
        "env": {"RESTIC_REPOSITORY": "/mnt/backups"},
        "timeout_s": 6000,
        "on_failure": ["ntfy-publish", "--topic", "alerts"],
    }
    assert canonical_runner(raw) == raw


def test_canonical_form_drops_unset_optionals_and_is_idempotent() -> None:
    once = canonical_runner(EXEC)
    assert once == {"kind": "exec", "argv": ["/usr/bin/true"], "env": {}}
    assert canonical_runner(once) == once
    unit = canonical_runner(SYSTEMD)
    assert unit == SYSTEMD and canonical_runner(unit) == unit


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ({"kind": "exec", "argv": []}, "must not be empty"),
        ({"kind": "exec", "argv": [""]}, "must not be blank"),
        ({"kind": "exec", "argv": ["./run.sh"]}, "absolute path or a bare command"),
        ({"kind": "exec", "argv": ["bin/run"]}, "absolute path or a bare command"),
        ({"kind": "exec", "argv": ["ok", "a\x00b"]}, "NUL"),
        ({"kind": "exec", "argv": ["ok"], "workdir": "relative/dir"}, "absolute"),
        ({"kind": "exec", "argv": ["ok"], "timeout_s": 0}, "greater than or equal to 1"),
        ({"kind": "exec", "argv": ["ok"], "timeout_s": 10**9}, "less than or equal"),
        ({"kind": "exec", "argv": ["ok"], "env": {"CANDYBAR_ID": "x"}}, "reserved"),
        ({"kind": "exec", "argv": ["ok"], "env": {"1BAD": "x"}}, "not a valid variable name"),
        ({"kind": "exec", "argv": ["ok"], "env": {"A": "a\x00"}}, "NUL"),
        ({"kind": "exec", "argv": ["ok"], "on_failure": []}, "must not be empty"),
        ({"kind": "exec", "argv": ["ok"], "surprise": 1}, "surprise"),
        ({"kind": "systemd", "unit": "x.timer"}, "<name>.service"),
        ({"kind": "systemd", "unit": "-oops.service"}, "<name>.service"),
        ({"kind": "systemd", "unit": "a/b.service"}, "<name>.service"),
        ({"kind": "systemd", "unit": "a b.service"}, "<name>.service"),
        ({"kind": "systemd"}, "unit"),
        # Story 7.4 owns the hermes runner: until it lands the kind is unknown.
        ({"kind": "hermes", "profile": "p", "job_id": "j"}, "kind"),
        ({"argv": ["ok"]}, "kind"),
    ],
)
def test_invalid_runner_is_rejected(raw: dict[str, Any], fragment: str) -> None:
    with pytest.raises(RunnerError, match=fragment):
        parse_runner(raw)


def test_runner_must_be_an_object() -> None:
    with pytest.raises(RunnerError, match="must be an object"):
        parse_runner(["exec"])


def test_instance_and_escaped_unit_names_are_valid() -> None:
    assert parse_runner({"kind": "systemd", "unit": "activity-report@proj.service"})
    assert parse_runner({"kind": "systemd", "unit": r"a\x2db.service"})


def test_runner_label() -> None:
    assert runner_label(canonical_runner(EXEC)) == "/usr/bin/true"
    assert runner_label(canonical_runner(SYSTEMD)) == "systemd:cache-prune.service"


# ---------------------------------------------------------------------------
# The manifest.

BASE = {"name": "demo", "schedule": "*/5 * * * *"}


def test_manifest_requires_exactly_one_of_entrypoint_and_runner() -> None:
    with pytest.raises(ValueError, match="exactly one of"):
        CandybarManifest.model_validate(BASE)
    with pytest.raises(ValueError, match="exactly one of"):
        CandybarManifest.model_validate({**BASE, "entrypoint": "/bin/true", "runner": EXEC})
    plain = CandybarManifest.model_validate({**BASE, "entrypoint": "/bin/true"})
    assert plain.runner is None and plain.entrypoint_label == "/bin/true"
    via_runner = CandybarManifest.model_validate({**BASE, "runner": SYSTEMD})
    assert via_runner.entrypoint is None
    assert via_runner.runner == SYSTEMD
    assert via_runner.entrypoint_label == "systemd:cache-prune.service"


def test_manifest_holds_the_runner_in_canonical_form() -> None:
    manifest = CandybarManifest.model_validate({**BASE, "runner": EXEC})
    assert manifest.runner == {"kind": "exec", "argv": ["/usr/bin/true"], "env": {}}
    assert manifest.model_dump()["runner"] == manifest.runner


def test_manifest_reports_a_bad_runner_readably() -> None:
    with pytest.raises(ValueError, match="invalid runner: .*argv"):
        CandybarManifest.model_validate({**BASE, "runner": {"kind": "exec", "argv": []}})


def test_load_manifest_reads_a_runner_table_and_skips_the_file_check(tmp_path: Path) -> None:
    (tmp_path / "candybar.toml").write_text(
        """
[candybar]
name = "prune"
schedule = "0 3 * * *"
timezone = "America/New_York"

[candybar.runner]
kind = "systemd"
unit = "cache-prune.service"
"""
    )
    manifest = load_manifest(tmp_path)
    assert manifest.runner == SYSTEMD and manifest.entrypoint is None


def test_load_manifest_exec_runner_argv_is_not_file_checked(tmp_path: Path) -> None:
    # argv names a host path the CLI's machine need not share: not validated.
    (tmp_path / "candybar.toml").write_text(
        """
[candybar]
name = "job"
schedule = "* * * * *"

[candybar.runner]
kind = "exec"
argv = ["/nonexistent/host/binary", "--flag"]
workdir = "/nonexistent"
timeout_s = 30
"""
    )
    assert load_manifest(tmp_path).runner["argv"] == ["/nonexistent/host/binary", "--flag"]


def test_load_manifest_still_checks_a_plain_entrypoint(tmp_path: Path) -> None:
    (tmp_path / "candybar.toml").write_text(
        '[candybar]\nname = "x"\nschedule = "* * * * *"\nentrypoint = "/nope/missing"\n'
    )
    with pytest.raises(ManifestError, match="not an existing file"):
        load_manifest(tmp_path)


# ---------------------------------------------------------------------------
# The API (in-memory registry rig, as in test_api_register.py).

from test_api_register import FakeRow, FakeSink  # noqa: E402  (shared fakes)


class Rig:
    def __init__(self) -> None:
        self.rows: dict[UUID, FakeRow] = {}
        self.sink = FakeSink()
        self.entrypoint_checks: list[str] = []

    def client(self) -> TestClient:
        @asynccontextmanager
        async def session_factory() -> AsyncIterator[Any]:
            yield object()

        return TestClient(create_app(session_factory=session_factory, sink=self.sink))

    def events(self) -> list[Any]:
        return [parse_envelope(json.loads(raw)) for raw in self.sink.published]


@pytest.fixture()
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    rig = Rig()

    async def name_taken(session: Any, name: str) -> bool:
        return any(r.name == name for r in rig.rows.values())

    async def insert(session: Any, manifest: Any, *, candybar_id: Any = None) -> FakeRow:
        row = FakeRow(
            id=candybar_id or uuid4(),
            name=manifest.name,
            description=manifest.description,
            tags=list(manifest.tags),
            schedule=manifest.schedule,
            entrypoint=manifest.entrypoint_label,
            noise=manifest.noise,
            runner=dict(manifest.runner) if manifest.runner is not None else None,
        )
        rig.rows[row.id] = row
        return row

    async def get(session: Any, candybar_id: UUID) -> FakeRow | None:
        return rig.rows.get(candybar_id)

    async def update(session: Any, row: FakeRow, changes: dict[str, Any]) -> FakeRow:
        for key, value in changes.items():
            setattr(row, key, value)
        if changes.get("runner") is not None:
            row.entrypoint = runner_label(changes["runner"])
        elif "entrypoint" in changes and "runner" not in changes:
            row.runner = None
        return row

    real_problem = app_module.entrypoint_problem

    def spy(entrypoint: str) -> Any:
        rig.entrypoint_checks.append(entrypoint)
        return real_problem(entrypoint)

    monkeypatch.setattr(app_module, "name_taken", name_taken)
    monkeypatch.setattr(app_module, "insert_candybar", insert)
    monkeypatch.setattr(app_module, "get_candybar", get)
    monkeypatch.setattr(app_module, "lock_candybar", get)
    monkeypatch.setattr(app_module, "update_candybar", update)
    monkeypatch.setattr(app_module, "entrypoint_problem", spy)
    return rig


def _body(**extra: Any) -> dict[str, Any]:
    return {"name": "demo", "schedule": "*/5 * * * *", **extra}


def test_register_with_a_systemd_runner_skips_the_filesystem_check(rig: Rig) -> None:
    resp = rig.client().post("/candybars", json=_body(runner=SYSTEMD))
    assert resp.status_code == 201, resp.text
    row = rig.rows[UUID(resp.json()["id"])]
    assert row.runner == SYSTEMD
    assert row.entrypoint == "systemd:cache-prune.service"
    assert rig.entrypoint_checks == []  # the container cannot see host paths
    # The contract-verbatim body carries neither executor-facing field.
    assert "runner" not in resp.json() and "entrypoint" not in resp.json()


def test_register_with_an_exec_runner_does_not_require_the_argv_on_this_filesystem(
    rig: Rig,
) -> None:
    runner = {"kind": "exec", "argv": ["/home/someone/bin/job"], "workdir": "/home/someone"}
    resp = rig.client().post("/candybars", json=_body(runner=runner))
    assert resp.status_code == 201, resp.text
    assert rig.rows[UUID(resp.json()["id"])].runner == {**runner, "env": {}}
    assert rig.entrypoint_checks == []


def test_register_a_plain_entrypoint_is_still_checked(rig: Rig) -> None:
    resp = rig.client().post("/candybars", json=_body(entrypoint="/no/such/file"))
    assert resp.status_code == 422
    assert rig.entrypoint_checks == ["/no/such/file"]


@pytest.mark.parametrize(
    "extra",
    [
        {},  # neither
        {"entrypoint": "/bin/true", "runner": EXEC},  # both
        {"runner": {"kind": "exec", "argv": []}},
        {"runner": {"kind": "hermes", "profile": "p", "job_id": "j"}},
        {"runner": {"kind": "exec", "argv": ["x"], "env": {"CANDYBAR_ID": "1"}}},
    ],
)
def test_register_rejects_an_invalid_definition(rig: Rig, extra: dict[str, Any]) -> None:
    resp = rig.client().post("/candybars", json=_body(**extra))
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert rig.rows == {} and rig.sink.published == []


def _register(rig: Rig, **extra: Any) -> UUID:
    resp = rig.client().post("/candybars", json=_body(**extra))
    assert resp.status_code == 201, resp.text
    rig.sink.published.clear()
    return UUID(resp.json()["id"])


def test_runner_read_route_reports_exactly_one_of_runner_and_entrypoint(rig: Rig) -> None:
    runner_id = _register(rig, runner=SYSTEMD)
    plain_id = _register(rig, name="plain", entrypoint="/bin/true")
    client = rig.client()
    assert client.get(f"/candybars/{runner_id}/runner").json() == {
        "runner": SYSTEMD,
        "entrypoint": None,
    }
    assert client.get(f"/candybars/{plain_id}/runner").json() == {
        "runner": None,
        "entrypoint": "/bin/true",
    }
    assert client.get(f"/candybars/{uuid4()}/runner").status_code == 404


def test_patch_a_runner_replaces_the_entrypoint(rig: Rig) -> None:
    cid = _register(rig, entrypoint="/bin/true")
    resp = rig.client().patch(f"/candybars/{cid}", json={"runner": SYSTEMD})
    assert resp.status_code == 200, resp.text
    assert rig.rows[cid].runner == SYSTEMD
    assert rig.rows[cid].entrypoint == "systemd:cache-prune.service"
    (event,) = rig.events()
    assert event.data.changes == {"runner": SYSTEMD}


def test_patch_an_entrypoint_replaces_the_runner_and_the_fact_says_so(rig: Rig) -> None:
    cid = _register(rig, runner=SYSTEMD)
    resp = rig.client().patch(f"/candybars/{cid}", json={"entrypoint": "/bin/true"})
    assert resp.status_code == 200, resp.text
    assert rig.rows[cid].runner is None and rig.rows[cid].entrypoint == "/bin/true"
    (event,) = rig.events()
    assert event.data.changes == {"entrypoint": "/bin/true", "runner": None}


def test_patch_with_the_stored_runner_is_a_no_op(rig: Rig) -> None:
    cid = _register(rig, runner=EXEC)
    resp = rig.client().patch(f"/candybars/{cid}", json={"runner": EXEC})
    assert resp.status_code == 200
    assert rig.sink.published == []  # canonical form: no spurious change


def test_patch_cannot_set_both_or_a_bad_runner(rig: Rig) -> None:
    cid = _register(rig, runner=EXEC)
    client = rig.client()
    both = client.patch(f"/candybars/{cid}", json={"runner": SYSTEMD, "entrypoint": "/bin/true"})
    assert both.status_code == 422
    bad = client.patch(f"/candybars/{cid}", json={"runner": {"kind": "exec", "argv": []}})
    assert bad.status_code == 422
    assert rig.rows[cid].runner == canonical_runner(EXEC) and rig.sink.published == []


def test_patch_another_field_leaves_the_runner_alone(rig: Rig) -> None:
    cid = _register(rig, runner=SYSTEMD)
    resp = rig.client().patch(f"/candybars/{cid}", json={"description": "now described"})
    assert resp.status_code == 200
    assert rig.rows[cid].runner == SYSTEMD
