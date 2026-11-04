"""The operator control surface: `ls`, `show`, `next`, `schedule`, `pause`,
`resume`, `archive`, `unarchive`, `skip`, `edit`, `history`, `tail`.

Thin wrappers over the api role (CANDYBAR_API_URL, default
http://127.0.0.1:8700) through the same REST client `candybar migrate` uses,
so every write publishes its `bloodbank.cron.candybar.*` fact like any other
API call. A candybar is addressed by registry id or by name everywhere.
Exit codes: 0 ok, 1 the API refused or is unreachable, 2 unknown candybar or
bad arguments.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo

import typer

from candybar.dispatcher.firetimes import fire_times
from candybar.migration.client import INVOCATIONS_PAGE, ApiError, CandybarApi
from candybar.project import (
    CandybarDef,
    LoadResult,
    Project,
    drift,
    find_project,
    load_path,
    load_project,
    project_at,
    project_of_tags,
)

JsonOption = Annotated[bool, typer.Option("--json", help="Print the raw API JSON.")]
RefArgument = Annotated[str, typer.Argument(metavar="NAME|ID", help="Candybar name or registry id.")]

# How far `skip` (no flags) looks for the candybar's next unskipped fire.
NEXT_FIRE_HORIZON = timedelta(days=366)
STREAM_RETRY_S = 3.0
# A past fire with no finished run reads "pending" (not "no-run") this long.
PENDING_GRACE = timedelta(minutes=15)
# `skip` checks at most this many upcoming fires for an unskipped one.
NEXT_FIRE_CANDIDATES = 50


def _now() -> datetime:
    return datetime.now(UTC)


def _api() -> CandybarApi:
    return CandybarApi()


def _fail(exc: ApiError) -> typer.Exit:
    typer.echo(str(exc), err=True)
    return typer.Exit(code=1)


def _print_json(body: Any) -> None:
    typer.echo(json.dumps(body, indent=2))


def _resolve(api: CandybarApi, ref: str) -> dict[str, Any]:
    """The candybar definition `ref` names: an id, else an exact name."""
    try:
        UUID(ref)
    except ValueError:
        pass
    else:
        try:
            found = api.get_candybar(ref)
        except ApiError as exc:
            raise _fail(exc) from exc
        if found is None:
            typer.echo(f"no candybar with id {ref}", err=True)
            raise typer.Exit(code=2)
        return found
    try:
        candybars = api.list_candybars()
    except ApiError as exc:
        raise _fail(exc) from exc
    for candybar in candybars:
        if candybar["name"] == ref:
            return candybar
    near = sorted(c["name"] for c in candybars if ref in c["name"])
    hint = f" (did you mean: {', '.join(near[:8])})" if near else ""
    typer.echo(f"no candybar named {ref!r}{hint}", err=True)
    raise typer.Exit(code=2)


def resolve_id(api: CandybarApi, ref: str) -> str:
    return _resolve(api, ref)["id"]


# --- formatting -------------------------------------------------------------


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _utc_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _relative(moment: datetime, now: datetime) -> str:
    seconds = int((moment - now).total_seconds())
    span = abs(seconds)
    if span < 60:
        text = f"{span}s"
    elif span < 3600:
        text = f"{span // 60}m"
    elif span < 86400:
        text = f"{span // 3600}h{(span % 3600) // 60:02d}m"
    else:
        text = f"{span // 86400}d{(span % 86400) // 3600}h"
    return f"in {text}" if seconds >= 0 else f"{text} ago"


def _when(value: str | None, now: datetime) -> str:
    """A UTC API timestamp as this machine's local time plus a relative hint."""
    if not value:
        return "-"
    moment = _parse_ts(value)
    return f"{moment.astimezone().strftime('%a %m-%d %H:%M')} ({_relative(moment, now)})"


def _bytes(count: int | None) -> str:
    size = float(count or 0)
    for unit in ("B", "K", "M", "G"):
        if size < 1024 or unit == "G":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}G"  # pragma: no cover - the loop always returns


def _duration(ms: int | None) -> str:
    if ms is None:
        return "-"
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms // 60_000}m{(ms % 60_000) // 1000:02d}s"


