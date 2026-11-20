"""Regenerate/check the shipped Typer usage snapshot from registered commands."""

import argparse
from pathlib import Path

import typer_usage

from candybar.cli.main import app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    check = parser.parse_args().check
    target = Path(__file__).resolve().parents[1] / "src/candybar/cli/candybar.usage.kdl"
    rendered = typer_usage.generate(app, bin_name="candybar")
    if check:
        if target.read_text() != rendered:
            raise SystemExit("Typer command table differs from candybar.usage.kdl; regenerate")
        print("candybar usage snapshot matches Typer commands")
    else:
        target.write_text(rendered)


if __name__ == "__main__":
    main()
