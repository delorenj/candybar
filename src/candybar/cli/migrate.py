"""`candybar migrate`: the Epic 7 cron-migration CLI (Story 7.5, AD-21).

Thin typer wiring over `candybar.migration.ops`. A REST client of the api
role (CANDYBAR_API_URL, default http://127.0.0.1:8700) that never writes the
registry itself; the ledger is `deploy/migration/ledger.yaml` (or
CANDYBAR_LEDGER / --ledger).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

import typer

LedgerOption = Annotated[
    Path | None,
    typer.Option(
        "--ledger",
        help="Ledger file (default: $CANDYBAR_LEDGER, else the checkout's deploy/migration/ledger.yaml).",
    ),
]

migrate_app = typer.Typer(
    name="migrate",
    help=(
        "Move a job from its native scheduler (systemd timer, crontab line, Hermes ticker) onto "
        "candybar, reversibly, through the committed migration ledger; or retire it for good "
        "(`decommission`). Exit codes: 0 ok, "
        "1 refused/failed, 2 invalid ledger or unknown id, 3 pending."
    ),
    no_args_is_help=True,
)


def _host():
    from candybar.migration.client import CandybarApi
    from candybar.migration.native import read_file, run_shell
    from candybar.migration.ops import Host

    return Host(
        api=CandybarApi(),
        shell=run_shell,
        read=read_file,
        sleep=time.sleep,
        echo=typer.echo,
    )


def _ledger(path: Path | None) -> Path:
    from candybar.migration.ledger import default_ledger_path

    return path.expanduser() if path is not None else default_ledger_path()


@migrate_app.command("plan")
def plan_cmd(
    ids: Annotated[
        list[str] | None,
        typer.Argument(help="Inventory ids (default: every planned entry)."),
    ] = None,
    wave: Annotated[
        str | None, typer.Option("--wave", help="Only entries of this wave, e.g. 7.7.")
    ] = None,
    write: Annotated[
        bool,
        typer.Option(
            "--write",
            help="Fill default disable/rollback commands and unit files into the ledger.",
        ),
    ] = False,
    ledger: LedgerOption = None,
) -> None:
    """Validate entries, probe their native schedulers and print what a cutover would do."""
    from candybar.migration import ops

    raise typer.Exit(
        ops.plan(_host(), _ledger(ledger), list(ids or []), wave=wave, write=write)
    )


@migrate_app.command("cutover")
def cutover_cmd(
    job_id: Annotated[
        str, typer.Argument(metavar="ID", help="Inventory id of the ledger entry.")
    ],
    wait: Annotated[
        float,
        typer.Option(
            "--wait",
            help="Seconds to wait for the cutover guard window to open (default 0).",
        ),
    ] = 0,
    ledger: LedgerOption = None,
) -> None:
    """Register the candybar disabled, retire the native scheduler, enable the candybar."""
    from candybar.migration import ops

    raise typer.Exit(ops.cutover(_host(), _ledger(ledger), job_id, wait_s=wait))


@migrate_app.command("verify")
def verify_cmd(
    job_id: Annotated[
        str, typer.Argument(metavar="ID", help="Inventory id of the ledger entry.")
    ],
    ledger: LedgerOption = None,
) -> None:
    """Mark the entry verified by its first scheduled run since cutover (exit 3 while none)."""
    from candybar.migration import ops

    raise typer.Exit(ops.verify(_host(), _ledger(ledger), job_id))


@migrate_app.command("rollback")
def rollback_cmd(
    job_id: Annotated[
        str, typer.Argument(metavar="ID", help="Inventory id of the ledger entry.")
    ],
    reason: Annotated[
        str, typer.Option("--reason", help="Why, recorded in the entry's history.")
    ] = "",
    ledger: LedgerOption = None,
) -> None:
    """Restore the native scheduler first, then disable the candybar (best effort; works with the daemon down)."""
    from candybar.migration import ops

    raise typer.Exit(ops.rollback(_host(), _ledger(ledger), job_id, reason=reason))


@migrate_app.command("decommission")
def decommission_cmd(
    job_id: Annotated[
        str, typer.Argument(metavar="ID", help="Inventory id of the ledger entry.")
    ],
    reason: Annotated[
        str,
        typer.Option("--reason", help="Why the job is gone for good, recorded in the entry's history."),
    ],
    ledger: LedgerOption = None,
) -> None:
    """Retire a job for good: native scheduler retired, candybar disabled, `rollback` refused afterwards."""
    from candybar.migration import ops

    raise typer.Exit(ops.decommission(_host(), _ledger(ledger), job_id, reason=reason))


@migrate_app.command("status")
def status_cmd(
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
    probe_native: Annotated[
        bool, typer.Option("--probe", help="Also probe each native scheduler.")
    ] = False,
    wave: Annotated[
        str | None, typer.Option("--wave", help="Only entries of this wave.")
    ] = None,
    ledger: LedgerOption = None,
) -> None:
    """One line per ledger entry: status, wave, candybar and its enabled flag."""
    from candybar.migration import ops

    raise typer.Exit(
        ops.status(
            _host(),
            _ledger(ledger),
            as_json=as_json,
            probe_native=probe_native,
            wave=wave,
        )
    )
