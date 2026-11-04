"""`candybar create --no-tui`, plus the pure helpers behind it (timezone, spec building)."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from candybar.cli.main import app
from candybar.migration.client import ApiError
from candybar.project import load_path
from candybar.tui import spec as spec_mod
from candybar.tui.spec import (
    Answers,
    SpecError,
    build_spec,
    host_timezone,
    list_user_units,
)

BASE = ["create", "--no-tui", "--schedule", "*/5 * * * *"]


@pytest.fixture(autouse=True)
def _utc_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "UTC")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "myproj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.chdir(root)
    return root.resolve()


@pytest.fixture
def loose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory that is neither a git repo nor holds candybar files."""
    here = tmp_path / "loose"
    here.mkdir()
    monkeypatch.chdir(here)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    return here.resolve()


def run(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def written(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


# --- every runner kind ---------------------------------------------------------


def test_exec_in_project_defaults_to_candybars_dir(project: Path) -> None:
    result = run(
        *BASE, "--name", "backup", "--description", "nightly", "--tags", "a, b",
        "--exec", "./scripts/backup.sh --quiet", "--env", "K=V", "--env", "X=1",
        "--timeout", "90", "--on-failure", "ntfy-publish --topic alerts",
    )
    assert result.exit_code == 0, result.output
    dest = project / ".candybars" / "backup.candybar.toml"
    assert f"wrote {dest}" in result.output
    assert "next: candybar register" in result.output
    table = written(dest)["candybar"]
    assert table["name"] == "backup" and table["tags"] == ["a", "b"]
    assert table["runner"] == {
        "kind": "exec",
        "argv": ["./scripts/backup.sh", "--quiet"],
        "env": {"K": "V", "X": "1"},
        "timeout_s": 90,
        "on_failure": ["ntfy-publish", "--topic", "alerts"],
    }
    loaded = load_path(dest)
    assert loaded.ok, loaded.errors
    manifest = loaded.defs[0].manifest
    assert f"project:{project.name}" in manifest.tags
    assert manifest.runner["workdir"] == str(project)


def test_exec_workdir_flag(project: Path) -> None:
    result = run(*BASE, "--name", "w", "--exec", "make build", "--workdir", "/srv/x")
    assert result.exit_code == 0, result.output
    assert written(project / ".candybars" / "w.candybar.toml")["candybar"]["runner"]["workdir"] == "/srv/x"


def test_systemd(project: Path) -> None:
    result = run(*BASE, "--name", "prune", "--systemd", "cache-prune.service")
    assert result.exit_code == 0, result.output
    runner = written(project / ".candybars" / "prune.candybar.toml")["candybar"]["runner"]
    assert runner == {"kind": "systemd", "unit": "cache-prune.service"}


def test_hermes(project: Path) -> None:
    result = run(*BASE, "--name", "digest", "--hermes", "33god-pm:a1b2c3", "--timeout", "600")
    assert result.exit_code == 0, result.output
    runner = written(project / ".candybars" / "digest.candybar.toml")["candybar"]["runner"]
    assert runner == {"kind": "hermes", "profile": "33god-pm", "job_id": "a1b2c3", "timeout_s": 600}


def test_hermes_needs_profile_and_job(project: Path) -> None:
    result = run(*BASE, "--name", "digest", "--hermes", "justprofile")
    assert result.exit_code == 2
    assert "PROFILE:JOB_ID" in result.output


def test_n8n_webhook_and_url(project: Path) -> None:
    result = run(*BASE, "--name", "hook", "--n8n", "candybar/digest", "--data", '{"lane": "ops"}')
    assert result.exit_code == 0, result.output
    runner = written(project / ".candybars" / "hook.candybar.toml")["candybar"]["runner"]
    assert runner == {"kind": "n8n", "webhook": "candybar/digest", "data": {"lane": "ops"}}
    result = run(*BASE, "--name", "hook2", "--n8n", "https://n8n.example/webhook/x")
    assert result.exit_code == 0, result.output
    runner = written(project / ".candybars" / "hook2.candybar.toml")["candybar"]["runner"]
    assert runner == {"kind": "n8n", "url": "https://n8n.example/webhook/x"}


def test_n8n_bad_data(project: Path) -> None:
    result = run(*BASE, "--name", "hook", "--n8n", "x/y", "--data", "[1]")
    assert result.exit_code == 2
    assert "JSON object" in result.output


def test_entrypoint(project: Path) -> None:
    (project / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    result = run(*BASE, "--name", "script", "--entrypoint", "./run.sh")
    assert result.exit_code == 0, result.output
    table = written(project / ".candybars" / "script.candybar.toml")["candybar"]
    assert table["entrypoint"] == "./run.sh" and "runner" not in table
    assert load_path(project / ".candybars" / "script.candybar.toml").ok


def test_entrypoint_missing_file_exits_2(project: Path) -> None:
    result = run(*BASE, "--name", "script", "--entrypoint", "./nope.sh")
    assert result.exit_code == 2
    assert "not an existing file" in result.output
    assert not (project / ".candybars").exists()


def test_entrypoint_rejects_runner_options(project: Path) -> None:
    result = run(*BASE, "--name", "s", "--entrypoint", "./run.sh", "--timeout", "5")
    assert result.exit_code == 2
    assert "needs a runner" in result.output


def test_noise_disabled_timezone(project: Path) -> None:
    result = run(
        *BASE, "--name", "quiet", "--exec", "true", "--noise", "--disabled",
        "--timezone", "America/New_York",
    )
    assert result.exit_code == 0, result.output
    table = written(project / ".candybars" / "quiet.candybar.toml")["candybar"]
    assert table["noise"] is True and table["enabled"] is False
    assert table["timezone"] == "America/New_York"


def test_timeout_on_systemd_exits_2(project: Path) -> None:
    result = run(*BASE, "--name", "p", "--systemd", "x.service", "--timeout", "5")
    assert result.exit_code == 2
    assert "no timeout" in result.output


# --- destinations -----------------------------------------------------------------


def test_project_file_appends_and_creates(project: Path) -> None:
    first = run(*BASE, "--name", "one", "--exec", "true", "--project-file")
    assert first.exit_code == 0, first.output
    assert f"wrote {project / '.candybar.toml'}" in first.output
    second = run(*BASE, "--name", "two", "--exec", "false", "--project-file")
    assert second.exit_code == 0, second.output
    assert f"appended two to {project / '.candybar.toml'}" in second.output
    assert set(written(project / ".candybar.toml")["candybars"]) == {"one", "two"}


def test_out_writes_exactly_there(project: Path, tmp_path: Path) -> None:
    dest = tmp_path / "elsewhere" / "x.candybar.toml"
    result = run(*BASE, "--name", "x", "--exec", "true", "--out", str(dest))
    assert result.exit_code == 0, result.output
    assert dest.is_file()
    assert not (project / ".candybars").exists()
    assert f"next: candybar register {dest}" in result.output


def test_out_and_project_file_conflict(project: Path) -> None:
    result = run(*BASE, "--name", "x", "--exec", "true", "--out", "a.toml", "--project-file")
    assert result.exit_code == 2


def test_outside_project_defaults_to_cwd(loose: Path) -> None:
    result = run(*BASE, "--name", "solo", "--exec", "true")
    assert result.exit_code == 0, result.output
    dest = loose / "solo.candybar.toml"
    assert f"wrote {dest}" in result.output
    assert f"next: candybar register {dest}" in result.output
    assert load_path(dest).ok


def test_project_file_outside_project_exits_2(loose: Path) -> None:
    result = run(*BASE, "--name", "solo", "--exec", "true", "--project-file")
    assert result.exit_code == 2
    assert "needs a project" in result.output


def test_print_writes_nothing(project: Path) -> None:
    result = run(*BASE, "--name", "peek", "--exec", "true", "--print")
    assert result.exit_code == 0, result.output
    assert tomllib.loads(result.output)["candybar"]["name"] == "peek"
    assert not (project / ".candybars").exists()


def test_print_still_validates(project: Path) -> None:
    result = run("create", "--no-tui", "--schedule", "bad cron", "--name", "p", "--exec", "true", "--print")
    assert result.exit_code == 2


def test_existing_file_refused_then_forced(project: Path) -> None:
    assert run(*BASE, "--name", "again", "--exec", "true").exit_code == 0
    again = run(*BASE, "--name", "again", "--exec", "false")
    assert again.exit_code == 2 and "already exists" in again.output
    forced = run(*BASE, "--name", "again", "--exec", "false", "--force")
    assert forced.exit_code == 0, forced.output
    argv = written(project / ".candybars" / "again.candybar.toml")["candybar"]["runner"]["argv"]
    assert argv == ["false"]


def test_name_clash_across_project_files_exits_2(project: Path) -> None:
    assert run(*BASE, "--name", "dup", "--exec", "true", "--project-file").exit_code == 0
    clash = run(*BASE, "--name", "dup", "--exec", "true")
    assert clash.exit_code == 2
    assert "already defined" in clash.output
    assert not (project / ".candybars" / "dup.candybar.toml").exists()


def test_project_file_name_clash_exits_2(project: Path) -> None:
    assert run(*BASE, "--name", "dup", "--exec", "true", "--project-file").exit_code == 0
    clash = run(*BASE, "--name", "dup", "--exec", "true", "--project-file")
    assert clash.exit_code == 2


# --- argument errors ----------------------------------------------------------------


def test_missing_everything_names_what_is_missing(project: Path) -> None:
    result = run("create", "--no-tui")
    assert result.exit_code == 2
    for needle in ("--name", "--schedule", "runner"):
        assert needle in result.output


def test_missing_runner_only(project: Path) -> None:
    result = run(*BASE, "--name", "x")
    assert result.exit_code == 2
    assert "runner" in result.output and "--name" not in result.output.replace("(--name", "")


def test_two_runners_exit_2(project: Path) -> None:
    result = run(*BASE, "--name", "x", "--exec", "true", "--systemd", "a.service")
    assert result.exit_code == 2
    assert "exactly one runner" in result.output


def test_invalid_cron_exit_2(project: Path) -> None:
    result = run("create", "-y", "--name", "x", "--exec", "true", "--schedule", "* * *")
    assert result.exit_code == 2
    assert "5-field" in result.output


def test_hashed_cron_exit_2(project: Path) -> None:
    result = run("create", "-y", "--name", "x", "--exec", "true", "--schedule", "H * * * *")
    assert result.exit_code == 2
    assert "hashed" in result.output


def test_invalid_name_exit_2(project: Path) -> None:
    result = run(*BASE, "--name", "Bad_Name", "--exec", "true")
    assert result.exit_code == 2
    assert "invalid name" in result.output


def test_invalid_timezone_exit_2(project: Path) -> None:
    result = run(*BASE, "--name", "x", "--exec", "true", "--timezone", "Mars/Base")
    assert result.exit_code == 2


def test_bad_env_line_exit_2(project: Path) -> None:
    result = run(*BASE, "--name", "x", "--exec", "true", "--env", "NOEQUALS")
    assert result.exit_code == 2
    assert "KEY=value" in result.output


def test_bad_command_quoting_exit_2(project: Path) -> None:
    result = run(*BASE, "--name", "x", "--exec", 'echo "unterminated')
    assert result.exit_code == 2


def test_new_alias(project: Path) -> None:
    result = run("new", "-y", "--name", "viaalias", "--schedule", "0 * * * *", "--exec", "true")
    assert result.exit_code == 0, result.output
    assert (project / ".candybars" / "viaalias.candybar.toml").is_file()


def test_tui_refused_without_a_tty(project: Path) -> None:
    result = run("create")
    assert result.exit_code == 2
    assert "--no-tui" in result.output
    assert not (project / ".candybars").exists()


# --- --register -----------------------------------------------------------------------


class _FakeApi:
    calls: ClassVar[list[dict[str, Any]]] = []
    error: ClassVar[ApiError | None] = None

    def register(self, definition: dict[str, Any]) -> dict[str, Any]:
        type(self).calls.append(definition)
        if type(self).error is not None:
            raise type(self).error
        return {"id": "11111111-1111-7111-8111-111111111111", "name": definition["name"]}


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> type[_FakeApi]:
    _FakeApi.calls = []
    _FakeApi.error = None
    monkeypatch.setattr("candybar.cli.create.CandybarApi", _FakeApi)
    return _FakeApi


def test_register_posts_the_resolved_definition(project: Path, fake_api: type[_FakeApi]) -> None:
    result = run(*BASE, "--name", "reg", "--exec", "./go.sh", "--register")
    assert result.exit_code == 0, result.output
    assert "registered reg (id 11111111-1111-7111-8111-111111111111)" in result.output
    sent = fake_api.calls[0]
    assert sent["name"] == "reg"
    assert f"project:{project.name}" in sent["tags"]
    assert sent["runner"]["argv"] == [str(project / "go.sh")]
    assert sent["runner"]["workdir"] == str(project)


def test_register_disabled_hints_resume(project: Path, fake_api: type[_FakeApi]) -> None:
    result = run(*BASE, "--name", "reg", "--exec", "true", "--disabled", "--register")
    assert result.exit_code == 0, result.output
    assert fake_api.calls[0]["enabled"] is False
    assert "candybar resume reg" in result.output


def test_register_api_failure_exits_1_and_keeps_file(project: Path, fake_api: type[_FakeApi]) -> None:
    fake_api.error = ApiError("connection refused")
    result = run(*BASE, "--name", "reg", "--exec", "true", "--register")
    assert result.exit_code == 1
    assert "unreachable" in result.output
    assert (project / ".candybars" / "reg.candybar.toml").is_file()


def test_register_not_called_without_flag(project: Path, fake_api: type[_FakeApi]) -> None:
    assert run(*BASE, "--name", "reg", "--exec", "true").exit_code == 0
    assert fake_api.calls == []


# --- pure helpers -----------------------------------------------------------------------


def test_host_timezone_prefers_tz_env(tmp_path: Path) -> None:
    assert host_timezone(localtime=tmp_path / "nope", env={"TZ": ":Europe/Paris"}) == "Europe/Paris"


def test_host_timezone_reads_localtime_symlink(tmp_path: Path) -> None:
    link = tmp_path / "localtime"
    link.symlink_to("/usr/share/zoneinfo/America/Chicago")
    assert host_timezone(localtime=link, env={}) == "America/Chicago"


def test_host_timezone_falls_back_to_utc(tmp_path: Path) -> None:
    assert host_timezone(localtime=tmp_path / "missing", env={}) == "UTC"
    plain = tmp_path / "localtime"
    plain.write_text("not a link", encoding="utf-8")
    assert host_timezone(localtime=plain, env={"TZ": "Not/AZone"}) == "UTC"


def test_host_timezone_skips_invalid_tz_for_symlink(tmp_path: Path) -> None:
    link = tmp_path / "localtime"
    link.symlink_to("/usr/share/zoneinfo/Asia/Tokyo")
    assert host_timezone(localtime=link, env={"TZ": "garbage"}) == "Asia/Tokyo"


def test_build_spec_requires_a_runner_kind() -> None:
    with pytest.raises(SpecError) as info:
        build_spec(Answers(name="x", schedule="* * * * *"))
    assert info.value.field == "kind"


def test_build_spec_blames_the_field() -> None:
    with pytest.raises(SpecError) as info:
        build_spec(Answers(name="x", schedule="* * * * *", kind="exec", command="true", timeout="abc"))
    assert info.value.field == "timeout"
    with pytest.raises(SpecError) as info:
        build_spec(Answers(name="x", schedule="* * * * *", kind="exec", command="true", env=["1BAD=x"]))
    assert info.value.field == "env"


def test_list_user_units_parses_and_never_raises() -> None:
    out = SimpleNamespace(
        returncode=0,
        stdout="b.service enabled enabled\na.service static -\ntmpl@.service static -\nx.timer enabled -\n",
    )
    assert list_user_units(lambda *a, **k: out) == ["a.service", "b.service"]

    def boom(*a: Any, **k: Any) -> Any:
        raise FileNotFoundError("systemctl")

    assert list_user_units(boom) == []
    assert list_user_units(lambda *a, **k: SimpleNamespace(returncode=1, stdout="a.service x")) == []
    assert spec_mod.KINDS[0] == "exec"
