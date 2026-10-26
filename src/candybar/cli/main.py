"""candybar CLI entry point.

A bare typer app for the walking skeleton: `--help` and `--version` work today;
subcommands (`register`, `run`) arrive with Stories 1.4 and 1.8.
"""

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


if __name__ == "__main__":
    app()
