"""`candybar create` (alias `new`): write a candybar file, interactively or from flags.

Inside a project the default file is `<root>/.candybars/<name>.candybar.toml`;
`--project-file` appends to `<root>/.candybar.toml`; `--out` writes exactly
there; outside any project it is `./<name>.candybar.toml`. The file goes
through `project.plan_write`, so what lands on disk is exactly what
`candybar register` will accept.

The wizard (Textual) only collects answers and is imported lazily; this
module does the validating, writing and registering.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from candybar.migration.client import ApiError, CandybarApi
from candybar.project import (
    Planned,
    Project,
    WriteError,
    find_project,
    plan_write,
    write,
)
from candybar.tui.schedule import next_fires
from candybar.tui.spec import (
    Answers,
    CreateResult,
    SpecError,
    build_spec,
    host_timezone,
    name_error,
    parse_hermes_ref,
    schedule_error,
    timezone_error,
)


def _fail(message: str, code: int = 2) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code=code)


def _runner_flags(
    exec_: str | None, systemd: str | None, hermes: str | None, n8n: str | None, entrypoint: str | None
) -> list[tuple[str, str]]:
    given = [
        ("exec", exec_), ("systemd", systemd), ("hermes", hermes), ("n8n", n8n), ("entrypoint", entrypoint),
    ]
    return [(kind, value) for kind, value in given if value is not None]


def _answers_from_flags(
    *,
    name: str | None,
    description: str | None,
    tags: str | None,
    schedule: str | None,
    timezone: str | None,
    runner: list[tuple[str, str]],
    workdir: str | None,
    env: list[str],
    data: str | None,
    timeout: int | None,
    on_failure: str | None,
    noise: bool,
    disabled: bool,
) -> Answers:
    a = Answers(
        name=name or "",
        description=description or "",
        tags=tags or "",
        schedule=schedule or "",
        timezone=timezone or "",  # "" = not given: the host's zone (wizard and flags alike)
        workdir=workdir or "",
        env=list(env),
        data=data or "",
        timeout="" if timeout is None else str(timeout),
        on_failure=on_failure or "",
        noise=noise,
        enabled=not disabled,
    )
    for kind, value in runner:
        a.kind = kind
        if kind == "exec":
            a.command = value
        elif kind == "systemd":
            a.unit = value
        elif kind == "hermes":
            a.profile, a.job_id = parse_hermes_ref(value)
        elif kind == "n8n":
            a.target = value
        else:
            a.entrypoint = value
    return a


def _destination(
    project: Project | None, name: str, out: Path | None, project_file: bool
) -> Path:
    if out is not None:
        return out if out.is_absolute() else Path.cwd() / out
    if project_file:
        if project is None:
            raise _fail("--project-file needs a project: run this inside a git repo (or a directory with .candybars/)")
        return project.project_file
    if project is not None:
        return project.default_file_for(name)
    return Path.cwd() / f"{name}.candybar.toml"


def _register(planned: Planned, name: str) -> None:
    found = next((d for d in planned.result.defs if d.name == name), None)
    if found is None:  # pragma: no cover - plan_write already validated the file
        raise _fail(f"cannot register: {name!r} not found in {planned.path}", 1)
    try:
        row = CandybarApi().register(found.manifest.model_dump(mode="json"))
    except ApiError as exc:
        where = "candybar API unreachable" if exc.unreachable else f"API refused (HTTP {exc.status})"
        raise _fail(f"{where}: {exc}; the file stays written at {planned.path}", 1) from exc
    typer.echo(f"registered {row.get('name', name)} (id {row.get('id', '?')})")
    if not found.manifest.enabled:
        typer.echo(f"registered disabled; start it with: candybar resume {name}")


def create(
    name: Annotated[str | None, typer.Option("--name", help="Candybar name (lowercase slug).")] = None,
    description: Annotated[str | None, typer.Option("--description", help="What it does.")] = None,
    tags: Annotated[str | None, typer.Option("--tags", help="Comma-separated tags.")] = None,
    schedule: Annotated[str | None, typer.Option("--schedule", help="5-field cron, e.g. '0 3 * * *'.")] = None,
    timezone: Annotated[
        str | None, typer.Option("--timezone", help="IANA zone the cron is read in (default: this host's zone).")
    ] = None,
    exec_: Annotated[str | None, typer.Option("--exec", help='Run a command line, e.g. --exec "./backup.sh --quiet".')] = None,
    workdir: Annotated[str | None, typer.Option("--workdir", help="exec: working directory (default: project root).")] = None,
    env: Annotated[list[str] | None, typer.Option("--env", help="exec: KEY=VAL, repeatable.")] = None,
    systemd: Annotated[str | None, typer.Option("--systemd", help="Start this user service unit.")] = None,
    hermes: Annotated[str | None, typer.Option("--hermes", help="Trigger a Hermes job: PROFILE:JOB_ID.")] = None,
    n8n: Annotated[str | None, typer.Option("--n8n", help="POST to an n8n webhook: a path, or a full http(s) URL.")] = None,
    data: Annotated[str | None, typer.Option("--data", help="n8n: static JSON object handed to the workflow.")] = None,
    entrypoint: Annotated[str | None, typer.Option("--entrypoint", help="Run this executable script file.")] = None,
    timeout: Annotated[int | None, typer.Option("--timeout", help="exec/hermes/n8n: timeout in seconds.")] = None,
    on_failure: Annotated[str | None, typer.Option("--on-failure", help="Command line run after a failure.")] = None,
    noise: Annotated[bool, typer.Option("--noise", help="Mark the candybar as noisy.")] = False,
    disabled: Annotated[bool, typer.Option("--disabled", help="Create it disabled (enabled = false).")] = False,
    project_file: Annotated[
        bool, typer.Option("--project-file", help="Append to <root>/.candybar.toml instead of its own file.")
    ] = False,
    out: Annotated[Path | None, typer.Option("--out", help="Write exactly this path.")] = None,
    print_: Annotated[bool, typer.Option("--print", help="Render and validate, print the TOML, write nothing.")] = False,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing one-candybar file.")] = False,
    register: Annotated[bool, typer.Option("--register", help="Register it through the API after writing.")] = False,
    no_tui: Annotated[
        bool, typer.Option("--no-tui", "--yes", "-y", help="Never prompt: everything comes from the flags.")
    ] = False,
) -> None:
    """Create a candybar file: a step-by-step TUI, or flags with --no-tui.

    Inside a git repo the file is .candybars/<name>.candybar.toml (or an entry
    in .candybar.toml with --project-file), so `candybar register` there picks
    it up. Outside a project it is ./<name>.candybar.toml. (Alias: `candybar new`.)
    """
    if out is not None and project_file:
        raise _fail("--out and --project-file are mutually exclusive")
    runner = _runner_flags(exec_, systemd, hermes, n8n, entrypoint)
    if len(runner) > 1:
        flags = ", ".join(f"--{kind}" for kind, _ in runner)
        raise _fail(f"choose exactly one runner, got {flags}")
    try:
        answers = _answers_from_flags(
            name=name, description=description, tags=tags, schedule=schedule, timezone=timezone,
            runner=runner, workdir=workdir, env=env or [], data=data, timeout=timeout,
            on_failure=on_failure, noise=noise, disabled=disabled,
        )
    except SpecError as exc:
        raise _fail(str(exc)) from exc
    project = find_project()

    if no_tui:
        result = _from_flags(answers, project, out, project_file, force)
    else:
        result = _from_wizard(answers, project, out, project_file, force)
        register = register or result.register

    planned = _plan(result.spec, result.dest, project, result.overwrite or force)
    if print_:
        typer.echo(planned.text, nl=False)
        return
    try:
        path = write(planned)
    except OSError as exc:
        raise _fail(f"cannot write {planned.path}: {exc}", 1) from exc
    name_ = result.spec["name"]
    typer.echo(f"appended {name_} to {path}" if planned.appended else f"wrote {path}")
    if register:
        _register(planned, name_)
    elif project is not None and project.owns(path):
        typer.echo("next: candybar register")
    else:
        typer.echo(f"next: candybar register {path}")


def _plan(spec: dict, dest: Path, project: Project | None, overwrite: bool) -> Planned:
    try:
        return plan_write(spec, dest, project=project, overwrite=overwrite)
    except WriteError as exc:
        raise _fail(str(exc)) from exc


def _from_flags(
    a: Answers, project: Project | None, out: Path | None, project_file: bool, force: bool
) -> CreateResult:
    missing = []
    if not a.name:
        missing.append("--name")
    if not a.schedule:
        missing.append("--schedule")
    if not a.kind:
        missing.append("a runner (--exec, --systemd, --hermes, --n8n or --entrypoint)")
    if missing:
        raise _fail("missing: " + ", ".join(missing) + " (or run without --no-tui for the wizard)")
    if not a.timezone:
        a.timezone = host_timezone()
    for problem in (name_error(a.name), schedule_error(a.schedule), timezone_error(a.timezone)):
        if problem:
            raise _fail(problem)
    if not next_fires(a.schedule, a.timezone, count=1):
        typer.echo(f"warning: {a.schedule!r} never fires (no such date); writing it anyway", err=True)
    try:
        spec = build_spec(a)
    except SpecError as exc:
        raise _fail(str(exc)) from exc
    return CreateResult(spec=spec, dest=_destination(project, a.name, out, project_file), overwrite=force)


def _from_wizard(
    a: Answers, project: Project | None, out: Path | None, project_file: bool, force: bool
) -> CreateResult:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise _fail("the wizard needs a terminal; pass --no-tui with --name, --schedule and a runner flag")
    if out is not None:
        choice, custom = "custom", str(out)
    elif project_file:
        if project is None:
            raise _fail("--project-file needs a project: run this inside a git repo (or a directory with .candybars/)")
        choice, custom = "project-file", ""
    else:
        choice, custom = "default", ""
    from candybar.tui.create_app import run_create_wizard

    result = run_create_wizard(project, a, dest_choice=choice, custom_path=custom, overwrite=force)
    if result is None:
        typer.echo("cancelled, nothing written", err=True)
        raise typer.Exit(code=1)
    return result


def install(app: typer.Typer) -> None:
    """Register `create` (and its `new` alias) on the root CLI."""
    app.command("create")(create)
    app.command("new", hidden=True)(create)


__all__ = ["create", "install"]
