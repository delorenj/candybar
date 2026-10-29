"""Thin read-only CLI for Candybar scheduling policy."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import typer

from candybar import scheduling_policy as policy

DEFAULT_POLICY = Path(__file__).resolve().parents[3] / "deploy/scheduling-policy.json"


def install(app: typer.Typer) -> None:
    @app.command("doctor")
    def doctor(
        repo: Annotated[Path | None, typer.Option(help="Lint tracked and untracked repo timer units.")] = None,
        live: Annotated[bool, typer.Option(help="Read machine timers, crontabs, all Hermes profiles and registry.")] = False,
        inventory: Annotated[Path | None, typer.Option(help="Audit an offline inventory JSON (no live calls).")] = None,
        policy_file: Annotated[Path, typer.Option("--policy", help="Exact exceptions with owner/reason.")] = DEFAULT_POLICY,
        n8n_export: Annotated[Path | None, typer.Option(help="Optional read-only n8n workflow export JSON.")] = None,
        write_inventory: Annotated[Path | None, typer.Option(help="Save sanitized live inventory.")] = None,
        json_output: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        """Find clocks bypassing Candybar. Read-only; exit 0 clean, 1 bypass, 2 unknown."""
        if (live and inventory) or not (live or inventory or repo) or (write_inventory and not live) or (n8n_export and not live):
            raise typer.BadParameter("choose --repo, --live or --inventory; export options require --live")
        reports = {}
        try:
            exceptions = policy.load_policy(policy_file)
            if repo:
                reports["repo"] = policy.repo_lint(repo, exceptions)
            if live or inventory:
                base = os.environ.get("CANDYBAR_API_URL", "http://127.0.0.1:8700")
                data = policy.collect(base, Path.home() / ".hermes", n8n_export) if live else json.loads(Path(str(inventory)).read_text())
                if write_inventory:
                    write_inventory.parent.mkdir(parents=True, exist_ok=True)
                    write_inventory.write_text(json.dumps(data, indent=2) + "\n")
                reports["machine"] = policy.audit(data, exceptions, base=base if live else None)
        except (OSError, ValueError, TypeError):
            reports["input"] = {"status": "unknown", "unknown": ["policy or inventory unreadable/malformed"]}
        status = "unknown" if any(r["status"] == "unknown" for r in reports.values()) else "fail" if any(r["status"] == "fail" for r in reports.values()) else "pass"
        report = {"status": status, "checks": reports}
        if json_output:
            typer.echo(json.dumps(report, indent=2))
        else:
            typer.echo(f"scheduling-policy: {status.upper()}")
            for name, result in reports.items():
                for row in result.get("violations", []):
                    typer.echo(f"{name}: {row['id']}: {row['message']}")
                for unknown in result.get("unknown", []):
                    typer.echo(f"UNKNOWN {name}: {unknown}")
                for excluded in result.get("not_audited", []):
                    typer.echo(f"NOT AUDITED: {excluded}")
        raise typer.Exit(policy.exit_code(report))