def _table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(cell)) for w, cell in zip(widths, row, strict=True)]
    for row in [headers, *rows]:
        typer.echo("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())


def _flags(candybar: dict[str, Any]) -> str:
    if candybar.get("archived"):
        flags = ["archived"]
    else:
        flags = [] if candybar.get("enabled", True) else ["paused"]
    if candybar.get("noise"):
        flags.append("noise")
    return ",".join(flags) or "-"


def _row_state(row: dict[str, Any], now: datetime) -> str:
    if row.get("skipped"):
        return "skipped"
    if row["state"] == "past":
        if row.get("lastResult"):
            return row["lastResult"]
        # lastResult only reflects finished runs: a recent fire may still be running.
        recent = now - _parse_ts(row["fireTime"]) < PENDING_GRACE
        return "pending" if recent else "no-run"
    return row["state"]


# --- reads ------------------------------------------------------------------


def ls(
    all_: Annotated[
        bool, typer.Option("--all", "-a", help="Include paused candybars.")
    ] = False,
    archived: Annotated[
        bool, typer.Option("--archived", help="Include archived candybars (hidden otherwise).")
    ] = False,
    tag: Annotated[str | None, typer.Option("--tag", "-t", help="Only candybars with this tag.")] = None,
    hide_noise: Annotated[
        bool, typer.Option("--hide-noise", help="Leave out candybars in the noise collection.")
    ] = False,
    global_: Annotated[
        bool,
        typer.Option("--global", "-g", help="List the whole registry even inside a project."),
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """List candybars: the project's own inside a project, else the whole registry.

    Inside a project (a git repo holding .candybar.toml or .candybars/*.candybar.toml,
    or one the registry already has candybars for) the list is that project's
    candybars with their sync state; --global lists the registry instead.
    Registry mode shows active candybars unless --all; archived ones only with --archived.
    """
    api = _api()
    project = None if global_ else find_project()
    has_files = project is not None and bool(project.files())
    failure: ApiError | None = None
    rows: list[dict[str, Any]] | None
    try:
        rows = api.list_candybars()
    except ApiError as exc:
        rows, failure = None, exc
    project_mode = project is not None and (
        has_files or (rows is not None and any(project.tag in r.get("tags", []) for r in rows))
    )
    if project_mode:
        assert project is not None
        _ls_project(api, project, rows, failure, archived=archived, tag=tag,
                    hide_noise=hide_noise, as_json=as_json)
        return
    if rows is None:
        assert failure is not None
        raise _fail(failure) from failure
    _ls_registry(rows, all_=all_, archived=archived, tag=tag, hide_noise=hide_noise, as_json=as_json)


def _ls_registry(
    candybars: list[dict[str, Any]],
    *,
    all_: bool,
    archived: bool,
    tag: str | None,
    hide_noise: bool,
    as_json: bool,
) -> None:
    stored = sum(1 for c in candybars if c.get("archived"))
    paused = sum(1 for c in candybars if not c.get("enabled", True) and not c.get("archived"))
    shown = [
        c
        for c in candybars
        if (c.get("archived") and archived or not c.get("archived") and (all_ or c.get("enabled", True)))
        and (tag is None or tag in c.get("tags", []))
        and not (hide_noise and c.get("noise"))
    ]
    if as_json:
        _print_json(shown)
        return
    rows = [
        [c["name"], c["schedule"], c.get("timezone", "UTC"), _flags(c), ",".join(c.get("tags", [])), c["id"]]
        for c in sorted(shown, key=lambda c: c["name"])
    ]
    _table(["NAME", "SCHEDULE", "TZ", "FLAGS", "TAGS", "ID"], rows)
    footer = f"{len(shown)} shown of {len(candybars)}"
    hidden = []
    if paused and not all_:
        hidden.append(f"{paused} paused hidden; --all to include")
    if stored and not archived:
        hidden.append(f"{stored} archived hidden; --archived to include")
    if hidden:
        footer += f" ({'; '.join(hidden)})"
    typer.echo(footer)


def _registry_state(row: dict[str, Any]) -> str:
    if row.get("archived"):
        return "archived"
    return "enabled" if row.get("enabled", True) else "paused"


def _source_label(project: Project, definition: CandybarDef) -> tuple[str, str]:
    try:
        where = str(definition.source.relative_to(project.root))
    except ValueError:
        where = str(definition.source)
    label = where if definition.table == "[candybar]" else f"{where} {definition.table}"
    return label, definition.table


def _ls_project(
    api: CandybarApi,
    project: Project,
    rows: list[dict[str, Any]] | None,
    failure: ApiError | None,
    *,
    archived: bool,
    tag: str | None,
    hide_noise: bool,
    as_json: bool,
) -> None:
    """`ls` inside a project: its local definitions against the registry."""
    loaded = load_project(project)
    for error in loaded.errors:
        typer.echo(f"warning: {error}", err=True)
    if failure is not None:
        typer.echo(f"warning: {failure}; showing local definitions only", err=True)
    by_name = {r["name"]: r for r in rows or []}
    entries: list[dict[str, Any]] = []
    for d in sorted(loaded.defs, key=lambda d: d.name):
        manifest = d.manifest
        if (tag is not None and tag not in manifest.tags) or (hide_noise and manifest.noise):
            continue
        row = by_name.get(d.name)
        fields: list[str] = []
        if rows is None:
            state, sync = "?", "?"
        elif row is None:
            state, sync = "unregistered", "new"
        else:
            state = _registry_state(row)
            owner = project_of_tags(row.get("tags", []))
            if owner is None:
                sync = "unowned"
            elif owner != project.name:
                sync = f"conflict:{owner}"
            else:
                try:
                    fields = list(drift(manifest, {**row, **api.runner(row["id"])}))
                except ApiError as exc:
                    typer.echo(f"warning: {d.name}: {exc}", err=True)
                    sync = "?"
                else:
                    sync = f"drift:{','.join(fields)}" if fields else "ok"
        source, table = _source_label(project, d)
        entries.append(
            {"name": d.name, "state": state, "sync": sync, "drift": fields, "source": source,
             "table": table, "definition": manifest.model_dump(mode="json"), "registered": row,
             "schedule": manifest.schedule, "tz": manifest.timezone}
        )
    local_names = {d.name for d in loaded.defs}
    for row in sorted(rows or [], key=lambda r: r["name"]):
        if project.tag not in row.get("tags", []) or row["name"] in local_names:
            continue
        if (row.get("archived") and not archived) or (tag is not None and tag not in row.get("tags", [])):
            continue
        if hide_noise and row.get("noise"):
            continue
        entries.append(
            {"name": row["name"], "state": _registry_state(row), "sync": "orphan", "drift": [],
             "source": "-", "table": None, "definition": None, "registered": row,
             "schedule": row["schedule"], "tz": row.get("timezone", "UTC")}
        )
    entries.sort(key=lambda e: e["name"])
    if as_json:
        keys = ("name", "state", "sync", "drift", "source", "table", "definition", "registered")
        _print_json([{k: e[k] for k in keys} for e in entries])
    else:
        _table(
            ["NAME", "SCHEDULE", "TZ", "STATE", "SYNC", "SOURCE", "ID"],
            [
                [e["name"], e["schedule"], e["tz"], e["state"], e["sync"], e["source"],
                 (e["registered"] or {}).get("id", "-")]
                for e in entries
            ],
        )
        typer.echo(_project_footer(project, entries))
    if failure is not None:
        raise typer.Exit(code=1)


def _project_footer(project: Project, entries: list[dict[str, Any]]) -> str:
    count = len(entries)
    footer = f"{count} candybar{'' if count == 1 else 's'} in project {project.name} ({project.root})"
    syncs = [e["sync"] for e in entries]
    behind = sum(1 for s in syncs if s == "new" or s.startswith("drift:"))
    hints = []
    if behind:
        hints.append(f"{behind} unregistered/drifted; `candybar register` syncs them")
    if any(s.startswith("drift:") for s in syncs):
        hints.append("`--update` applies drift")
    if (orphans := sum(1 for e in entries if e["sync"] == "orphan" and e["state"] != "archived")):
        hints.append(f"{orphans} orphaned; `candybar register --prune` archives them")
    if (unowned := syncs.count("unowned")):
        hints.append(f"{unowned} not owned by a project; `candybar register --update` adopts them")
    return footer + (f" ({'; '.join(hints)})" if hints else "")


# --- register ---------------------------------------------------------------


def _project_of_path(path: Path) -> tuple[Project | None, bool]:
    """(project, whole) for an explicit `register PATH`: whole = the path is the project itself."""
    if path.is_dir():
        if (path / "candybar.toml").is_file():
            return None, False
        return project_at(path), True
    found = find_project(path.parent)
    return (found, False) if found is not None and found.owns(path) else (None, False)


def register_candybars(
    path: Path | None, names: list[str], *, update: bool, prune: bool, dry_run: bool
) -> None:
    """`candybar register`: a manifest (create-only) or a project's candybars (sync)."""
    from candybar.registry import ManifestError

    project: Project | None
    whole = True
    if path is None:
        project = find_project()
        if project is None:
            typer.echo("not inside a project; pass a path", err=True)
            raise typer.Exit(code=2)
        if not project.files() and not prune:
            typer.echo(
                f"no candybars in project {project.name}: add a .candybar.toml or "
                ".candybars/<name>.candybar.toml (`candybar create` writes one)",
                err=True,
            )
            raise typer.Exit(code=2)
        loaded = load_project(project)
    else:
        try:
            loaded = load_path(path)
        except ManifestError as exc:
            typer.echo(f"invalid manifest: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        project, whole = _project_of_path(path)
    if loaded.errors:
        for error in loaded.errors:
            typer.echo(f"invalid manifest: {error}", err=True)
        raise typer.Exit(code=2)
    if project is None and (update or prune):
        typer.echo("--update and --prune apply to project candybar files only", err=True)
        raise typer.Exit(code=2)
    if prune and names:
        typer.echo("--prune needs the whole project: drop --name", err=True)
        raise typer.Exit(code=2)
    if prune and not whole:
        typer.echo("--prune needs the whole project: run it without a file path", err=True)
        raise typer.Exit(code=2)
    wanted = list(dict.fromkeys(names))
    known = {d.name for d in loaded.defs}
    if unknown := [n for n in wanted if n not in known]:
        typer.echo(
            f"no candybar named {', '.join(map(repr, unknown))} (defined: {', '.join(sorted(known))})",
            err=True,
        )
        raise typer.Exit(code=2)
    defs = [d for d in loaded.defs if not wanted or d.name in wanted]
    if project is None:
        _register_plain(defs, dry_run=dry_run)
    else:
        _register_project(
            project, defs, loaded, update=update, prune=prune, dry_run=dry_run,
            sweep_orphans=whole and not wanted,
        )


def _register_plain(defs: list[CandybarDef], *, dry_run: bool) -> None:
    """Create-only registration of candybars that belong to no project."""
    api = _api()
    failed = False
    for d in defs:
        if dry_run:
            typer.echo(f"would register {d.name}")
            continue
        try:
            body = api.register(d.manifest.model_dump(mode="json"))
        except ApiError as exc:
            # The API's problem+json body explains the rejection.
            typer.echo(json.dumps(exc.problem) if exc.problem else str(exc), err=True)
            failed = True
            if exc.unreachable:
                break
            continue
        typer.echo(f"registered {body['name']} (id {body['id']})")
    if failed:
        raise typer.Exit(code=1)


SUMMARY_ORDER = (
    "registered", "updated", "adopted", "unchanged", "drifted", "exists", "archived",
    "orphans", "pruned", "conflicts", "errors",
)


def _register_project(
    project: Project,
    defs: list[CandybarDef],
    loaded: LoadResult,
    *,
    update: bool,
    prune: bool,
    dry_run: bool,
    sweep_orphans: bool,
) -> None:
    api = _api()
    try:
        rows = api.list_candybars()
    except ApiError as exc:
        raise _fail(exc) from exc
    by_name = {r["name"]: r for r in rows}
    counts: Counter[str] = Counter()
    would = "would " if dry_run else ""
    failed = aborted = False
    for d in defs:
        name, row = d.name, by_name.get(d.name)
        try:
            if row is None:
                if dry_run:
                    typer.echo(f"would register {name}")
                else:
                    body = api.register(d.manifest.model_dump(mode="json"))
                    typer.echo(f"registered {name} (id {body['id']})")
                counts["registered"] += 1
                continue
            owner = project_of_tags(row.get("tags", []))
            if owner is not None and owner != project.name:
                typer.echo(f"conflict {name}: registered by project {owner}", err=True)
                counts["conflicts"] += 1
                failed = True
                continue
            if row.get("archived"):
                typer.echo(f"archived {name}; `candybar unarchive {name}` first")
                counts["archived"] += 1
                continue
            changes = drift(d.manifest, {**row, **api.runner(row["id"])})
            fields = ", ".join(changes)
            if owner is None:
                if not update or not changes:
                    typer.echo(f"exists {name} (not owned by a project; --update adopts it)")
                    counts["exists"] += 1
                    continue
                if not dry_run:
                    api.update(row["id"], changes)
                typer.echo(f"{would}{'adopt' if dry_run else 'adopted'} {name}")
                counts["adopted"] += 1
            elif not changes:
                typer.echo(f"unchanged {name}")
                counts["unchanged"] += 1
            elif not update:
                typer.echo(f"drifted {name}: {fields} (rerun with --update)")
                counts["drifted"] += 1
            else:
                if not dry_run:
                    api.update(row["id"], changes)
                typer.echo(f"{would}{'update' if dry_run else 'updated'} {name}: {fields}")
                counts["updated"] += 1
        except ApiError as exc:
            typer.echo(f"error {name}: {exc}", err=True)
            counts["errors"] += 1
            failed = True
            if exc.unreachable:
                aborted = True
                break
    if sweep_orphans and not aborted:
        local = {d.name for d in loaded.defs}
        for row in sorted(rows, key=lambda r: r["name"]):
            if project.tag not in row.get("tags", []) or row.get("archived") or row["name"] in local:
                continue
            if not prune:
                typer.echo(
                    f"orphan {row['name']} (registered, no local definition; --prune archives it)"
                )
                counts["orphans"] += 1
                continue
            try:
                if not dry_run:
                    api.set_archived(row["id"], True)
            except ApiError as exc:
                typer.echo(f"error {row['name']}: {exc}", err=True)
                counts["errors"] += 1
                failed = True
                if exc.unreachable:
                    break
                continue
            typer.echo(f"{would}{'archive' if dry_run else 'archived'} orphan {row['name']}")
            counts["pruned"] += 1
    parts = [f"{counts[k]} {k}" for k in SUMMARY_ORDER if counts[k]]
    suffix = " (dry run)" if dry_run else ""
    typer.echo(f"project {project.name}{suffix}: {', '.join(parts) or 'nothing to do'}")
    if failed:
        raise typer.Exit(code=1)


def show(
    ref: RefArgument,
    limit: Annotated[int, typer.Option("--runs", "-n", min=0, help="Recent runs to list.")] = 5,
    as_json: JsonOption = False,
) -> None:
    """Show one candybar: definition, runner, totals and its latest runs."""
    api = _api()
    candybar = _resolve(api, ref)
    try:
        detail = api.get_candybar(candybar["id"]) or candybar
        runner = api.runner(candybar["id"])
        runs = api.invocations(candybar["id"], limit) if limit else []
    except ApiError as exc:
        raise _fail(exc) from exc
    if as_json:
        _print_json({**detail, **runner, "invocations": runs})
        return
    now = _now()
    total = detail.get("totalInvoked", 0)
    ok = detail.get("totalSuccessful", 0)
    lines = [
        ("name", detail["name"]),
        ("id", detail["id"]),
        ("description", detail.get("description") or "-"),
        ("schedule", f"{detail['schedule']}  ({detail.get('timezone', 'UTC')})"),
        (
            "state",
            "archived"
            if detail.get("archived")
            else "paused" if not detail.get("enabled", True) else "enabled",
        ),
        ("noise", "yes" if detail.get("noise") else "no"),
        ("tags", ", ".join(detail.get("tags", [])) or "-"),
        ("runs", f"{total} invoked, {ok} succeeded, {total - ok} failed"),
        ("created", _when(detail.get("createdAt"), now)),
    ]
    if runner.get("runner"):
        lines.append(("runner", json.dumps(runner["runner"])))
    else:
        lines.append(("entrypoint", runner.get("entrypoint") or "-"))
    width = max(len(key) for key, _ in lines)
    for key, value in lines:
        typer.echo(f"{key.rjust(width)}  {value}")
    if runs:
        typer.echo("")
        _runs_table(runs, now)


def next_(as_json: JsonOption = False) -> None:
    """The single next fire across all candybars."""
    try:
        row = _api().schedule_next()
    except ApiError as exc:
        raise _fail(exc) from exc
    if row is None:
        typer.echo("nothing is scheduled to fire", err=True)
        raise typer.Exit(code=1)
    if as_json:
        _print_json(row)
        return
    noise = "  [noise]" if row.get("noise") else ""
    typer.echo(f"{row['name']}  {_when(row['fireTime'], _now())}  {row['schedule']}{noise}")


def schedule(
    ahead: Annotated[float, typer.Option("--ahead", help="Hours after now to include.")] = 12,
    back: Annotated[float, typer.Option("--back", help="Hours before now to include.")] = 0,
    ref: Annotated[
        str | None, typer.Option("--candybar", "-c", metavar="NAME|ID", help="Only this candybar.")
    ] = None,
    hide_noise: Annotated[
        bool, typer.Option("--hide-noise", help="Leave out noise candybars (e.g. minutely smoke jobs).")
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Every fire in a window around now: past results, current, upcoming, skipped."""
    if ahead < 0 or back < 0 or ahead + back <= 0:
        typer.echo("--ahead/--back must be >= 0 and span a non-empty window", err=True)
        raise typer.Exit(code=2)
    api = _api()
    only = _resolve(api, ref)["id"] if ref else None
    now = _now()
    start = (now - timedelta(hours=back)).replace(second=0, microsecond=0)
    end = now + timedelta(hours=ahead)
    try:
        rows = api.schedule(_utc_z(start), _utc_z(end))
    except ApiError as exc:
        raise _fail(exc) from exc
    rows = [
        r
        for r in rows
        if (only is None or r["candybarId"] == only) and not (hide_noise and r.get("noise"))
    ]
    if as_json:
        _print_json(rows)
        return
    table = []
    for row in rows:
        marker = ">" if row["state"] == "current" else " "
        table.append([marker, _when(row["fireTime"], now), row["name"], _row_state(row, now), row["schedule"]])
    _table([" ", "FIRE", "NAME", "STATE", "SCHEDULE"], table)
    typer.echo(f"{len(rows)} fires")


def history(
    ref: RefArgument,
    limit: Annotated[int, typer.Option("--runs", "-n", min=1, help="How many runs.")] = 20,
    failed: Annotated[bool, typer.Option("--failed", help="Only failed runs.")] = False,
    as_json: JsonOption = False,
) -> None:
    """A candybar's recorded runs, newest first, with their measures."""
    api = _api()
    candybar = _resolve(api, ref)
    try:
        # --failed filters client-side, so read the API's whole page first.
        runs = api.invocations(candybar["id"], INVOCATIONS_PAGE if failed else limit)
    except ApiError as exc:
        raise _fail(exc) from exc
    if failed:
        runs = [r for r in runs if r.get("result") == "fail"][:limit]
    if as_json:
        _print_json(runs)
        return
    if not runs:
        typer.echo(f"no {'failed' if failed else 'recorded'} runs")
        return
    _runs_table(runs, _now())


def _runs_table(runs: list[dict[str, Any]], now: datetime) -> None:
    rows = []
    for run in runs:
        result = run.get("result") or "running"  # the row exists from claim time
        note = run.get("errorMessage") or ""
        if result == "fail" and run.get("exitCode") not in (None, 0):
            note = f"exit {run['exitCode']}" + (f": {note}" if note else "")
        rows.append(
            [
                _when(run["startedAt"], now),
                run.get("trigger") or "-",
                result,
                _duration(run.get("runtimeMs")),
                _bytes(run.get("memoryPeakBytes")),
                f"{run.get('cpuLoad') or 0:.2f}",
                str(run.get("tokenSpend") or 0),
                note.splitlines()[0][:80] if note else "",
            ]
        )
    _table(["STARTED", "TRIGGER", "RESULT", "RUNTIME", "MEM", "CPU", "TOKENS", "NOTE"], rows)


# --- writes -----------------------------------------------------------------


def _set_enabled(ref: str, enabled: bool) -> None:
    api = _api()
    candybar = _resolve(api, ref)
    verb = "resumed" if enabled else "paused"
    if candybar.get("enabled", True) == enabled:
        typer.echo(f"{candybar['name']} already {verb}")
        return
    try:
        api.set_enabled(candybar["id"], enabled)
    except ApiError as exc:
        raise _fail(exc) from exc
    typer.echo(f"{verb} {candybar['name']}")


def _set_archived(ref: str, archived: bool) -> None:
    api = _api()
    candybar = _resolve(api, ref)
    verb = "archived" if archived else "unarchived"
    if bool(candybar.get("archived")) == archived:
        typer.echo(f"{candybar['name']} already {verb}")
        return
    try:
        api.set_archived(candybar["id"], archived)
    except ApiError as exc:
        raise _fail(exc) from exc
    if archived:
        typer.echo(f"archived {candybar['name']} (also paused; `candybar unarchive` reverses it)")
    else:
        typer.echo(f"unarchived {candybar['name']} (still paused: `candybar resume {candybar['name']}`)")


def archive(ref: RefArgument) -> None:
    """Archive a candybar: paused and hidden from `ls` (and Holocene) until unarchived."""
    _set_archived(ref, True)


def unarchive(ref: RefArgument) -> None:
    """Unarchive a candybar. It stays paused until `candybar resume`."""
    _set_archived(ref, False)


def pause(ref: RefArgument) -> None:
    """Stop scheduling a candybar (it stays registered; `run` still works)."""
    _set_enabled(ref, False)


def resume(ref: RefArgument) -> None:
    """Schedule a paused candybar again."""
    _set_enabled(ref, True)


def _next_unskipped_fire(api: CandybarApi, candybar: dict[str, Any], now: datetime) -> str | None:
    """The candybar's next fire with no skip on it.

    Fire instants come from the dispatcher's own `fire_times` (no /schedule
    window over every candybar, which a few minutely jobs push past the API's
    fire cap); each candidate's skip state is one single-minute /schedule read.
    """
    start = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    zone = ZoneInfo(candybar.get("timezone") or "UTC")
    candidates = fire_times(candybar["schedule"], zone, start, start + NEXT_FIRE_HORIZON)
    for _, fire in zip(range(NEXT_FIRE_CANDIDATES), candidates, strict=False):
        rows = api.schedule(_utc_z(fire), _utc_z(fire + timedelta(minutes=1)))
        row = next(
            (r for r in rows if r["candybarId"] == candybar["id"] and _parse_ts(r["fireTime"]) == fire),
            None,
        )
        if row is None or not row.get("skipped"):
            return _utc_z(fire)
    return None


def skip(
    ref: RefArgument,
    at: Annotated[
        str | None,
        typer.Option("--at", help="Skip the fire at this ISO time (naive = this machine's local time)."),
    ] = None,
    today: Annotated[
        bool, typer.Option("--today", help="Skip every remaining fire today (in the candybar's timezone).")
    ] = False,
    day: Annotated[
        str | None, typer.Option("--date", help="Skip every fire on this YYYY-MM-DD (candybar's timezone).")
    ] = None,
) -> None:
    """Skip the next fire (default), one fire (--at), or a whole day (--today / --date)."""
    if sum(bool(x) for x in (at, today, day)) > 1:
        typer.echo("pick at most one of --at, --today, --date", err=True)
        raise typer.Exit(code=2)
    api = _api()
    candybar = _resolve(api, ref)
    name = candybar["name"]
    try:
        if today or day:
            if day:
                try:
                    target = date.fromisoformat(day)
                except ValueError as exc:
                    typer.echo(f"--date must be YYYY-MM-DD, got {day!r}", err=True)
                    raise typer.Exit(code=2) from exc
            else:
                target = _now().astimezone(ZoneInfo(candybar.get("timezone") or "UTC")).date()
            api.skip_day(candybar["id"], target.isoformat())
            typer.echo(f"skipped {name} for {target.isoformat()} ({candybar.get('timezone', 'UTC')})")
            return
        if at:
            try:
                moment = datetime.fromisoformat(at)
            except ValueError as exc:
                typer.echo(f"--at must be an ISO time, got {at!r}", err=True)
                raise typer.Exit(code=2) from exc
            fire = _utc_z(moment.astimezone())  # astimezone() reads a naive time as local
        else:
            fire = _next_unskipped_fire(api, candybar, _now())
            if fire is None:
                typer.echo(
                    f"{name} has no unskipped fire among its next {NEXT_FIRE_CANDIDATES}; pass --at",
                    err=True,
                )
                raise typer.Exit(code=1)
        result = api.skip(candybar["id"], _utc_z(_parse_ts(fire)))
    except ApiError as exc:
        raise _fail(exc) from exc
    typer.echo(f"skipped {name} at {_when(result['fireTime'], _now())}")


def edit(
    ref: RefArgument,
    schedule_: Annotated[str | None, typer.Option("--schedule", help="New 5-field cron.")] = None,
    timezone: Annotated[str | None, typer.Option("--timezone", help="New IANA zone.")] = None,
    name: Annotated[str | None, typer.Option("--name", help="Rename.")] = None,
    description: Annotated[str | None, typer.Option("--description", help="New description.")] = None,
    tags: Annotated[
        str | None, typer.Option("--tags", help="Replace tags (comma-separated; '' clears).")
    ] = None,
    noise: Annotated[
        bool | None, typer.Option("--noise/--no-noise", help="Add to / remove from the noise collection.")
    ] = None,
) -> None:
    """Change a candybar's definition (the runner/entrypoint stays with `migrate` or PATCH)."""
    changes: dict[str, Any] = {
        key: value
        for key, value in (
            ("schedule", schedule_),
            ("timezone", timezone),
            ("name", name),
            ("description", description),
            ("noise", noise),
        )
        if value is not None
    }
    if tags is not None:
        changes["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if not changes:
        typer.echo("nothing to change: pass at least one option", err=True)
        raise typer.Exit(code=2)
    api = _api()
    candybar = _resolve(api, ref)
    try:
        updated = api.update(candybar["id"], changes)
    except ApiError as exc:
        raise _fail(exc) from exc
    diff = [
        f"{key}: {candybar.get(key)!r} -> {updated.get(key)!r}"
        for key in changes
        if candybar.get(key) != updated.get(key)
    ]
    typer.echo(f"updated {updated['name']}" + ("".join(f"\n  {d}" for d in diff) if diff else " (no change)"))


# --- run --wait and tail ----------------------------------------------------


def wait_for_invocation(
    api: CandybarApi,
    invocation_id: str,
    timeout_s: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any] | None:
    """Poll until the invocation has a result.

    The executor writes the row at claim time with a null `result` and fills
    it at finalize, so a 404 and a null result both mean "still running".
    """
    deadline = clock() + timeout_s
    delay = 0.5
    while True:
        try:
            row = api.get_invocation(invocation_id)
        except ApiError as exc:
            # A blip (API restart, proxy 502) says nothing about the run itself.
            if not (exc.unreachable or (exc.status or 0) >= 500):
                raise
            row = None
        if row is not None and row.get("result") is not None:
            return row
        if clock() >= deadline:
            return None
        sleep(delay)
        delay = min(delay * 2, 5.0)


def print_run(row: dict[str, Any]) -> None:
    typer.echo(
        f"{row['result']} in {_duration(row.get('runtimeMs'))} "
        f"(exit {row.get('exitCode')}, mem {_bytes(row.get('memoryPeakBytes'))}, "
        f"tokens {row.get('tokenSpend') or 0})"
    )
    if row["result"] != "success":
        if row.get("errorMessage"):
            typer.echo(f"error: {row['errorMessage']}")
        if row.get("stderrTail"):
            typer.echo(row["stderrTail"].rstrip())


def _sse_frames(url: str, last_id: str | None) -> Iterator[tuple[str | None, str, str]]:
    """(id, event, data) per SSE frame; returns when the server closes."""
    headers = {"Accept": "text/event-stream"}
    if last_id:
        headers["Last-Event-ID"] = last_id
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=180) as response:
        frame_id: str | None = None
        event = "message"
        data: list[str] = []
        for raw in response:
            line = raw.decode().rstrip("\r\n")
            if not line:
                if data or frame_id:
                    yield frame_id, event, "\n".join(data)
                frame_id, event, data = None, "message", []
                continue
            if line.startswith(":"):
                continue
            key, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if key == "id":
                frame_id = value
            elif key == "event":
                event = value
            elif key == "data":
                data.append(value)


def _describe(event: str, body: dict[str, Any], names: dict[str, str], now: datetime) -> str | None:
    """One human line per stream event, or None for events not worth a line."""
    stamp = now.astimezone().strftime("%H:%M:%S")
    candybar_id = body.get("candybarId")
    row = body.get("row") or {}
    name = row.get("name") or names.get(candybar_id or "", candybar_id or "?")
    if event == "invocation.started":
        return f"{stamp}  ▶ {name}  started ({body.get('trigger')})"
    if event == "invocation.completed":
        mark = "✓" if body.get("result") == "success" else "✗"
        return f"{stamp}  {mark} {name}  {body.get('result')} ({body.get('trigger')})"
    if event == "skip.applied":
        return f"{stamp}  ⤼ {name}  skipped ({body.get('reason')})"
    if event == "definition.changed":
        definition = body.get("definition") or {}
        if definition.get("id"):
            names[definition["id"]] = definition.get("name", names.get(definition["id"], "?"))
        changes = body.get("changes")
        detail = f" {json.dumps(changes)}" if changes else ""
        return f"{stamp}  ✎ {definition.get('name', name)}  {body.get('change')}{detail}"
    if event == "current.advanced":
        current = body.get("current")
        if not current:
            return f"{stamp}  · nothing scheduled"
        return f"{stamp}  · next: {current['name']} {_when(current['fireTime'], now)}"
    if event == "reset":
        return f"{stamp}  ↻ stream reset (missed events; refetch `candybar schedule`)"
    return None


def tail(
    show_ticks: Annotated[
        bool, typer.Option("--ticks", help="Also print the next-fire line on every tick.")
    ] = False,
    hide_noise: Annotated[
        bool, typer.Option("--hide-noise", help="Leave out events of noise candybars.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print each event's JSON payload.")] = False,
) -> None:
    """Follow live invocations, skips and definition changes (Ctrl-C to stop)."""
    api = _api()
    try:
        candybars = api.list_candybars()
    except ApiError as exc:
        raise _fail(exc) from exc
    names = {c["id"]: c["name"] for c in candybars}
    noisy = {c["id"] for c in candybars if c.get("noise")}
    url = f"{api.base}/schedule/stream"
    last_id: str | None = None
    # A reconnect backoff, not a schedule clock (AD-1 is the heartbeat's alone),
    # passed by reference like `migrate`'s Host.sleep.
    backoff = time.sleep
    try:
        while True:
            try:
                for frame_id, event, data in _sse_frames(url, last_id):
                    last_id = frame_id or last_id
                    try:
                        body = json.loads(data) if data else {}
                    except json.JSONDecodeError:
                        continue
                    if event == "definition.changed":
                        definition = body.get("definition") or {}
                        if definition.get("id") and "noise" in definition:
                            (noisy.add if definition["noise"] else noisy.discard)(definition["id"])
                    if hide_noise and body.get("candybarId") in noisy:
                        continue
                    if event == "current.advanced" and not show_ticks:
                        continue
                    if as_json:
                        typer.echo(json.dumps({"event": event, **body}))
                        continue
                    line = _describe(event, body, names, _now())
                    if line:
                        typer.echo(line)
            except BrokenPipeError:
                # The reader went away (`| head`): stop, quietly.
                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
                raise typer.Exit(code=0) from None
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                typer.echo(f"stream dropped ({exc}); reconnecting in {STREAM_RETRY_S:.0f}s", err=True)
            backoff(STREAM_RETRY_S)
    except KeyboardInterrupt:
        raise typer.Exit(code=0) from None


def install(app: typer.Typer) -> None:
    """Register the control commands on the root CLI."""
    app.command("ls")(ls)
    app.command("list")(ls)
    app.command("show")(show)
    app.command("next")(next_)
    app.command("schedule")(schedule)
    app.command("history")(history)
    app.command("pause")(pause)
    app.command("resume")(resume)
    app.command("archive")(archive)
    app.command("unarchive")(unarchive)
    app.command("skip")(skip)
    app.command("edit")(edit)
    app.command("tail")(tail)
