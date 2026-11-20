"""Regenerate/check the shipped Typer usage snapshot from registered commands."""

import argparse
import json
from pathlib import Path

import typer_usage

from candybar.cli.main import app
from candybar.cli.doctor import DEFAULT_POLICY


def render_usage() -> str:
    # The policy is bundled with Candybar, not relative to the caller's cwd.
    # Normalize just its parser default; do not rewrite user examples or paths.
    rendered = typer_usage.generate(app, bin_name="candybar")
    absolute = f"default={json.dumps(str(DEFAULT_POLICY))}"
    portable = 'default="<candybar-install>/deploy/scheduling-policy.json"'
    assert rendered.count(absolute) == 1, "doctor policy default registration changed"
    return rendered.replace(absolute, portable)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args()
    check = args.check
    target = Path(__file__).resolve().parents[1] / "src/candybar/cli/candybar.usage.kdl"
    rendered = render_usage()
    if check:
        if target.read_text() != rendered:
            raise SystemExit("Typer command table differs from candybar.usage.kdl; regenerate")
        print("candybar usage snapshot matches Typer commands")
    elif args.stdout:
        print(rendered, end="")
    else:
        target.write_text(rendered)


if __name__ == "__main__":
    main()
