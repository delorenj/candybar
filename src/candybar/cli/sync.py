"""Context-aware synchronization: no global manifest enrollment."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer


def sync(
    path: Annotated[Path | None, typer.Argument(help="Saved manifest or project directory; defaults to current project.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate and preview without registry writes.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable reconciliation report.")] = False,
) -> None:
    """Sync the saved manifest or current project's candybars through the API.

    Project files validate together. Plain candybar.toml keeps legacy path rules.
    No global config or repository enrollment is required. New jobs start paused.
    """
    from candybar.context_sync import sync_context

    try:
        report = sync_context(path, dry_run=dry_run)
    except (OSError, TypeError, ValueError) as exc:
        if as_json:
            typer.echo(json.dumps({"ok": False, "results": [], "errors": [str(exc)]}))
        else:
            typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    if as_json:
        typer.echo(json.dumps({**asdict(report), "ok": report.ok}, default=str))
    else:
        for result in report.results:
            fields = result.get("fields", [])
            suffix = f" ({', '.join(fields)})" if fields else ""
            typer.echo(f"{result['status']} {result['name']}{suffix}")
        for error in report.errors:
            typer.echo(error, err=True)
    if not report.ok:
        raise typer.Exit(code=1)


def install(app: typer.Typer) -> None:
    app.command()(sync)
