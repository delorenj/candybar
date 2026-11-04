"""`candybar steward ...` and `candybar profile` (steward-contract.md sections 2-3).

- `candybar steward enrich|fix|evaluate <name> [--wait]`: queue one steward
  task (POST /candybars/{id}/steward); with --wait, follow it to the end and
  print its outcome (exit 1 when it failed, 3 when --timeout passes first).
  A 409 (a task of that action is already active) follows the active one.
- `candybar steward tasks [--active] [--candybar NAME]`: the task list.
- `candybar profile <name>`: the stored overview, project and evaluation.

Thin wrappers over the api role through the same REST client as the rest of
the CLI; the steward role on the host does the work.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

import typer

from candybar.cli.control import (
    RefArgument,
    _api,
    _fail,
    _print_json,
    _resolve,
    _table,
    _when,
)
from candybar.migration.client import ApiError, CandybarApi

steward_app = typer.Typer(name="steward", help="Enrich, fix and evaluate candybars through the steward.")

WaitOption = Annotated[bool, typer.Option("--wait", "-w", help="Follow the task to the end.")]
TimeoutOption = Annotated[
    float, typer.Option("--timeout", help="Seconds --wait gives up after (the task keeps going).")
]
JsonOption = Annotated[bool, typer.Option("--json", help="Print the raw API JSON.")]
TERMINAL = ("succeeded", "failed")


def _now() -> datetime:
    return datetime.now(UTC)


def wait_for_task(
    api: CandybarApi,
    candybar_id: str,
    task_id: str,
    timeout_s: float,
    *,
    on_change: Callable[[dict[str, Any]], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any] | None:
    """Poll the candybar's task list until `task_id` is terminal; None on timeout."""
    deadline = clock() + timeout_s
    delay = 2.0
    seen: str | None = None
    while True:
        try:
            tasks = api.candybar_tasks(candybar_id, limit=200)
        except ApiError as exc:
            if not (exc.unreachable or (exc.status or 0) >= 500):
                raise
            tasks = []
        task = next((t for t in tasks if t["id"] == task_id), None)
        if task is not None and task["status"] != seen:
            seen = task["status"]
            if on_change is not None:
                on_change(task)
        if task is not None and task["status"] in TERMINAL:
            return task
        if clock() >= deadline:
            return None
        sleep(delay)
        delay = min(delay * 1.5, 10.0)


def print_task(task: dict[str, Any]) -> None:
    lines = [
        ("task", f"{task['id']} ({task['action']}, {task['model']})"),
        ("status", task["status"]),
        ("outcome", task.get("outcome") or "-"),
        ("summary", task.get("summary") or "-"),
    ]
    if task.get("error"):
        lines.append(("error", task["error"]))
    if task.get("sessionId"):
        lines.append(("session", task["sessionId"]))
    if task.get("reportKey"):
        lines.append(("report", f"delo/daily-reports/{task['reportKey']}"))
    if task.get("invocationId"):
        lines.append(("proving run", task["invocationId"]))
    if task.get("parentTaskId"):
        lines.append(("parent", task["parentTaskId"]))
    width = max(len(k) for k, _ in lines)
    for key, value in lines:
        typer.echo(f"{key.rjust(width)}  {value}")


def _request(action: str, ref: str, wait: bool, timeout: float, as_json: bool) -> None:
    api = _api()
    candybar = _resolve(api, ref)
    try:
        task = api.steward(candybar["id"], action)
        typer.echo(f"queued {action} of {candybar['name']}: task {task['id']} ({task['model']})", err=as_json)
    except ApiError as exc:
        active = (exc.problem or {}).get("taskId") if exc.status == 409 else None
        if not (wait and active):
            raise _fail(exc) from exc
        typer.echo(f"{action} of {candybar['name']} already active: following task {active}", err=True)
        task = {"id": active}
    if not wait:
        if as_json:
            _print_json(task)
        return

    def progress(row: dict[str, Any]) -> None:
        typer.echo(f"  {row['status']}" + (f" (session {row['sessionId']})" if row.get("sessionId") else ""), err=True)

    try:
        final = wait_for_task(api, candybar["id"], task["id"], timeout, on_change=progress)
    except ApiError as exc:
        raise _fail(exc) from exc
    if final is None:
        typer.echo(f"still running after {timeout:g}s; check `candybar steward tasks --active`", err=True)
        raise typer.Exit(code=3)
    if as_json:
        _print_json(final)
    else:
        print_task(final)
    if final["status"] != "succeeded":
        raise typer.Exit(code=1)


