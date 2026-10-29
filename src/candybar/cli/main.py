"""candybar CLI entry point.

`--help`/`--version` and `daemon` (Story 1.3); `register` (Story 1.4)
loads a candybar.toml manifest and registers it via the API; `run`
(Story 1.8) invokes a registered candybar now via the API (AD-12);
`migrate` (Story 7.5, AD-21) drives the cron-migration ledger; the
operator control commands (`ls`, `show`, `next`, `schedule`, `history`,
`pause`, `resume`, `skip`, `edit`, `tail`) live in `candybar.cli.control`.
"""

import asyncio
import json
import logging
import os
import signal
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

from candybar import __version__
from candybar.cli import control, doctor
from candybar.cli.migrate import migrate_app

app = typer.Typer(
    name="candybar",
    help="Candybar cron framework CLI — register, invoke, and inspect scheduled candybars.",
)

API_URL_ENV = "CANDYBAR_API_URL"
DEFAULT_API_URL = "http://127.0.0.1:8700"

app.add_typer(migrate_app, name="migrate")
control.install(app)
doctor.install(app)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"candybar {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", "-V", callback=_version_callback, is_eager=True, help="Show version and exit."),
    ] = False,
) -> None:
    """Candybar cron framework CLI."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


@app.command()
def daemon(
    roles: Annotated[
        str,
        typer.Option(
            "--roles",
            help="Comma-separated roles to run, or 'all' (default). Duplicates are dropped.",
        ),
    ] = "all",
) -> None:
    """Run the candybar daemon: selected roles in one asyncio process (AD-2).

    NATS is configured via the NATS_URL env var (default nats://localhost:4222).
    """
    from candybar.daemon import resolve_roles, run_daemon

    try:
        selected = resolve_roles(roles)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(f"candybar daemon starting roles: {', '.join(selected)}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    stop = asyncio.Event()

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await run_daemon(selected, stop)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        # Belt-and-braces for the window before signal handlers install.
        stop.set()
    # A crashed role propagates out of the TaskGroup above (nonzero exit);
    # reaching here means a clean, signal-driven shutdown.


@app.command()
def register(
    path: Annotated[Path, typer.Argument(help="Directory containing candybar.toml, or the file itself.")],
) -> None:
    """Register a candybar from its candybar.toml manifest via the API (Story 1.4).

    The API base URL comes from CANDYBAR_API_URL (default http://127.0.0.1:8700).
    Manifest validation happens locally first (slug name, 5-field cron,
    entrypoint must exist); the API re-validates server-side.
    """
    from candybar.registry import ManifestError, load_manifest

    try:
        manifest = load_manifest(path)
    except ManifestError as exc:
        typer.echo(f"invalid manifest: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    base = os.environ.get(API_URL_ENV, DEFAULT_API_URL).rstrip("/")
    payload = manifest.model_dump_json().encode()
    request = urllib.request.Request(
        f"{base}/candybars",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        # The API's problem+json body explains the rejection.
        typer.echo(exc.read().decode(), err=True)
        raise typer.Exit(code=1) from exc
    except (urllib.error.URLError, OSError) as exc:
        typer.echo(f"candybar API unreachable at {base}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"registered {body['name']} (id {body['id']})")


@app.command()
def run(
    candybar_ref: Annotated[
        str, typer.Argument(metavar="NAME|ID", help="Candybar name or registry id (UUID).")
    ],
    wait: Annotated[
        bool, typer.Option("--wait", "-w", help="Wait for the run to finish and print its result.")
    ] = False,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Seconds --wait gives up after (the run keeps going).")
    ] = 3600,
) -> None:
    """Invoke a registered candybar now, out of schedule (Story 1.8, AD-12).

    Calls POST /candybars/{id}/invoke on CANDYBAR_API_URL (default
    http://127.0.0.1:8700). The API publishes a trigger=manual invoke command
    and the executor runs it exactly like a scheduled fire; this command
    returns once the invocation is accepted, not when it finishes, unless
    --wait (exit 1 on a failed run, 3 when --timeout passes first).
    """
    from candybar.migration.client import ApiError, CandybarApi

    base = os.environ.get(API_URL_ENV, DEFAULT_API_URL).rstrip("/")
    try:
        UUID(candybar_ref)
        candybar_id = candybar_ref
    except ValueError:
        # A name: resolve it to the id the invoke route takes.
        candybar_id = control.resolve_id(CandybarApi(base), candybar_ref)
    # Quote the whole segment: an id can never reach a different route.
    url = f"{base}/candybars/{urllib.parse.quote(candybar_id, safe='')}/invoke"
    request = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        # The API's problem+json body explains the rejection (404 unknown id, ...).
        typer.echo(exc.read().decode(), err=True)
        raise typer.Exit(code=1) from exc
    except (urllib.error.URLError, OSError) as exc:
        typer.echo(f"candybar API unreachable at {base}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        f"invoked {body['name']} (id {body['candybarId']}): "
        f"invocation {body['invocationId']}, idempotency_key {body['idempotencyKey']}"
    )
    if not wait:
        return
    try:
        row = control.wait_for_invocation(CandybarApi(base), body["invocationId"], timeout)
    except ApiError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if row is None:
        typer.echo(f"still running after {timeout:g}s; check `candybar history {body['name']}`", err=True)
        raise typer.Exit(code=3)
    control.print_run(row)
    if row["result"] != "success":
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
