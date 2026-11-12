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
    path: Annotated[
        Path | None, typer.Argument(help="Project directory or saved manifest; defaults to current project."),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Explicit legacy watcher config; otherwise discover current project."),
    ] = None,
    once: Annotated[
        bool, typer.Option("--once", help="Reconcile once and exit instead of watching filesystem events."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Preview one reconciliation without changing the registry."),
    ] = False,
    if_present: Annotated[
        bool, typer.Option("--if-present", help="Exit quietly when this context has no manifests (shell entry hooks)."),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print a one-shot report as JSON (requires --once or --dry-run)."),
    ] = False,
) -> None:
    """Sync project and legacy candybar manifests on save, through the API.

    Discover the current project automatically; no global enrollment is needed.
    --config retains explicit configured mode for compatibility. New registrations
    start paused; existing pause/archive/skip state is never synchronized.
    No polling backend, Git push, or recurring schedule is used.
    """
    from candybar.autosync import load_config, reconcile, run_watch

    config_path = config.expanduser().absolute() if config is not None else None
    if config is not None and path is not None:
        typer.echo("pass a project path or --config, not both", err=True)
        raise typer.Exit(code=2)
    if as_json and not (once or dry_run):
        typer.echo("--json requires --once or --dry-run", err=True)
        raise typer.Exit(code=2)
    try:
        if config_path is not None:
            settings = load_config(config_path)
        else:
            from candybar.context_sync import context_config
            settings = context_config(path)
    except (OSError, TypeError, ValueError) as exc:
        if if_present and isinstance(exc, ValueError) and str(exc).startswith("no candybar manifests in context:"):
            return
        typer.echo(f"invalid watch context {config_path or path or Path.cwd()}: {exc}", err=True)
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
    from candybar.watch_lock import watch_singleton

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    with watch_singleton(settings) as sole:
        if not sole:
            return  # CAND-53: another watcher already covers this tree; stay quiet.
        try:
            run_watch(settings, config_path=config_path)
        except KeyboardInterrupt:
            pass
        except (OSError, ValueError, RuntimeError) as exc:
            typer.echo(f"watch stopped: {exc}", err=True)
            raise typer.Exit(code=1) from exc


def install(app: typer.Typer) -> None:
    app.command()(watch)
