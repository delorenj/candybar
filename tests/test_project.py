"""Tests for candybar.project: project discovery, the two file formats, path rules,
rendering, safe writes and registry drift. Pure: no API, only tmp dirs."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from candybar import project as pj
from candybar.project import (
    Project,
    WriteError,
    drift,
    find_project,
    load_path,
    load_project,
    parse_text,
    plan_write,
    project_of_tags,
    render,
    write,
)
from candybar.registry.manifest import CandybarManifest, ManifestError


def git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root.resolve()


@pytest.fixture()
def no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend nothing is a git checkout (the tmp dir may sit inside one)."""
    monkeypatch.setattr(pj, "_git_toplevel", lambda start: None)


def put(path: Path, text: str, *, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)
    return path


def script(root: Path, rel: str = "scripts/job.sh") -> Path:
    return put(root / rel, "#!/bin/sh\nexit 0\n", mode=0o755)


def multi(name: str, schedule: str = "*/5 * * * *", argv: str = '["bash", "-c", "true"]') -> str:
    return f'[candybars.{name}]\nschedule = "{schedule}"\n[candybars.{name}.runner]\nkind = "exec"\nargv = {argv}\n'


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return git_repo(tmp_path / "myproj")


# --- discovery -----------------------------------------------------------------


def test_find_project_uses_git_toplevel_from_a_subdirectory(repo: Path) -> None:
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    found = find_project(sub)
    assert found == Project(repo)
    assert found.name == "myproj" and found.tag == "project:myproj"


