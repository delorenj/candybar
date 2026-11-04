"""CLI tests for project mode: `candybar register` (no path / project path) and
`candybar ls|list` inside a project, against an in-memory fake API.

The fake replaces candybar.cli.control._api, so nothing touches the network
(or the live registry). Projects are real tmp git repos the tests chdir into.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from candybar import project as pj
from candybar.cli import control
from candybar.cli.main import app
from candybar.migration.client import ApiError
from candybar.project import CandybarDef, Project, load_project

ALPHA = "01900000-0000-7000-8000-00000000000a"
BETA = "01900000-0000-7000-8000-00000000000b"
GAMMA = "01900000-0000-7000-8000-00000000000c"
OLD = "01900000-0000-7000-8000-0000000000d1"
OTHER = "01900000-0000-7000-8000-0000000000e1"
NEWID = "01900000-0000-7000-8000-0000000000f1"


class FakeApi:
    """The slice of CandybarApi register/ls use, over an in-memory registry."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.runners: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.fail: dict[str, ApiError] = {}  # method name -> error to raise
        self.fail_names: dict[str, ApiError] = {}  # candybar name -> error on register/update

    # reads
    def list_candybars(self) -> list[dict[str, Any]]:
        self.calls.append(("list", None))
        if "list" in self.fail:
            raise self.fail["list"]
        return [dict(r) for r in self.rows]

    def runner(self, candybar_id: str) -> dict[str, Any]:
        self.calls.append(("runner", candybar_id))
        if "runner" in self.fail:
            raise self.fail["runner"]
        return dict(self.runners.get(candybar_id, {"runner": None, "entrypoint": None}))

    # writes
    def register(self, definition: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("register", definition))
        if definition["name"] in self.fail_names:
            raise self.fail_names[definition["name"]]
        if "register" in self.fail:
            raise self.fail["register"]
        return {"id": NEWID, "name": definition["name"]}

    def update(self, candybar_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("update", (candybar_id, changes)))
        row = next(r for r in self.rows if r["id"] == candybar_id)
        if row["name"] in self.fail_names:
            raise self.fail_names[row["name"]]
        return row

    def set_archived(self, candybar_id: str, archived: bool) -> dict[str, Any]:
        self.calls.append(("archive", (candybar_id, archived)))
        if "archive" in self.fail:
            raise self.fail["archive"]
        return {}

    # helpers
    def writes(self) -> list[tuple[str, Any]]:
        return [c for c in self.calls if c[0] in {"register", "update", "archive"}]

    def of(self, kind: str) -> list[Any]:
        return [arg for k, arg in self.calls if k == kind]

    def add(self, d: CandybarDef, cid: str, **kw: Any) -> dict[str, Any]:
        """A registry row in sync with a local definition (override fields via kw)."""
        m = d.manifest
        row = {
            "id": cid, "name": m.name, "description": m.description, "tags": list(m.tags),
            "schedule": m.schedule, "timezone": m.timezone, "noise": m.noise,
            "enabled": True, "archived": False, **kw,
        }
        self.rows.append(row)
        self.runners[cid] = {"runner": m.runner, "entrypoint": m.entrypoint}
        return row

    def add_raw(self, name: str, cid: str, **kw: Any) -> dict[str, Any]:
        row = {
            "id": cid, "name": name, "description": "", "tags": [], "schedule": "* * * * *",
            "timezone": "UTC", "noise": False, "enabled": True, "archived": False, **kw,
        }
        self.rows.append(row)
        return row


def unreachable() -> ApiError:
    return ApiError("candybar API unreachable at http://x: refused")


def exec_table(name: str, schedule: str = "*/5 * * * *", extra: str = "") -> str:
    return (
        f'[candybars.{name}]\nschedule = "{schedule}"\n{extra}'
        f'[candybars.{name}.runner]\nkind = "exec"\nargv = ["bash", "-c", "true"]\n'
    )