@steward_app.command("enrich")
def enrich(ref: RefArgument, wait: WaitOption = False, timeout: TimeoutOption = 1800, as_json: JsonOption = False) -> None:
    """Write the candybar's overview and classify its project."""
    _request("enrich", ref, wait, timeout, as_json)


@steward_app.command("fix")
def fix(ref: RefArgument, wait: WaitOption = False, timeout: TimeoutOption = 5400, as_json: JsonOption = False) -> None:
    """Repair a drifted candybar, commit and push, and prove it with one manual run."""
    _request("fix", ref, wait, timeout, as_json)


@steward_app.command("evaluate")
def evaluate(ref: RefArgument, wait: WaitOption = False, timeout: TimeoutOption = 2400, as_json: JsonOption = False) -> None:
    """Evaluate a candybar (Nominal/Suboptimal/Drifted/Orphaned) and run the follow-up."""
    _request("evaluate", ref, wait, timeout, as_json)


@steward_app.command("tasks")
def tasks(
    active: Annotated[bool, typer.Option("--active", help="Only queued and running tasks.")] = False,
    ref: Annotated[
        str | None, typer.Option("--candybar", "-c", metavar="NAME|ID", help="Only this candybar.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=500, help="How many.")] = 30,
    as_json: JsonOption = False,
) -> None:
    """Steward tasks, newest first."""
    api = _api()
    try:
        names = {c["id"]: c["name"] for c in api.list_candybars()}
        if ref is not None:
            candybar = _resolve(api, ref)
            rows = api.candybar_tasks(candybar["id"], limit=min(limit, 200))
            if active:
                rows = [t for t in rows if t["status"] not in TERMINAL]
        else:
            rows = api.tasks(active=active, limit=limit)
    except ApiError as exc:
        raise _fail(exc) from exc
    if as_json:
        _print_json(rows)
        return
    if not rows:
        typer.echo("no active steward tasks" if active else "no steward tasks")
        return
    now = _now()
    table = [
        [
            _when(t["createdAt"], now),
            names.get(t["candybarId"], t["candybarId"]),
            t["action"],
            t["status"],
            t.get("outcome") or "-",
            ((t.get("error") or t.get("summary") or "").splitlines() or [""])[0][:70],
            t["id"],
        ]
        for t in rows
    ]
    _table(["CREATED", "CANDYBAR", "ACTION", "STATUS", "OUTCOME", "NOTE", "ID"], table)


def profile(ref: RefArgument, as_json: JsonOption = False) -> None:
    """A candybar's steward profile: overview, project and latest evaluation."""
    api = _api()
    candybar = _resolve(api, ref)
    try:
        body = api.profile(candybar["id"])
    except ApiError as exc:
        raise _fail(exc) from exc
    if as_json:
        _print_json(body)
        return
    now = _now()
    typer.echo(f"{candybar['name']}  (profile updated {_when(body.get('updatedAt'), now)})")
    project = body.get("project")
    typer.echo(
        "project: "
        + (f"{project['name']} ({project['id']}, {project['method']}, {project['confidence']:.2f})" if project else "-")
    )
    overview = body.get("overview")
    if overview:
        typer.echo(f"\noverview (template v{body.get('templateVersion')}, {_when(body.get('enrichedAt'), now)}):")
        typer.echo(f"  {overview['summary']}")
        typer.echo(f"  {overview['purpose']}")
        for key in ("behavior", "inputs", "outputs", "sideEffects", "dependencies", "failureModes", "verification", "runbook"):
            items = overview.get(key) or []
            if items:
                typer.echo(f"  {key}:")
                for item in items:
                    typer.echo(f"    - {item}")
        typer.echo(f"  source: {json.dumps(overview.get('source'))}")
        typer.echo(f"  cadence: {overview.get('cadenceNote')}")
    else:
        typer.echo("overview: - (run `candybar steward enrich " + candybar["name"] + "`)")
    evaluation = body.get("evaluation")
    if evaluation:
        typer.echo(f"\nevaluation: {evaluation['outcome']} ({_when(evaluation.get('evaluatedAt'), now)})")
        typer.echo(f"  {evaluation['summary']}")
        for axis, entry in (evaluation.get("axes") or {}).items():
            typer.echo(f"  {axis:<12} {entry['rating']:<5} {entry['finding']}")
        typer.echo(f"  recommendation: {evaluation['recommendation']}")
        typer.echo(f"  follow-up: {evaluation['followUp']}; report: delo/daily-reports/{evaluation['reportKey']}")
    else:
        typer.echo("evaluation: -")


def install(app: typer.Typer) -> None:
    app.add_typer(steward_app, name="steward")
    app.command("profile")(profile)