def test_find_project_default_start_is_cwd(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    assert find_project() == Project(repo)


def test_find_project_accepts_a_file_as_start(repo: Path) -> None:
    f = put(repo / "x.txt", "hi")
    assert find_project(f) == Project(repo)


def test_find_project_outside_git_uses_marker_dir(tmp_path: Path, no_git: None) -> None:
    root = tmp_path / "loose"
    (root / ".candybars").mkdir(parents=True)
    deep = root / "src" / "pkg"
    deep.mkdir(parents=True)
    assert find_project(deep) == Project(root.resolve())


def test_find_project_outside_git_uses_marker_file(tmp_path: Path, no_git: None) -> None:
    root = tmp_path / "loose2"
    put(root / ".candybar.toml", multi("x"))
    (root / "sub").mkdir()
    assert find_project(root / "sub") == Project(root.resolve())


def test_find_project_none_without_git_or_markers(tmp_path: Path, no_git: None) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    assert find_project(bare) is None


def test_git_toplevel_beats_marker_in_a_parent(tmp_path: Path) -> None:
    (tmp_path / ".candybars").mkdir()
    inner = git_repo(tmp_path / "inner")
    assert find_project(inner) == Project(inner)


def test_project_files_order_and_ownership(repo: Path) -> None:
    project = Project(repo)
    assert project.files() == []
    put(repo / ".candybars" / "b.candybar.toml", "")
    put(repo / ".candybars" / "a.candybar.toml", "")
    put(repo / ".candybars" / "notes.toml", "")
    put(repo / ".candybar.toml", "")
    assert project.files() == [
        repo / ".candybar.toml",
        repo / ".candybars" / "a.candybar.toml",
        repo / ".candybars" / "b.candybar.toml",
    ]
    assert project.owns(repo / ".candybar.toml")
    assert project.owns(repo / ".candybars" / "new.candybar.toml")  # by location, not existence
    assert not project.owns(repo / ".candybars" / "notes.toml")
    assert not project.owns(repo / "candybar.toml")
    assert not project.owns(repo / ".candybars" / "sub" / "x.candybar.toml")
    assert project.default_file_for("nightly") == repo / ".candybars" / "nightly.candybar.toml"


def test_project_of_tags() -> None:
    assert project_of_tags(["ops", "project:alpha", "project:beta"]) == "alpha"
    assert project_of_tags(["ops"]) is None
    assert project_of_tags(["project:"]) is None
    assert project_of_tags([]) is None


# --- loading both formats -------------------------------------------------------


def test_both_formats_in_one_project_and_in_one_file(repo: Path) -> None:
    script(repo)
    put(
        repo / ".candybar.toml",
        multi("alpha")
        + "\n"
        + multi("beta", "0 * * * *")
        + '\n[candybar]\nname = "gamma"\nschedule = "1 1 * * *"\nentrypoint = "scripts/job.sh"\n',
    )
    put(
        repo / ".candybars" / "delta.candybar.toml",
        '[candybar]\nschedule = "2 2 * * *"\nentrypoint = "scripts/job.sh"\n',
    )
    result = load_project(Project(repo))
    assert result.ok, result.errors
    assert sorted(d.name for d in result.defs) == ["alpha", "beta", "delta", "gamma"]
    by = result.by_name()
    assert by["alpha"].table == "[candybars.alpha]"
    assert by["gamma"].table == "[candybar]"
    assert by["delta"].source == repo / ".candybars" / "delta.candybar.toml"


def test_single_table_name_defaults_to_the_stem_in_candybars_dir(repo: Path) -> None:
    script(repo)
    put(
        repo / ".candybars" / "nightly-backup.candybar.toml",
        '[candybar]\nschedule = "0 3 * * *"\nentrypoint = "scripts/job.sh"\n',
    )
    result = load_project(Project(repo))
    assert result.ok and [d.name for d in result.defs] == ["nightly-backup"]


def test_single_table_in_project_file_needs_a_name(repo: Path) -> None:
    script(repo)
    put(repo / ".candybar.toml", '[candybar]\nschedule = "0 3 * * *"\nentrypoint = "scripts/job.sh"\n')
    result = load_project(Project(repo))
    assert not result.ok and result.defs == []
    assert "name" in result.errors[0]


def test_stem_default_does_not_apply_outside_candybars_files(tmp_path: Path) -> None:
    result = parse_text(
        '[candybar]\nschedule = "* * * * *"\n[candybar.runner]\nkind = "exec"\nargv = ["true"]\n',
        tmp_path / "other.toml",
        project=None,
    )
    assert result.defs == [] and "name" in result.errors[0]


def test_key_name_mismatch_is_an_error(repo: Path) -> None:
    put(
        repo / ".candybar.toml",
        '[candybars.alpha]\nname = "beta"\nschedule = "* * * * *"\n'
        '[candybars.alpha.runner]\nkind = "exec"\nargv = ["true"]\n',
    )
    result = load_project(Project(repo))
    assert result.defs == []
    assert "sets name = 'beta'" in result.errors[0] and "[candybars.alpha]" in result.errors[0]


def test_matching_explicit_name_is_fine(repo: Path) -> None:
    put(
        repo / ".candybar.toml",
        '[candybars.alpha]\nname = "alpha"\nschedule = "* * * * *"\n'
        '[candybars.alpha.runner]\nkind = "exec"\nargv = ["true"]\n',
    )
    assert load_project(Project(repo)).ok


def test_duplicate_names_across_files(repo: Path) -> None:
    put(repo / ".candybar.toml", multi("alpha"))
    put(repo / ".candybars" / "alpha.candybar.toml", multi("alpha", "0 * * * *"))
    result = load_project(Project(repo))
    assert [d.name for d in result.defs] == ["alpha"]  # the first one survives
    assert len(result.errors) == 1
    assert "duplicate candybar name 'alpha'" in result.errors[0]
    assert ".candybar.toml" in result.errors[0] and "alpha.candybar.toml" in result.errors[0]


def test_duplicate_between_single_and_multi_in_one_file(repo: Path) -> None:
    put(
        repo / ".candybars" / "alpha.candybar.toml",
        multi("alpha") + '\n[candybar]\nschedule = "1 * * * *"\n[candybar.runner]\nkind = "exec"\nargv = ["true"]\n',
    )
    result = load_project(Project(repo))
    assert any("duplicate" in e for e in result.errors)


def test_unknown_top_level_key_is_an_error(repo: Path) -> None:
    put(repo / ".candybar.toml", multi("alpha") + '\n[tasks.x]\nrun = "y"\n')
    result = load_project(Project(repo))
    assert [d.name for d in result.defs] == ["alpha"]  # valid siblings still load
    assert any("unknown top-level key" in e and "'tasks'" in e for e in result.errors)


def test_empty_and_invalid_files(repo: Path) -> None:
    put(repo / ".candybars" / "empty.candybar.toml", "")
    put(repo / ".candybars" / "broken.candybar.toml", "[candybar\n")
    result = load_project(Project(repo))
    assert any("no candybars" in e for e in result.errors)
    assert any("invalid TOML" in e for e in result.errors)


def test_non_table_values_are_errors(repo: Path) -> None:
    put(repo / ".candybars" / "a.candybar.toml", 'candybar = "x"\n')
    put(repo / ".candybars" / "b.candybar.toml", 'candybars = "x"\n')
    put(repo / ".candybars" / "c.candybar.toml", "[candybars]\nz = 1\n")
    errors = "\n".join(load_project(Project(repo)).errors)
    assert "`candybar` must be a table" in errors
    assert "`candybars` must be a table of" in errors
    assert "[candybars.z] must be a table" in errors


def test_one_bad_candybar_does_not_hide_the_good_one(repo: Path) -> None:
    put(repo / ".candybar.toml", multi("good") + "\n" + multi("bad", "not a cron"))
    result = load_project(Project(repo))
    assert [d.name for d in result.defs] == ["good"]
    assert len(result.errors) == 1 and "[candybars.bad]" in result.errors[0] and "schedule" in result.errors[0]


# --- project path rules -----------------------------------------------------------


def test_argv0_with_slash_resolves_against_root_bare_command_does_not(repo: Path) -> None:
    put(
        repo / ".candybar.toml",
        multi("rel", argv='["./scripts/run.sh", "--x"]') + "\n" + multi("bare", argv='["bash", "scripts/x.sh"]'),
    )
    by = load_project(Project(repo)).by_name()
    assert by["rel"].manifest.runner["argv"] == [str(repo / "scripts" / "run.sh"), "--x"]
    assert by["bare"].manifest.runner["argv"] == ["bash", "scripts/x.sh"]


def test_absolute_argv0_is_untouched(repo: Path) -> None:
    put(repo / ".candybar.toml", multi("abs", argv='["/usr/bin/true"]'))
    assert load_project(Project(repo)).defs[0].manifest.runner["argv"] == ["/usr/bin/true"]


def test_workdir_defaults_to_root_and_relative_resolves_against_it(repo: Path) -> None:
    put(
        repo / ".candybar.toml",
        multi("dflt")
        + '\n[candybars.rel]\nschedule = "* * * * *"\n[candybars.rel.runner]\nkind = "exec"\n'
        'argv = ["true"]\nworkdir = "sub/dir"\n'
        + '\n[candybars.absolute]\nschedule = "* * * * *"\n[candybars.absolute.runner]\nkind = "exec"\n'
        'argv = ["true"]\nworkdir = "/srv"\n',
    )
    by = load_project(Project(repo)).by_name()
    assert by["dflt"].manifest.runner["workdir"] == str(repo)
    assert by["rel"].manifest.runner["workdir"] == str(repo / "sub" / "dir")
    assert by["absolute"].manifest.runner["workdir"] == "/srv"


def test_non_exec_runners_get_no_workdir(repo: Path) -> None:
    put(
        repo / ".candybar.toml",
        '[candybars.unit]\nschedule = "* * * * *"\n[candybars.unit.runner]\nkind = "systemd"\nunit = "x.service"\n',
    )
    assert "workdir" not in load_project(Project(repo)).defs[0].manifest.runner


def test_on_failure_with_slash_resolves_against_root(repo: Path) -> None:
    put(
        repo / ".candybar.toml",
        '[candybars.alpha]\nschedule = "* * * * *"\n[candybars.alpha.runner]\nkind = "exec"\n'
        'argv = ["true"]\non_failure = ["./scripts/alert.sh", "boom"]\n',
    )
    runner = load_project(Project(repo)).defs[0].manifest.runner
    assert runner["on_failure"] == [str(repo / "scripts" / "alert.sh"), "boom"]


def test_entrypoint_resolves_against_root_and_must_exist(repo: Path) -> None:
    script(repo)
    put(
        repo / ".candybars" / "ok.candybar.toml",
        '[candybar]\nschedule = "* * * * *"\nentrypoint = "scripts/job.sh"\n',
    )
    put(
        repo / ".candybars" / "gone.candybar.toml",
        '[candybar]\nschedule = "* * * * *"\nentrypoint = "scripts/nope.sh"\n',
    )
    result = load_project(Project(repo))
    # The file lives in .candybars/, but the path is from the project root.
    assert result.by_name()["ok"].manifest.entrypoint == str(repo / "scripts" / "job.sh")
    assert len(result.errors) == 1
    assert "entrypoint is not an existing file" in result.errors[0] and "scripts/nope.sh" in result.errors[0]


def test_project_tag_added_once_and_keeps_user_tags(repo: Path) -> None:
    put(
        repo / ".candybar.toml",
        multi("plain")
        + '\n[candybars.tagged]\ntags = ["ops", "project:myproj"]\nschedule = "* * * * *"\n'
        '[candybars.tagged.runner]\nkind = "exec"\nargv = ["true"]\n'
        + '\n[candybars.mine]\ntags = ["ops"]\nschedule = "* * * * *"\n'
        '[candybars.mine.runner]\nkind = "exec"\nargv = ["true"]\n',
    )
    by = load_project(Project(repo)).by_name()
    assert by["plain"].manifest.tags == ["project:myproj"]
    assert by["tagged"].manifest.tags == ["ops", "project:myproj"]
    assert by["mine"].manifest.tags == ["ops", "project:myproj"]


# --- load_path -----------------------------------------------------------------


def test_load_path_legacy_directory_keeps_ad8_semantics(tmp_path: Path) -> None:
    d = tmp_path / "legacy"
    script(d, "run.sh")
    put(d / "candybar.toml", '[candybar]\nname = "legacy"\nschedule = "* * * * *"\nentrypoint = "./run.sh"\n')
    result = load_path(d)
    assert result.ok
    (only,) = result.defs
    assert only.manifest.tags == []  # no project tag
    assert only.manifest.entrypoint == str((d / "run.sh").resolve())  # against the file's dir


def test_load_path_legacy_file_inside_a_repo_gets_no_project_rules(repo: Path) -> None:
    script(repo, "tool/run.sh")
    legacy = put(repo / "tool" / "candybar.toml", '[candybar]\nname = "tool"\nschedule = "* * * * *"\nentrypoint = "run.sh"\n')
    (only,) = load_path(legacy).defs
    assert only.manifest.tags == []
    assert only.manifest.entrypoint == str(repo / "tool" / "run.sh")


def test_load_path_project_file_gets_project_rules(repo: Path) -> None:
    script(repo)
    f = put(repo / ".candybars" / "job.candybar.toml", '[candybar]\nschedule = "* * * * *"\nentrypoint = "scripts/job.sh"\n')
    (only,) = load_path(f).defs
    assert only.name == "job"
    assert only.manifest.tags == ["project:myproj"]
    assert only.manifest.entrypoint == str(repo / "scripts" / "job.sh")


def test_load_path_project_directory(repo: Path) -> None:
    put(repo / ".candybar.toml", multi("a") + "\n" + multi("b"))
    assert sorted(d.name for d in load_path(repo).defs) == ["a", "b"]


def test_load_path_plain_file_with_several_tables_has_no_project(tmp_path: Path) -> None:
    f = put(tmp_path / "jobs.toml", multi("a") + "\n" + multi("b"))
    result = load_path(f)
    assert sorted(d.name for d in result.defs) == ["a", "b"]
    assert all(d.manifest.tags == [] for d in result.defs)


def test_load_path_raises_when_nothing_is_there(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(ManifestError, match="no candybar.toml"):
        load_path(tmp_path / "empty")
    with pytest.raises(ManifestError, match="no candybar file"):
        load_path(tmp_path / "missing.toml")


def test_load_path_dedupes_within_a_file(tmp_path: Path) -> None:
    f = put(
        tmp_path / "x.candybar.toml",
        multi("a") + '\n[candybar]\nname = "a"\nschedule = "1 * * * *"\n[candybar.runner]\nkind = "exec"\nargv = ["true"]\n',
    )
    result = load_path(f)
    assert len(result.defs) == 1 and any("duplicate" in e for e in result.errors)


# --- render ----------------------------------------------------------------------


SPEC = {
    "name": "nightly",
    "description": "d",
    "tags": ["a"],
    "schedule": "0 3 * * *",
    "timezone": "America/New_York",
    "noise": True,
    "enabled": False,
    "runner": {"kind": "exec", "argv": ["bash", "-c", "true"], "workdir": "/srv", "timeout_s": 30},
}


def _spec_manifest(spec: dict) -> CandybarManifest:
    return CandybarManifest.model_validate(spec)


def test_render_single_roundtrips_through_tomllib() -> None:
    raw = tomllib.loads(render(SPEC))
    assert list(raw) == ["candybar"]
    assert _spec_manifest(raw["candybar"]) == _spec_manifest(SPEC)


def test_render_multi_roundtrips_and_drops_the_name_line() -> None:
    text = render(SPEC, style="multi", comment="line one\nline two")
    assert text.startswith("# line one\n# line two\n[candybars.nightly]\n")
    raw = tomllib.loads(text)
    body = raw["candybars"]["nightly"]
    assert "name" not in body
    assert _spec_manifest({"name": "nightly", **body}) == _spec_manifest(SPEC)


def test_render_leaves_out_defaults_that_say_nothing() -> None:
    text = render(
        {"name": "n", "description": "", "tags": [], "schedule": "* * * * *", "timezone": "UTC",
         "noise": False, "enabled": True, "entrypoint": "/bin/true", "runner": None}
    )
    assert text == '[candybar]\nname = "n"\nschedule = "* * * * *"\nentrypoint = "/bin/true"\n'


def test_render_field_order() -> None:
    text = render(SPEC)
    keys = [line.split(" =")[0] for line in text.splitlines() if " = " in line]
    assert keys[:7] == ["name", "description", "tags", "schedule", "timezone", "noise", "enabled"]
    assert text.index("[candybar.runner]") > text.index("enabled")


def test_render_special_strings_roundtrip() -> None:
    nasty = 'quote " back\\slash \t tab \n newline unicode é 日本 \U0001f36c del\x7f ctl\x01 end'
    spec = {
        "name": "nasty",
        "description": nasty,
        "schedule": "* * * * *",
        "runner": {
            "kind": "exec",
            "argv": ["echo", nasty],
            "env": {"A": nasty, "B": "x"},
            "workdir": "/tmp",
        },
    }
    for style in ("single", "multi"):
        raw = tomllib.loads(render(spec, style=style))
        body = raw["candybar"] if style == "single" else raw["candybars"]["nasty"]
        assert body["description"] == nasty
        assert body["runner"]["argv"] == ["echo", nasty]
        assert body["runner"]["env"] == {"A": nasty, "B": "x"}


def test_render_quoted_keys_and_inline_tables() -> None:
    spec = {
        "name": "keys",
        "schedule": "* * * * *",
        "runner": {
            "kind": "exec", "argv": ["true"], "workdir": "/tmp",
            "env": {"A.B": "1", "weird key": "2", "ok_key-1": "3"},
        },
    }
    text = render(spec)
    assert '"A.B" = "1"' in text and '"weird key" = "2"' in text and "ok_key-1 = " in text.replace('"', "")
    assert tomllib.loads(text)["candybar"]["runner"]["env"] == spec["runner"]["env"]
    # a name that is not a bare key is quoted in the multi header (and fails the slug rule on load)
    assert render({"name": "has space", "schedule": "* * * * *"}, style="multi").startswith('[candybars."has space"]')


def test_toml_value_scalars_and_errors() -> None:
    assert pj.toml_value(True) == "true" and pj.toml_value(7) == "7" and pj.toml_value(1.5) == "1.5"
    assert pj.toml_value([]) == "[]" and pj.toml_value({}) == "{}"
    assert pj.toml_value(("a", 1)) == '["a", 1]'
    with pytest.raises(ValueError):
        pj.toml_value(None)
    with pytest.raises(TypeError):
        pj.toml_value(object())


def test_render_rejects_missing_name_and_unknown_style() -> None:
    with pytest.raises(ValueError, match="needs a name"):
        render({"schedule": "* * * * *"})
    with pytest.raises(ValueError, match="unknown style"):
        render({"name": "n"}, style="weird")


# --- plan_write / write -------------------------------------------------------------


def spec(name: str = "fresh", **extra: object) -> dict:
    return {
        "name": name,
        "schedule": "*/10 * * * *",
        "runner": {"kind": "exec", "argv": ["bash", "-c", "true"]},
        **extra,
    }


def test_plan_write_new_file_in_candybars_dir(repo: Path) -> None:
    project = Project(repo)
    dest = project.default_file_for("fresh")
    planned = plan_write(spec(), dest, project=project)
    assert planned.path == dest and not planned.appended
    assert planned.text.startswith("[candybar]\n")
    assert not dest.exists()  # planning never touches disk
    assert planned.result.ok and planned.result.defs[0].manifest.tags == ["project:myproj"]
    assert write(planned) == dest and dest.read_text() == planned.text
    assert load_project(project).by_name()["fresh"].source == dest


def test_plan_write_appends_to_project_file(repo: Path) -> None:
    project = Project(repo)
    existing = multi("old")
    put(project.project_file, existing)
    planned = plan_write(spec(), project.project_file, project=project, comment="why")
    assert planned.appended
    assert planned.text.startswith(existing + "\n# why\n[candybars.fresh]\n")
    write(planned)
    assert sorted(load_project(project).by_name()) == ["fresh", "old"]


def test_plan_write_append_separator_without_trailing_newline(repo: Path) -> None:
    project = Project(repo)
    put(project.project_file, multi("old").rstrip("\n"))
    planned = plan_write(spec(), project.project_file, project=project)
    write(planned)
    assert load_project(project).ok


def test_plan_write_creates_project_file_when_absent(repo: Path) -> None:
    project = Project(repo)
    planned = plan_write(spec(), project.project_file, project=project)
    assert not planned.appended and planned.text.startswith("[candybars.fresh]\n")
    write(planned)
    assert [d.name for d in load_project(project).defs] == ["fresh"]


def test_plan_write_refuses_an_existing_file_unless_overwrite(repo: Path) -> None:
    project = Project(repo)
    dest = put(project.default_file_for("fresh"), "# mine\n")
    with pytest.raises(WriteError, match="already exists"):
        plan_write(spec(), dest, project=project)
    planned = plan_write(spec(), dest, project=project, overwrite=True)
    write(planned)
    assert dest.read_text().startswith("[candybar]")


def test_plan_write_name_clash_across_project_files(repo: Path) -> None:
    project = Project(repo)
    put(project.project_file, multi("fresh"))
    with pytest.raises(WriteError, match="already defined"):
        plan_write(spec("fresh"), project.default_file_for("fresh"), project=project)
    # and the other way around
    other = Project(git_repo(repo.parent / "other"))
    put(other.default_file_for("fresh"), render(spec("fresh")))
    with pytest.raises(WriteError, match="already defined|duplicate"):
        plan_write(spec("fresh"), other.project_file, project=other)


def test_plan_write_name_clash_inside_the_project_file(repo: Path) -> None:
    project = Project(repo)
    put(project.project_file, multi("fresh"))
    with pytest.raises(WriteError):
        plan_write(spec("fresh"), project.project_file, project=project)


def test_plan_write_validates_like_register(repo: Path) -> None:
    project = Project(repo)
    with pytest.raises(WriteError, match="schedule"):
        plan_write(spec(schedule="nope"), project.default_file_for("x"), project=project)
    with pytest.raises(WriteError, match="entrypoint is not an existing file"):
        plan_write(
            {"name": "x", "schedule": "* * * * *", "entrypoint": "scripts/missing.sh"},
            project.default_file_for("x"),
            project=project,
        )


def test_plan_write_without_a_project_uses_ad8_rules(tmp_path: Path) -> None:
    script(tmp_path, "run.sh")
    dest = tmp_path / "out" / "candybar.toml"
    planned = plan_write(
        {"name": "solo", "schedule": "* * * * *", "entrypoint": str(tmp_path / "run.sh")}, dest, project=None
    )
    assert planned.result.defs[0].manifest.tags == []


# --- drift ---------------------------------------------------------------------------


def manifest(**kw: object) -> CandybarManifest:
    base = {
        "name": "j", "schedule": "* * * * *", "tags": ["a", "b"],
        "runner": {"kind": "exec", "argv": ["true"]},
    }
    return CandybarManifest.model_validate({**base, **kw})


def registered(m: CandybarManifest, **kw: object) -> dict:
    row = {
        "name": m.name, "description": m.description, "tags": list(m.tags), "schedule": m.schedule,
        "timezone": m.timezone, "noise": m.noise, "runner": m.runner, "entrypoint": m.entrypoint_label,
    }
    if m.entrypoint is None:
        row["entrypoint"] = None
    return {**row, **kw}


def test_no_drift_when_identical() -> None:
    m = manifest()
    assert drift(m, registered(m)) == {}


def test_drift_reports_changed_fields_with_wanted_values() -> None:
    m = manifest(description="new", schedule="0 * * * *", timezone="America/Chicago", noise=True)
    old = manifest()
    assert drift(m, registered(old)) == {
        "description": "new", "schedule": "0 * * * *", "timezone": "America/Chicago", "noise": True,
    }


def test_drift_tags_ignore_order() -> None:
    m = manifest(tags=["b", "a"])
    assert drift(m, registered(m, tags=["a", "b"])) == {}
    assert drift(m, registered(m, tags=["a"])) == {"tags": ["b", "a"]}
    assert drift(m, registered(m, tags=None)) == {"tags": ["b", "a"]}


def test_drift_runner_change() -> None:
    m = manifest(runner={"kind": "exec", "argv": ["false"]})
    old = manifest()
    assert drift(m, registered(old)) == {"runner": m.runner}


def test_drift_skips_fields_the_row_does_not_carry() -> None:
    m = manifest(schedule="0 * * * *")
    assert drift(m, {"name": "j"}) == {}
    assert drift(m, {"schedule": "* * * * *"}) == {"schedule": "0 * * * *"}


def test_drift_entrypoint_to_runner_switch_sends_only_the_runner() -> None:
    m = manifest()  # wants a runner
    row = registered(m, runner=None, entrypoint="/old/run.sh")
    assert drift(m, row) == {"runner": m.runner}


def test_drift_runner_to_entrypoint_switch_sends_only_the_entrypoint(tmp_path: Path) -> None:
    f = script(tmp_path)
    m = CandybarManifest.model_validate({"name": "j", "schedule": "* * * * *", "entrypoint": str(f)})
    row = {"schedule": "* * * * *", "runner": {"kind": "exec", "argv": ["true"]}, "entrypoint": "true"}
    assert drift(m, row) == {"entrypoint": str(f)}


def test_drift_never_includes_enabled() -> None:
    m = manifest(enabled=False)
    assert drift(m, registered(m, enabled=True)) == {}
    assert "enabled" not in pj.SYNCED_FIELDS


# --- steward context sees project files ------------------------------------------------


def test_steward_manifest_names_cover_both_formats(repo: Path) -> None:
    from candybar.steward.context import _manifest_names

    put(repo / ".candybar.toml", multi("alpha") + "\n" + multi("beta"))
    single = put(repo / ".candybars" / "gamma.candybar.toml", '[candybar]\nschedule = "* * * * *"\n')
    legacy = put(repo / "candybar.toml", '[candybar]\nname = "delta"\nschedule = "* * * * *"\n')
    assert _manifest_names(repo / ".candybar.toml") == {"alpha", "beta"}
    assert _manifest_names(single) == {"gamma"}
    assert _manifest_names(legacy) == {"delta"}
    assert _manifest_names(repo / "missing.toml") == set()


# --- review round 1 regressions ----------------------------------------------------


def test_symlinked_argv0_is_not_resolved(repo: Path) -> None:
    """`.venv/bin/python` must stay the venv's path, not the interpreter it links to."""
    target = script(repo, "real/python")
    link = repo / ".venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    put(repo / ".candybar.toml", multi("venv", argv='["./.venv/bin/python", "-V"]'))
    (d,) = load_project(Project(repo)).defs
    assert d.manifest.runner["argv"][0] == str(repo / ".venv" / "bin" / "python")


def test_linked_worktree_keeps_the_main_repo_name(tmp_path: Path) -> None:
    main = git_repo(tmp_path / "mainrepo")
    subprocess.run(["git", "-C", str(main), "commit", "-q", "--allow-empty", "-m", "x"], check=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"})
    wt = tmp_path / "feature-wt"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(wt)], check=True)
    found = find_project(wt)
    assert found is not None and found.root == wt.resolve()
    assert found.name == "mainrepo" and found.tag == "project:mainrepo"
    assert find_project(main).name == "mainrepo"


def test_legacy_manifest_tolerates_other_top_level_tables(tmp_path: Path, no_git: None) -> None:
    script(tmp_path, "run.sh")
    put(tmp_path / "candybar.toml",
        '[meta]\nowner = "x"\n[candybar]\nname = "legacy"\nschedule = "* * * * *"\nentrypoint = "./run.sh"\n')
    result = load_path(tmp_path)
    assert result.ok, result.errors
    assert result.defs[0].manifest.tags == []


def test_a_foreign_project_tag_is_refused(repo: Path) -> None:
    put(repo / ".candybar.toml", multi("x").replace('schedule', 'tags = ["project:other"]\nschedule'))
    result = load_project(Project(repo))
    assert not result.defs
    assert any("project:other" in e and "added automatically" in e for e in result.errors)


def test_on_failure_resolves_for_every_runner_kind(repo: Path) -> None:
    put(repo / ".candybar.toml",
        '[candybars.svc]\nschedule = "* * * * *"\n[candybars.svc.runner]\nkind = "systemd"\n'
        'unit = "x.service"\non_failure = ["./bin/alert", "--now"]\n')
    (d,) = load_project(Project(repo)).defs
    assert d.manifest.runner["on_failure"] == [str(repo / "bin" / "alert"), "--now"]


def test_append_of_an_existing_name_says_so(repo: Path) -> None:
    put(repo / ".candybar.toml", multi("dup"))
    with pytest.raises(WriteError, match="already defines 'dup'"):
        plan_write({"name": "dup", "schedule": "* * * * *", "runner": {"kind": "exec", "argv": ["true"]}},
                   repo / ".candybar.toml", project=Project(repo))
