"""Native manifest autosync CLI; registration and scheduling remain API-owned."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer


def default_config() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "candybar/watch.toml"


def watch(
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Watch config (default: $XDG_CONFIG_HOME/candybar/watch.toml)."),
    ] = None,
    once: Annotated[
        bool, typer.Option("--once", help="Reconcile once and exit instead of watching filesystem events."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Preview one reconciliation without changing the registry."),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print a one-shot report as JSON (requires --once or --dry-run)."),
    ] = False,
) -> None:
    """Sync project and legacy candybar manifests on save, through the API.

    Configuration explicitly enrolls project directories, legacy files, or small
    discovery roots. New registrations start paused. Existing pause/archive/skip
    state is never synchronized, and removing a file never deletes a job.
    No polling backend, Git push, or recurring schedule is used.
    """
    from candybar.autosync import load_config, reconcile, run_watch

    path = (config or default_config()).expanduser().absolute()
    if as_json and not (once or dry_run):
        typer.echo("--json requires --once or --dry-run", err=True)
        raise typer.Exit(code=2)
    try:
        settings = load_config(path)
    except (OSError, TypeError, ValueError) as exc:
        typer.echo(f"invalid watch config {path}: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if once or dry_run:
        report = reconcile(settings, dry_run=dry_run)
        if as_json:
            typer.echo(json.dumps({**asdict(report), "ok": report.ok}, default=str))
        else:
            for result in report.results:
                fields = result.get("fields") or []
                suffix = f" ({', '.join(fields)})" if fields else ""
                typer.echo(f"{result['status']} {result.get('name', '?')}: {result.get('source', '')}{suffix}")
            for error in report.errors:
                typer.echo(error, err=True)
            typer.echo("watch reconciliation: " + ("ok" if report.ok else "unsynced"))
        if not report.ok:
            raise typer.Exit(code=1)
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        run_watch(settings, config_path=path)
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError) as exc:
        typer.echo(f"watch stopped: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def install(app: typer.Typer) -> None:
    app.command()(watch)
