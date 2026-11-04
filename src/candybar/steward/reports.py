"""Evaluate reports (steward-contract.md section 4, evaluate).

`~/code/DeLoDocs/Reports/<YYYY-MM-DD in America/New_York>/candybar-evaluate-<name>-<HHMM>-<task8>.md`
(HHMM in New York; task8 = the evaluate task id's first 8 characters, so a
second evaluate the same day never overwrites the first) with front matter
`date, type: candybar-evaluation, candybar, outcome, generated_by:
candybar-steward`, plus a sibling `.json`; both are copied with `mc cp` to
`delo/daily-reports/<date>/` (the hindsight analyzer's precedent). The
stored `reportKey` is `<date>/<stem>.md`.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

REPORTS_DIR_ENV = "CANDYBAR_REPORTS_DIR"
DEFAULT_REPORTS_DIR = Path.home() / "code" / "DeLoDocs" / "Reports"
S3_PREFIX_ENV = "CANDYBAR_REPORTS_S3"
DEFAULT_S3_PREFIX = "delo/daily-reports"
MC_ENV = "CANDYBAR_MC"
REPORT_ZONE = ZoneInfo("America/New_York")


class ReportError(RuntimeError):
    """The report could not be written or uploaded."""


@dataclass(frozen=True)
class Report:
    date: str
    key: str
    markdown: Path
    json: Path
    s3: tuple[str, str]


def report_date(moment: datetime) -> str:
    return moment.astimezone(REPORT_ZONE).date().isoformat()


def report_stem(name: str, moment: datetime, task_id: UUID | str) -> str:
    local = moment.astimezone(REPORT_ZONE)
    return f"candybar-evaluate-{name}-{local:%H%M}-{str(task_id)[:8]}"


def report_key(name: str, moment: datetime, task_id: UUID | str) -> str:
    return f"{report_date(moment)}/{report_stem(name, moment, task_id)}.md"


def report_json_path(key: str, reports_dir: Path | None = None) -> Path:
    """The local `.json` sibling of a stored `reportKey` (it holds the evaluation)."""
    root = (reports_dir or Path(os.environ.get(REPORTS_DIR_ENV) or DEFAULT_REPORTS_DIR)).expanduser()
    return root / (key.removesuffix(".md") + ".json")


def _axes_table(axes: dict[str, Any]) -> list[str]:
    lines = ["| Axis | Rating | Finding |", "|---|---|---|"]
    for axis in ("efficacy", "cost", "value", "usage", "alternatives"):
        entry = axes.get(axis) or {}
        finding = str(entry.get("finding", "")).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {axis} | {entry.get('rating', '-')} | {finding} |")
    return lines


def render_markdown(
    *,
    name: str,
    evaluation: dict[str, Any],
    definition: dict[str, Any],
    runner: dict[str, Any] | None,
    task: dict[str, Any],
    runs: list[dict[str, Any]],
    follow_up_note: str,
) -> str:
    date = evaluation["reportKey"].split("/", 1)[0]
    ok = sum(1 for r in runs if r.get("result") == "success")
    failed = sum(1 for r in runs if r.get("result") == "fail")
    front = [
        "---",
        f"date: {date}",
        "type: candybar-evaluation",
        f"candybar: {name}",
        f"outcome: {evaluation['outcome']}",
        "generated_by: candybar-steward",
        "tags:",
        "  - candybar",
        "  - steward",
        "  - generated",
        "---",
        "",
    ]
    body = [
        f"# Candybar evaluation: {name}",
        "",
        f"**Outcome: {evaluation['outcome']}.** {evaluation['summary']}",
        "",
        "## Axes",
        "",
        *_axes_table(evaluation.get("axes") or {}),
        "",
        "## Recommendation",
        "",
        evaluation["recommendation"],
        "",
        "## Follow-up",
        "",
        f"`{evaluation['followUp']}`: {follow_up_note}",
        "",
        "## The candybar",
        "",
        f"- id: `{definition.get('id')}`",
        f"- schedule: `{definition.get('schedule')}` ({definition.get('timezone', 'UTC')})",
        f"- state: {'archived' if definition.get('archived') else 'enabled' if definition.get('enabled', True) else 'paused'}",
        f"- runner: `{json.dumps(runner)}`" if runner else "- runner: bare entrypoint",
        f"- last {len(runs)} runs: {ok} succeeded, {failed} failed",
        f"- totals: {definition.get('totalInvoked', 0)} invoked, {definition.get('totalSuccessful', 0)} succeeded",
        "",
        "## Provenance",
        "",
        f"- steward task: `{task.get('id')}` (model `{task.get('model')}`)",
        f"- OpenCode session: `{task.get('sessionId')}`",
        f"- evaluated at: {evaluation['evaluatedAt']}",
        "",
    ]
    return "\n".join(front + body)


def _mc() -> str:
    return os.environ.get(MC_ENV, "mc")


def write_and_upload(
    *,
    name: str,
    evaluation: dict[str, Any],
    markdown: str,
    payload: dict[str, Any],
    reports_dir: Path | None = None,
    s3_prefix: str | None = None,
) -> Report:
    """Write both files, then `mc cp` both. Blocking; raises ReportError on any failure."""
    date, _, file_name = evaluation["reportKey"].partition("/")
    stem = file_name.removesuffix(".md") or f"candybar-evaluate-{name}"
    root = (reports_dir or Path(os.environ.get(REPORTS_DIR_ENV) or DEFAULT_REPORTS_DIR)).expanduser()
    folder = root / date
    md_path = folder / f"{stem}.md"
    json_path = folder / f"{stem}.json"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown, encoding="utf-8")
        json_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ReportError(f"could not write the report under {folder}: {exc}") from exc
    prefix = (s3_prefix or os.environ.get(S3_PREFIX_ENV) or DEFAULT_S3_PREFIX).rstrip("/")
    targets = (f"{prefix}/{date}/{md_path.name}", f"{prefix}/{date}/{json_path.name}")
    for source, target in zip((md_path, json_path), targets, strict=True):
        try:
            done = subprocess.run(
                [_mc(), "cp", "--quiet", str(source), target],
                capture_output=True, text=True, timeout=60, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReportError(f"mc cp {source.name} failed: {exc}") from exc
        if done.returncode != 0:
            raise ReportError(f"mc cp {source.name} -> {target} failed: {(done.stderr or done.stdout).strip()[:300]}")
    return Report(date=date, key=evaluation["reportKey"], markdown=md_path, json=json_path, s3=targets)


async def write_and_upload_async(**kwargs: Any) -> Report:
    return await asyncio.to_thread(write_and_upload, **kwargs)
