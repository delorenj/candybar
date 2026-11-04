"""The create wizard, driven headlessly with Textual's pilot (no pytest-asyncio needed)."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import (
    Button,
    ContentSwitcher,
    Input,
    RadioSet,
    Select,
    Static,
    Switch,
    TextArea,
)

from candybar.project import Project
from candybar.tui.create_app import CreateApp
from candybar.tui.spec import Answers, CreateResult

SIZE = (110, 45)


@pytest.fixture(autouse=True)
def _utc_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "UTC")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    root = tmp_path / "myproj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.chdir(root)
    return Project(root.resolve())


def drive(
    app: CreateApp, script: Callable[[Any], Awaitable[None]]
) -> CreateResult | None:
    async def main() -> CreateResult | None:
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await script(pilot)
            await pilot.pause()
        return app.return_value

    return asyncio.run(main())


def current(app: CreateApp) -> str:
    return str(app.query_one("#steps", ContentSwitcher).current)


def banner(app: CreateApp) -> str:
    return str(app.query_one("#banner", Static).render())


def shown(app: CreateApp, widget_id: str) -> bool:
    return bool(app.query_one(f"#{widget_id}").display)


def test_happy_path_exec_with_preset(project: Project) -> None:
    app = CreateApp(project, unit_loader=None)

    async def script(pilot: Any) -> None:
        assert "Step 1/6: Basics" in banner(app)
        app.query_one("#name", Input).value = "nightly-backup"
        app.query_one("#description", Input).value = "back it up"
        app.query_one("#tags", Input).value = "backup, nightly"
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-2" and "Step 2/6: Schedule" in banner(app)

        app.query_one("#preset", Select).value = "weekdays"
        await pilot.pause()
        assert shown(app, "box-time") and not shown(app, "box-dow")
        app.query_one("#preset-time", Input).value = "03:15"
        await pilot.pause()
        assert app.query_one("#schedule", Input).value == "15 3 * * 1-5"
        app.query_one("#timezone", Input).value = "America/New_York"
        await pilot.pause()
        preview = str(app.query_one("#sched-preview", Static).render())
        assert "Weekdays at 03:15" in preview and preview.count("Next fires") == 1 and preview.count(" EST") == 5
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-3"

        assert shown(app, "box-exec") and not shown(app, "box-systemd")
        app.query_one("#command", Input).value = "./scripts/backup.sh --quiet"
        app.query_one("#env", TextArea).text = "MODE=full\n\nREGION=us"
        app.query_one("#timeout", Input).value = "600"
        app.query_one("#on_failure", Input).value = "ntfy-publish --topic alerts"
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-4"

        app.query_one("#noise", Switch).value = True
        app.query_one("#enabled", Switch).value = False
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-5"
        assert str(project.root / ".candybars" / "nightly-backup.candybar.toml") in str(
            app.query_one("#dest-abs", Static).render()
        )
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-6" and "Step 6/6: Review" in banner(app)
        assert app.planned is not None and 'name = "nightly-backup"' in app.planned.text
        assert not app.query_one("#write", Button).disabled
        await pilot.press("ctrl+s")

    result = drive(app, script)
    assert result is not None
    assert result.dest == project.root / ".candybars" / "nightly-backup.candybar.toml"
    assert result.register is False and result.overwrite is False
    assert result.spec == {
        "name": "nightly-backup",
        "description": "back it up",
        "tags": ["backup", "nightly"],
        "schedule": "15 3 * * 1-5",
        "timezone": "America/New_York",
        "noise": True,
        "enabled": False,
        "runner": {
            "kind": "exec",
            "argv": ["./scripts/backup.sh", "--quiet"],
            "env": {"MODE": "full", "REGION": "us"},
            "timeout_s": 600,
            "on_failure": ["ntfy-publish", "--topic", "alerts"],
        },
    }


def test_buttons_and_write_register(project: Project) -> None:
    prefill = Answers(name="hooked", schedule="*/5 * * * *", kind="n8n", target="candybar/x")
    app = CreateApp(project, prefill, unit_loader=None)

    async def script(pilot: Any) -> None:
        for _ in range(5):
            await pilot.click("#next")
            await pilot.pause(0.3)  # a pressed Button ignores clicks while it flashes
        assert current(app) == "step-6"
        assert not shown(app, "next") and shown(app, "write-register")
        await pilot.click("#write-register")

    result = drive(app, script)
    assert result is not None and result.register is True
    assert result.spec["runner"] == {"kind": "n8n", "webhook": "candybar/x"}


def test_cancel_returns_none(project: Project) -> None:
    app = CreateApp(project, unit_loader=None)

    async def script(pilot: Any) -> None:
        app.query_one("#name", Input).value = "wont-be-written"
        await pilot.press("escape")

    assert drive(app, script) is None
    assert not (project.root / ".candybars").exists()


def test_invalid_name_blocks_next(project: Project) -> None:
    app = CreateApp(project, unit_loader=None)

    async def script(pilot: Any) -> None:
        app.query_one("#name", Input).value = "Bad Name"
        await pilot.pause()
        assert shown(app, "err-name")  # live, before pressing anything
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-1"
        assert shown(app, "err-name")
        app.query_one("#name", Input).value = ""
        await pilot.click("#next")
        await pilot.pause(0.3)
        assert current(app) == "step-1"
        app.query_one("#name", Input).value = "fine"
        await pilot.pause()
        assert not shown(app, "err-name")
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-2"
        await pilot.press("escape")

    assert drive(app, script) is None


def test_invalid_cron_and_timezone_block_next(project: Project) -> None:
    app = CreateApp(project, Answers(name="x"), unit_loader=None)

    async def script(pilot: Any) -> None:
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-2"
        app.query_one("#schedule", Input).value = "* * *"
        await pilot.pause()
        assert shown(app, "err-schedule")
        assert "5-field" in str(app.query_one("#err-schedule", Static).render())
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-2"
        app.query_one("#schedule", Input).value = "H * * * *"
        await pilot.pause()
        assert "hashed" in str(app.query_one("#err-schedule", Static).render())
        app.query_one("#schedule", Input).value = "0 * * * *"
        app.query_one("#timezone", Input).value = "Mars/Base"
        await pilot.pause()
        assert shown(app, "err-timezone")
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-2"
        app.query_one("#timezone", Input).value = "Europe/Paris"
        await pilot.pause()
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-3"
        await pilot.press("escape")

    assert drive(app, script) is None


def test_runner_step_shows_only_the_chosen_kind_and_validates(project: Project) -> None:
    app = CreateApp(project, Answers(name="x", schedule="0 * * * *"), unit_loader=None)

    async def script(pilot: Any) -> None:
        await pilot.press("ctrl+n")
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-3"
        for kind, box in (("systemd", "box-systemd"), ("hermes", "box-hermes"), ("n8n", "box-n8n"),
                          ("entrypoint", "box-entrypoint"), ("exec", "box-exec")):
            app.query_one("#kind", Select).value = kind
            await pilot.pause()
            visible = [b for b in ("box-exec", "box-systemd", "box-hermes", "box-n8n", "box-entrypoint") if shown(app, b)]
            assert visible == [box]
        assert shown(app, "box-timeout")
        app.query_one("#kind", Select).value = "systemd"
        await pilot.pause()
        assert not shown(app, "box-timeout") and shown(app, "box-on_failure")
        await pilot.press("ctrl+n")  # unit empty
        await pilot.pause()
        assert current(app) == "step-3" and shown(app, "err-unit")
        app.query_one("#unit", Input).value = "bogus"
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-3" and shown(app, "err-unit")
        app.query_one("#unit", Input).value = "cache-prune.service"
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-4"
        await pilot.press("escape")

    assert drive(app, script) is None


def test_entrypoint_must_exist(project: Project) -> None:
    (project.root / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    app = CreateApp(project, Answers(name="s", schedule="0 * * * *", kind="entrypoint"), unit_loader=None)

    async def script(pilot: Any) -> None:
        await pilot.press("ctrl+n")
        await pilot.press("ctrl+n")
        app.query_one("#entrypoint", Input).value = "./missing.sh"
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-3" and shown(app, "err-entrypoint")
        assert not shown(app, "box-on_failure")
        app.query_one("#entrypoint", Input).value = "./run.sh"
        for _ in range(3):
            await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-6"
        await pilot.press("ctrl+s")

    result = drive(app, script)
    assert result is not None and result.spec["entrypoint"] == "./run.sh"
    assert "runner" not in result.spec


def test_back_navigation_and_enter_on_last_field(project: Project) -> None:
    app = CreateApp(project, Answers(name="x"), unit_loader=None)

    async def script(pilot: Any) -> None:
        assert app.query_one("#back", Button).disabled
        app.query_one("#tags", Input).focus()
        await pilot.press("enter")  # last field of step 1 -> next
        await pilot.pause()
        assert current(app) == "step-2"
        assert not app.query_one("#back", Button).disabled
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert current(app) == "step-1"
        await pilot.press("ctrl+s")  # not the review step: nothing happens
        await pilot.pause()
        assert app.return_value is None and current(app) == "step-1"
        await pilot.press("escape")

    assert drive(app, script) is None


def test_destination_choices(project: Project) -> None:
    prefill = Answers(name="here", schedule="0 * * * *", kind="exec", command="true")
    app = CreateApp(project, prefill, dest_choice="project-file", unit_loader=None)

    async def script(pilot: Any) -> None:
        for _ in range(4):
            await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-5"
        assert str(project.root / ".candybar.toml") in str(app.query_one("#dest-abs", Static).render())
        assert app.query_one("#dest-path", Input).disabled
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert 'candybars.here' in (app.planned.text if app.planned else "")
        await pilot.press("ctrl+s")

    result = drive(app, script)
    assert result is not None and result.dest == project.root / ".candybar.toml"


def test_existing_file_blocks_destination_until_overwrite(project: Project) -> None:
    target = project.candybars_dir / "dup.candybar.toml"
    target.parent.mkdir()
    target.write_text('[candybar]\nname = "dup"\nschedule = "* * * * *"\n[candybar.runner]\nkind = "exec"\nargv = ["true"]\n', encoding="utf-8")
    prefill = Answers(name="dup", schedule="0 * * * *", kind="exec", command="true")
    app = CreateApp(project, prefill, unit_loader=None)

    async def script(pilot: Any) -> None:
        assert shown(app, "warn-name")  # the project already defines it
        for _ in range(4):
            await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-5"
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-5" and shown(app, "err-dest")
        app.query_one("#name", Input).value = "fresh"
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert current(app) == "step-6"
        await pilot.press("escape")

    assert drive(app, script) is None


def test_outside_a_project_only_custom_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    here = tmp_path / "loose"
    here.mkdir()
    monkeypatch.chdir(here)
    prefill = Answers(name="solo", schedule="0 * * * *", kind="exec", command="true")
    app = CreateApp(None, prefill, unit_loader=None, cwd=here)

    async def script(pilot: Any) -> None:
        for _ in range(4):
            await pilot.press("ctrl+n")
        await pilot.pause()
        assert len(app.query_one("#dest", RadioSet).query("RadioButton")) == 1
        assert str(here / "solo.candybar.toml") in str(app.query_one("#dest-abs", Static).render())
        await pilot.press("ctrl+n")
        await pilot.press("ctrl+s")

    result = drive(app, script)
    assert result is not None and result.dest == here / "solo.candybar.toml"


def test_systemd_unit_suggestions_are_best_effort(project: Project) -> None:
    app = CreateApp(project, Answers(name="x", kind="systemd"), unit_loader=lambda: ["a.service", "b.service"])

    async def script(pilot: Any) -> None:
        await pilot.pause(0.3)
        assert "2 user services" in str(app.query_one("#unit-hint", Static).render())
        await pilot.press("escape")

    assert drive(app, script) is None

    def broken() -> list[str]:
        raise RuntimeError("no systemd here")

    app = CreateApp(project, Answers(name="x", kind="systemd"), unit_loader=broken)

    async def script2(pilot: Any) -> None:
        await pilot.pause(0.3)
        await pilot.press("escape")

    assert drive(app, script2) is None


def test_switch_defaults_follow_prefill(project: Project) -> None:
    app = CreateApp(project, Answers(noise=True, enabled=False), unit_loader=None)

    async def script(pilot: Any) -> None:
        assert app.query_one("#noise", Switch).value is True
        assert app.query_one("#enabled", Switch).value is False
        await pilot.press("escape")

    assert drive(app, script) is None


def test_an_explicit_utc_prefill_is_not_replaced_by_the_host_zone(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", "America/New_York")
    assert CreateApp(project, Answers(timezone="UTC"), unit_loader=None).a.timezone == "UTC"
    assert CreateApp(project, Answers(), unit_loader=None).a.timezone == "America/New_York"