def put(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    api = FakeApi()
    monkeypatch.setattr(control, "_api", lambda: api)
    return api


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git project named `myproj`, cwd inside it, with three candybars in two files."""
    root = tmp_path / "myproj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    root = root.resolve()
    put(root / ".candybar.toml", exec_table("alpha") + "\n" + exec_table("beta", "0 * * * *"))
    put(
        root / ".candybars" / "gamma.candybar.toml",
        '[candybar]\nschedule = "30 4 * * *"\n[candybar.runner]\nkind = "exec"\nargv = ["true"]\n',
    )
    monkeypatch.chdir(root)
    return root


@pytest.fixture()
def defs(repo: Path) -> dict[str, CandybarDef]:
    return load_project(Project(repo)).by_name()


def run(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def synced(fake: FakeApi, defs: dict[str, CandybarDef]) -> None:
    fake.add(defs["alpha"], ALPHA)
    fake.add(defs["beta"], BETA)
    fake.add(defs["gamma"], GAMMA)


# --- register: entry points ---------------------------------------------------------


def test_register_outside_a_project_needs_a_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeApi
) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)
    monkeypatch.setattr(pj, "_git_toplevel", lambda start: None)
    result = run("register")
    assert result.exit_code == 2
    assert "not inside a project; pass a path" in result.output
    assert fake.calls == []


def test_register_project_without_files_hints_at_the_formats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeApi
) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.chdir(root)
    result = run("register")
    assert result.exit_code == 2
    for hint in (".candybar.toml", ".candybars/", "candybar create"):
        assert hint in result.output
    assert fake.calls == []


def test_register_registers_every_project_candybar(repo: Path, fake: FakeApi) -> None:
    result = run("register")
    assert result.exit_code == 0, result.output
    assert result.output.count(f"(id {NEWID})") == 3
    for name in ("alpha", "beta", "gamma"):
        assert f"registered {name} (id {NEWID})" in result.output
    assert "project myproj: 3 registered" in result.output
    sent = {d["name"]: d for d in fake.of("register")}
    assert set(sent) == {"alpha", "beta", "gamma"}
    assert sent["alpha"]["tags"] == ["project:myproj"]
    assert sent["alpha"]["runner"]["workdir"] == str(repo)  # exec default: the project root
    assert sent["beta"]["schedule"] == "0 * * * *"
    assert fake.of("list") == [None]  # one registry read


def test_register_from_a_subdirectory_of_the_project(
    repo: Path, fake: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = repo / "deep" / "er"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    result = run("register")
    assert result.exit_code == 0 and "project myproj: 3 registered" in result.output


def test_register_unchanged_when_in_sync(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    synced(fake, defs)
    result = run("register")
    assert result.exit_code == 0, result.output
    for name in ("alpha", "beta", "gamma"):
        assert f"unchanged {name}" in result.output
    assert "project myproj: 3 unchanged" in result.output
    assert fake.writes() == []
    assert sorted(fake.of("runner")) == sorted([ALPHA, BETA, GAMMA])


def test_register_load_errors_register_nothing(repo: Path, fake: FakeApi) -> None:
    put(repo / ".candybars" / "bad.candybar.toml", '[candybar]\nschedule = "nope"\n')
    put(repo / ".candybars" / "worse.candybar.toml", "[candybar\n")
    result = run("register")
    assert result.exit_code == 2
    assert result.stderr.count("invalid manifest:") == 2
    assert "bad.candybar.toml" in result.stderr and "worse.candybar.toml" in result.stderr
    assert fake.calls == []


# --- register: existing rows ---------------------------------------------------------


def test_register_drift_is_reported_then_updated(
    repo: Path, defs: dict[str, CandybarDef], fake: FakeApi
) -> None:
    synced(fake, defs)
    fake.rows[0]["schedule"] = "1 1 * * *"
    fake.rows[0]["description"] = "stale"
    plain = run("register")
    assert plain.exit_code == 0
    assert "drifted alpha: description, schedule (rerun with --update)" in plain.output
    assert "project myproj: 2 unchanged, 1 drifted" in plain.output
    assert fake.writes() == []

    result = run("register", "--update")
    assert result.exit_code == 0, result.output
    assert "updated alpha: description, schedule" in result.output
    assert "project myproj: 1 updated, 2 unchanged" in result.output
    ((cid, changes),) = fake.of("update")
    assert cid == ALPHA
    assert changes == {"description": "", "schedule": "*/5 * * * *"}  # only drifted fields, never `enabled`


def test_register_update_patches_a_runner_change(
    repo: Path, defs: dict[str, CandybarDef], fake: FakeApi
) -> None:
    synced(fake, defs)
    fake.runners[GAMMA] = {"runner": {"kind": "exec", "argv": ["false"], "workdir": str(repo)}, "entrypoint": "false"}
    result = run("register", "--update")
    assert "updated gamma: runner" in result.output
    ((_, changes),) = fake.of("update")
    assert changes == {"runner": defs["gamma"].manifest.runner}


def test_register_never_updates_enabled(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    synced(fake, defs)
    fake.rows[0]["enabled"] = False  # paused by the operator
    result = run("register", "--update")
    assert "unchanged alpha" in result.output
    assert fake.writes() == []


def test_register_archived_rows_are_left_alone(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    synced(fake, defs)
    fake.rows[1]["archived"] = True
    fake.rows[1]["schedule"] = "9 9 * * *"
    result = run("register", "--update")
    assert result.exit_code == 0
    assert "archived beta; `candybar unarchive beta` first" in result.output
    assert fake.writes() == []
    assert "1 archived" in result.output


def test_register_unowned_row_is_reported_then_adopted(
    repo: Path, defs: dict[str, CandybarDef], fake: FakeApi
) -> None:
    synced(fake, defs)
    fake.rows[0]["tags"] = ["ops"]
    plain = run("register")
    assert plain.exit_code == 0
    assert "exists alpha (not owned by a project; --update adopts it)" in plain.output
    assert fake.writes() == []

    result = run("register", "--update")
    assert "adopted alpha" in result.output and "project myproj: 1 adopted, 2 unchanged" in result.output
    ((cid, changes),) = fake.of("update")
    assert cid == ALPHA and changes == {"tags": ["project:myproj"]}


def test_register_conflict_with_another_project_exits_1_but_continues(
    repo: Path, defs: dict[str, CandybarDef], fake: FakeApi
) -> None:
    fake.add(defs["alpha"], ALPHA, tags=["project:elsewhere"])
    result = run("register", "--update")
    assert result.exit_code == 1
    assert "conflict alpha: registered by project elsewhere" in result.stderr
    assert "registered beta" in result.output and "registered gamma" in result.output
    assert [d["name"] for d in fake.of("register")] == ["beta", "gamma"]
    assert fake.of("update") == []  # never touches the other project's row
    assert "1 conflicts" in result.output


def test_register_conflict_wins_over_archived(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    fake.add(defs["alpha"], ALPHA, tags=["project:elsewhere"], archived=True)
    assert run("register").exit_code == 1


# --- register: orphans, prune, filter, dry run -----------------------------------------


def orphan_setup(fake: FakeApi, defs: dict[str, CandybarDef]) -> None:
    synced(fake, defs)
    fake.add_raw("old-job", OLD, tags=["project:myproj"])
    fake.add_raw("gone-job", "01900000-0000-7000-8000-0000000000d2", tags=["project:myproj"], archived=True)
    fake.add_raw("theirs", OTHER, tags=["project:elsewhere"])


def test_register_reports_orphans_without_pruning(
    repo: Path, defs: dict[str, CandybarDef], fake: FakeApi
) -> None:
    orphan_setup(fake, defs)
    result = run("register")
    assert result.exit_code == 0
    assert "orphan old-job (registered, no local definition; --prune archives it)" in result.output
    assert "gone-job" not in result.output and "theirs" not in result.output
    assert "1 orphans" in result.output
    assert fake.writes() == []


def test_register_prune_archives_orphans(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    orphan_setup(fake, defs)
    result = run("register", "--prune")
    assert result.exit_code == 0, result.output
    assert "archived orphan old-job" in result.output and "1 pruned" in result.output
    assert fake.of("archive") == [(OLD, True)]


def test_register_prune_refused_with_name(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    result = run("register", "--prune", "--name", "alpha")
    assert result.exit_code == 2 and "--prune" in result.stderr
    assert fake.calls == []


def test_register_name_filter_limits_work_and_skips_orphans(
    repo: Path, defs: dict[str, CandybarDef], fake: FakeApi
) -> None:
    fake.add_raw("old-job", OLD, tags=["project:myproj"])
    result = run("register", "-n", "alpha", "--name", "gamma")
    assert result.exit_code == 0
    assert [d["name"] for d in fake.of("register")] == ["alpha", "gamma"]
    assert "orphan" not in result.output and "beta" not in result.output


def test_register_unknown_name_exits_2(repo: Path, fake: FakeApi) -> None:
    result = run("register", "--name", "nope")
    assert result.exit_code == 2 and "nope" in result.stderr
    assert fake.calls == []


def test_register_dry_run_writes_nothing(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    fake.add(defs["alpha"], ALPHA, schedule="1 1 * * *")
    fake.add(defs["beta"], BETA, tags=[])
    fake.add_raw("old-job", OLD, tags=["project:myproj"])
    result = run("register", "--dry-run", "--update", "--prune")
    assert result.exit_code == 0, result.output
    assert "would update alpha: schedule" in result.output
    assert "would adopt beta" in result.output
    assert "would register gamma" in result.output
    assert "would archive orphan old-job" in result.output
    assert "(dry run)" in result.output
    assert fake.writes() == []


def test_register_dry_run_still_reports_drift(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    synced(fake, defs)
    fake.rows[0]["schedule"] = "1 1 * * *"
    result = run("register", "--dry-run")
    assert "drifted alpha: schedule (rerun with --update)" in result.output
    assert fake.writes() == []


# --- register: API failures --------------------------------------------------------------


def test_register_api_unreachable_exits_1(repo: Path, fake: FakeApi) -> None:
    fake.fail["list"] = unreachable()
    result = run("register")
    assert result.exit_code == 1 and "unreachable" in result.output
    assert fake.writes() == []


def test_register_per_candybar_error_is_reported_and_loop_continues(repo: Path, fake: FakeApi) -> None:
    fake.fail_names["beta"] = ApiError("POST /candybars: 409 name is taken", status=409)
    result = run("register")
    assert result.exit_code == 1
    assert "error beta: POST /candybars: 409 name is taken" in result.stderr
    assert "registered alpha" in result.output and "registered gamma" in result.output
    assert "1 errors" in result.output


def test_register_update_error_continues(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    synced(fake, defs)
    fake.rows[0]["schedule"] = "1 1 * * *"
    fake.rows[1]["schedule"] = "2 2 * * *"
    fake.fail_names["alpha"] = ApiError("PATCH: 422 bad", status=422)
    result = run("register", "--update")
    assert result.exit_code == 1
    assert "error alpha" in result.stderr and "updated beta: schedule" in result.output


def test_register_stops_when_the_api_vanishes_mid_run(repo: Path, fake: FakeApi) -> None:
    fake.fail_names["alpha"] = unreachable()
    result = run("register")
    assert result.exit_code == 1
    assert next(c for c in fake.calls if c[0] == "register")[1]["name"] == "alpha"
    assert len(fake.of("register")) == 1  # did not hammer the remaining candybars


def test_register_runner_fetch_error_is_per_candybar(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    synced(fake, defs)
    fake.fail["runner"] = ApiError("GET runner: 500", status=500)
    result = run("register")
    assert result.exit_code == 1 and result.stderr.count("error ") == 3


# --- register: explicit PATH -------------------------------------------------------------


def test_register_project_directory_path(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeApi
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = run("register", str(repo))
    assert result.exit_code == 0, result.output
    assert "project myproj: 3 registered" in result.output


def test_register_single_project_file_is_a_partial_load(repo: Path, fake: FakeApi) -> None:
    fake.add_raw("old-job", OLD, tags=["project:myproj"])
    result = run("register", str(repo / ".candybars" / "gamma.candybar.toml"))
    assert result.exit_code == 0, result.output
    assert [d["name"] for d in fake.of("register")] == ["gamma"]
    assert fake.of("register")[0]["tags"] == ["project:myproj"]
    assert "orphan" not in result.output  # one file is not the whole project


def test_register_prune_with_a_file_path_is_refused(repo: Path, fake: FakeApi) -> None:
    result = run("register", "--prune", str(repo / ".candybar.toml"))
    assert result.exit_code == 2 and fake.calls == []


# --- register: non-project input keeps the old behaviour -----------------------------------


def legacy_dir(tmp_path: Path) -> Path:
    d = tmp_path / "legacy"
    put(d / "run.sh", "#!/bin/sh\nexit 0\n")
    put(d / "candybar.toml", '[candybar]\nname = "legacy"\nschedule = "* * * * *"\nentrypoint = "./run.sh"\n')
    return d


def test_register_legacy_directory_is_create_only(tmp_path: Path, fake: FakeApi) -> None:
    result = run("register", str(legacy_dir(tmp_path)))
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"registered legacy (id {NEWID})"
    (sent,) = fake.of("register")
    assert sent["tags"] == [] and sent["name"] == "legacy"
    assert "list" not in [k for k, _ in fake.calls]  # no registry read in create-only mode


def test_register_legacy_inside_a_project_gets_no_project_tag(repo: Path, fake: FakeApi) -> None:
    d = legacy_dir(repo / "tools")
    result = run("register", str(d))
    assert result.exit_code == 0
    assert fake.of("register")[0]["tags"] == []


def test_register_legacy_rejects_update_and_prune(tmp_path: Path, fake: FakeApi) -> None:
    d = legacy_dir(tmp_path)
    assert run("register", "--update", str(d)).exit_code == 2
    assert run("register", "--prune", str(d)).exit_code == 2
    assert fake.calls == []


def test_register_plain_file_with_several_tables_posts_each(tmp_path: Path, fake: FakeApi) -> None:
    f = put(tmp_path / "jobs.toml", exec_table("one") + "\n" + exec_table("two"))
    result = run("register", str(f))
    assert result.exit_code == 0, result.output
    assert [d["name"] for d in fake.of("register")] == ["one", "two"]
    assert "project" not in result.output


def test_register_legacy_echoes_the_problem_body(tmp_path: Path, fake: FakeApi) -> None:
    problem = {"status": 409, "title": "Candybar name already registered"}
    fake.fail["register"] = ApiError("POST /candybars: 409 taken", status=409, problem=problem)
    result = run("register", str(legacy_dir(tmp_path)))
    assert result.exit_code == 1
    assert json.loads(result.stderr) == problem


def test_register_legacy_api_unreachable(tmp_path: Path, fake: FakeApi) -> None:
    fake.fail["register"] = unreachable()
    result = run("register", str(legacy_dir(tmp_path)))
    assert result.exit_code == 1 and "unreachable" in result.output


def test_register_missing_path_exits_2(tmp_path: Path, fake: FakeApi) -> None:
    result = run("register", str(tmp_path / "nowhere"))
    assert result.exit_code == 2 and "invalid manifest" in result.output


def test_register_legacy_dry_run(tmp_path: Path, fake: FakeApi) -> None:
    result = run("register", "--dry-run", str(legacy_dir(tmp_path)))
    assert result.exit_code == 0 and "would register legacy" in result.output
    assert fake.calls == []


# --- ls: mode selection ----------------------------------------------------------------------


def test_ls_without_project_candybars_is_the_registry_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeApi
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.chdir(root)
    fake.add_raw("somebody", OLD, tags=["ops"])
    result = run("ls")
    assert result.exit_code == 0
    assert "FLAGS" in result.output and "1 shown of 1" in result.output


def test_ls_in_a_project_with_only_registered_rows_is_project_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeApi
) -> None:
    root = tmp_path / "myproj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.chdir(root)
    fake.add_raw("old-job", OLD, tags=["project:myproj"])
    fake.add_raw("elsewhere-job", OTHER, tags=["project:elsewhere"])
    result = run("ls")
    assert result.exit_code == 0
    assert "SYNC" in result.output and "old-job" in result.output and "orphan" in result.output
    assert "elsewhere-job" not in result.output


def test_ls_global_forces_the_registry_listing(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    synced(fake, defs)
    fake.add_raw("elsewhere-job", OTHER, tags=["project:elsewhere"])
    for flag in ("--global", "-g"):
        result = run("ls", flag)
        assert "FLAGS" in result.output and "elsewhere-job" in result.output
        assert "SYNC" not in result.output
    assert fake.of("runner") == []


def test_list_is_an_alias_of_ls(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    synced(fake, defs)
    assert run("list").output == run("ls").output
    assert "SYNC" in run("list").output


# --- ls: project table ------------------------------------------------------------------------


def row_of(output: str, name: str) -> list[str]:
    line = next(ln for ln in output.splitlines() if ln.startswith(name + " "))
    return line.split()


def test_ls_project_table_states_and_sync(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    fake.add(defs["alpha"], ALPHA)  # in sync
    fake.add(defs["beta"], BETA, schedule="9 9 * * *", enabled=False)  # paused + drifted
    # gamma: unregistered
    fake.add_raw("old-job", OLD, tags=["project:myproj"], schedule="5 5 * * *")
    result = run("ls")
    assert result.exit_code == 0, result.output
    header = result.output.splitlines()[0].split()
    assert header == ["NAME", "SCHEDULE", "TZ", "STATE", "SYNC", "SOURCE", "ID"]

    alpha = result.output.splitlines()
    alpha_line = next(ln for ln in alpha if ln.startswith("alpha"))
    assert "enabled" in alpha_line and " ok " in alpha_line and ALPHA in alpha_line
    assert ".candybar.toml [candybars.alpha]" in alpha_line

    beta_line = next(ln for ln in alpha if ln.startswith("beta"))
    assert "paused" in beta_line and "drift:schedule" in beta_line

    gamma_line = next(ln for ln in alpha if ln.startswith("gamma"))
    assert "unregistered" in gamma_line and " new " in gamma_line
    assert ".candybars/gamma.candybar.toml" in gamma_line and "[" not in gamma_line.split(".candybars")[1]
    assert gamma_line.rstrip().endswith("-")  # no id yet

    old_line = next(ln for ln in alpha if ln.startswith("old-job"))
    assert "orphan" in old_line and "enabled" in old_line and old_line.split()[-2] == "-"

    assert "4 candybars in project myproj (" in result.output
    assert str(repo) in result.output
    assert "2 unregistered/drifted; `candybar register` syncs them" in result.output
    assert "1 orphaned" in result.output and "--prune" in result.output


def test_ls_runner_is_fetched_only_for_owned_matched_rows(
    repo: Path, defs: dict[str, CandybarDef], fake: FakeApi
) -> None:
    fake.add(defs["alpha"], ALPHA)
    fake.add(defs["beta"], BETA, tags=[])  # unowned: sync without a runner read
    fake.add_raw("old-job", OLD, tags=["project:myproj"])  # orphan: no local def
    fake.add_raw("theirs", OTHER, tags=["project:elsewhere"])
    run("ls")
    assert fake.of("runner") == [ALPHA]


def test_ls_unowned_and_conflict_sync_states(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    fake.add(defs["alpha"], ALPHA, tags=[])
    fake.add(defs["beta"], BETA, tags=["project:elsewhere"])
    result = run("ls")
    assert "unowned" in next(ln for ln in result.output.splitlines() if ln.startswith("alpha"))
    assert "conflict:elsewhere" in next(ln for ln in result.output.splitlines() if ln.startswith("beta"))
    assert "not owned by a project; `candybar register --update` adopts" in result.output


def test_ls_archived_def_shows_but_archived_orphan_needs_flag(
    repo: Path, defs: dict[str, CandybarDef], fake: FakeApi
) -> None:
    synced(fake, defs)
    fake.rows[0]["archived"] = True
    fake.add_raw("old-job", OLD, tags=["project:myproj"], archived=True)
    default = run("ls")
    assert "archived" in next(ln for ln in default.output.splitlines() if ln.startswith("alpha"))
    assert "old-job" not in default.output
    shown = run("ls", "--archived")
    assert "old-job" in shown.output and "archived" in next(
        ln for ln in shown.output.splitlines() if ln.startswith("old-job")
    )


def test_ls_all_flag_is_accepted(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    synced(fake, defs)
    fake.rows[1]["enabled"] = False
    for flag in ("--all", "-a"):
        result = run("ls", flag)
        assert result.exit_code == 0 and "beta" in result.output


def test_ls_tag_and_hide_noise_filters(repo: Path, tmp_path: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    put(
        repo / ".candybars" / "loud.candybar.toml",
        '[candybar]\nschedule = "* * * * *"\nnoise = true\ntags = ["smoke"]\n'
        '[candybar.runner]\nkind = "exec"\nargv = ["true"]\n',
    )
    fake.add_raw("old-noise", OLD, tags=["project:myproj", "smoke"], noise=True)
    smoke = run("ls", "--tag", "smoke")
    assert "loud" in smoke.output and "old-noise" in smoke.output and "alpha" not in smoke.output
    assert "2 candybars in project" in smoke.output
    quiet = run("ls", "--hide-noise")
    assert "loud" not in quiet.output and "old-noise" not in quiet.output and "alpha" in quiet.output
    assert "alpha" not in run("ls", "-t", "smoke").output


def test_ls_footer_singular_and_clean(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    synced(fake, defs)
    one = run("ls", "--tag", "project:myproj")
    assert "3 candybars in project myproj" in one.output and "syncs them" not in one.output
    (repo / ".candybars" / "gamma.candybar.toml").unlink()
    (repo / ".candybar.toml").write_text(exec_table("alpha"))
    fake.rows = fake.rows[:1]
    assert "1 candybar in project myproj" in run("ls").output


# --- ls: errors ----------------------------------------------------------------------------------


def test_ls_local_load_errors_warn_but_still_list(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    synced(fake, defs)
    put(repo / ".candybars" / "bad.candybar.toml", '[candybar]\nschedule = "nope"\n')
    result = run("ls")
    assert result.exit_code == 0
    assert "warning:" in result.stderr and "bad.candybar.toml" in result.stderr
    assert "alpha" in result.stdout and "bad" not in result.stdout.replace("candybar.toml", "")


def test_ls_api_unreachable_lists_local_definitions(repo: Path, fake: FakeApi) -> None:
    fake.fail["list"] = unreachable()
    result = run("ls")
    assert result.exit_code == 1
    assert "unreachable" in result.stderr
    for name in ("alpha", "beta", "gamma"):
        line = next(ln for ln in result.stdout.splitlines() if ln.startswith(name))
        assert " ? " in line + " "
    assert "3 candybars in project myproj" in result.stdout


def test_ls_api_unreachable_in_a_non_project_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeApi
) -> None:
    root = tmp_path / "plain2"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.chdir(root)
    fake.fail["list"] = unreachable()
    result = run("ls")
    assert result.exit_code == 1 and "unreachable" in result.output


def test_ls_runner_fetch_failure_is_a_warning(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    synced(fake, defs)
    fake.fail["runner"] = ApiError("GET runner: 500", status=500)
    result = run("ls")
    assert result.exit_code == 0
    assert result.stderr.count("warning:") == 3
    assert " ? " in next(ln for ln in result.stdout.splitlines() if ln.startswith("alpha")) + " "


# --- ls --json ------------------------------------------------------------------------------------


def test_ls_json_shape(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    fake.add(defs["alpha"], ALPHA)
    fake.add(defs["beta"], BETA, schedule="9 9 * * *")
    fake.add_raw("old-job", OLD, tags=["project:myproj"])
    result = run("ls", "--json")
    assert result.exit_code == 0, result.output
    data = {e["name"]: e for e in json.loads(result.stdout)}
    assert set(data) == {"alpha", "beta", "gamma", "old-job"}
    assert set(data["alpha"]) == {"name", "state", "sync", "drift", "source", "table", "definition", "registered"}

    assert data["alpha"]["state"] == "enabled" and data["alpha"]["sync"] == "ok" and data["alpha"]["drift"] == []
    assert data["alpha"]["source"] == ".candybar.toml [candybars.alpha]"
    assert data["alpha"]["table"] == "[candybars.alpha]"
    assert data["alpha"]["definition"]["schedule"] == "*/5 * * * *"
    assert data["alpha"]["registered"]["id"] == ALPHA

    assert data["beta"]["sync"] == "drift:schedule" and data["beta"]["drift"] == ["schedule"]

    assert data["gamma"]["state"] == "unregistered" and data["gamma"]["sync"] == "new"
    assert data["gamma"]["registered"] is None
    assert data["gamma"]["source"] == ".candybars/gamma.candybar.toml" and data["gamma"]["table"] == "[candybar]"

    assert data["old-job"]["sync"] == "orphan" and data["old-job"]["definition"] is None
    assert data["old-job"]["source"] == "-" and data["old-job"]["table"] is None


def test_ls_json_when_api_unreachable_still_prints_then_exits_1(repo: Path, fake: FakeApi) -> None:
    fake.fail["list"] = unreachable()
    result = run("ls", "--json")
    assert result.exit_code == 1
    assert {e["state"] for e in json.loads(result.stdout)} == {"?"}


def test_ls_json_global_is_the_raw_registry(repo: Path, defs: dict[str, CandybarDef], fake: FakeApi) -> None:
    synced(fake, defs)
    out = json.loads(run("ls", "--json", "-g").stdout)
    assert [r["id"] for r in out] == [ALPHA, BETA, GAMMA]
