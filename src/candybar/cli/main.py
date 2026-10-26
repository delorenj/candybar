"""candybar CLI entry point.

A bare typer app for the walking skeleton: `--help` and `--version` work today;
`daemon` (Story 1.3) runs the role-composed process; `register` and `run`
arrive with Stories 1.4 and 1.8.
"""

import asyncio
import logging
import signal
from typing import Annotated

import typer

from candybar import __version__

app = typer.Typer(
    name="candybar",
    help="Candybar cron framework CLI — register, invoke, and inspect scheduled candybars.",
)


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


if __name__ == "__main__":
    app()
