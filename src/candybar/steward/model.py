"""Steward contract shapes: Overview, Evaluation, Project, Profile and Task JSON.

steward-contract.md sections 2 and 3, as pydantic models and pure helpers
shared by the API routes and the steward role. No I/O and no database
imports (the registry is the only module that touches Postgres): callers
pass registry rows in and get contract JSON out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from ..events.envelope import StewardTaskData

ACTIONS = ("enrich", "fix", "evaluate", "refactor")
# The actions a client may request; `refactor` only ever comes from an
# evaluate's follow-up (contract section 3).
REQUESTABLE_ACTIONS = ("enrich", "fix", "evaluate")
STATUSES = ("queued", "running", "succeeded", "failed")
ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("succeeded", "failed")
# Actions that change a job (commit, push, `candybar edit`): one at a time per
# candybar (the API's 409) and per working directory (the worker's lock).
MUTATING_ACTIONS = ("fix", "refactor")
# Sessions that must not edit files (OpenCode permission rules + the prompt).
READ_ONLY_ACTIONS = ("enrich", "evaluate")
# The error of a task the operator cancelled (`candybar steward cancel`).
CANCELLED = "cancelled by operator"
# What happened to an evaluation's follow-up (stored on the evaluation).
FOLLOW_UP_STATUSES = ("queued", "done", "flagged", "failed")
# Task outcomes: fix fixed | not_fixed; refactor done_proven | done_not_proven | not_applied.
FIX_OUTCOMES = ("fixed", "not_fixed")
REFACTOR_OUTCOMES = ("done_proven", "done_not_proven", "not_applied")

MODEL_AUTO = "automaticai/auto"
MODEL_FAST = "automaticai/auto/fast"
# evaluate: auto; enrich, fix, refactor: auto/fast (contract section 3).
MODEL_FOR_ACTION = {
    "evaluate": MODEL_AUTO,
    "enrich": MODEL_FAST,
    "fix": MODEL_FAST,
    "refactor": MODEL_FAST,
}

OUTCOMES = ("Nominal", "Suboptimal", "Drifted", "Orphaned")
# Evaluate follow-ups run automatically (owner decision 2026-11-03).
FOLLOW_UP_FOR_OUTCOME = {
    "Nominal": "none",
    "Suboptimal": "refactor",
    "Drifted": "fix",
    "Orphaned": "archive",
}

# Allowed status moves; a terminal task never changes status again.
TRANSITIONS = {
    "queued": frozenset({"running", "failed"}),
    "running": frozenset({"succeeded", "failed"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
}

SUMMARY_MAX = 200


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# An array item: never empty or blank (an empty array is fine).
Item = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class OverviewSource(_Strict):
    repo: str | None
    paths: list[Item]
    runner: Literal["exec", "systemd", "hermes", "n8n"]


class Overview(_Strict):
    """The enrich overview: every field required, arrays may be empty, items never blank."""

    summary: str = Field(min_length=1, max_length=SUMMARY_MAX)
    purpose: str = Field(min_length=1)
    behavior: list[Item]
    inputs: list[Item]
    outputs: list[Item]
    sideEffects: list[Item]
    dependencies: list[Item]
    failureModes: list[Item]
    verification: list[Item]
    runbook: list[Item]
    source: OverviewSource
    cadenceNote: str = Field(min_length=1)


class Project(_Strict):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    method: Literal["ledger", "jev", "manual"]


class Axis(_Strict):
    rating: Literal["good", "fair", "poor"]
    finding: str = Field(min_length=1)


class Axes(_Strict):
    efficacy: Axis
    cost: Axis
    value: Axis
    usage: Axis
    alternatives: Axis


class EvaluationVerdict(_Strict):
    """What the evaluate agent answers; the steward adds the rest of an Evaluation."""

    outcome: Literal["Nominal", "Suboptimal", "Drifted", "Orphaned"]
    summary: str = Field(min_length=1)
    axes: Axes
    recommendation: str = Field(min_length=1)
    followUp: Literal["none", "refactor", "fix", "archive"] | None = None


class Evaluation(EvaluationVerdict):
    followUp: Literal["none", "refactor", "fix", "archive"]
    reportKey: str = Field(min_length=1)
    evaluatedAt: AwareDatetime
    taskId: UUID
    # What became of the follow-up: queued (a task is queued or running),
    # done, flagged (not acted on automatically: the operator decides) or
    # failed; null for followUp "none". The task, when one ran.
    followUpStatus: Literal["queued", "done", "flagged", "failed"] | None = None
    followUpTaskId: UUID | None = None

    @field_validator("evaluatedAt")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class ProfileUpdate(_Strict):
    """PUT /candybars/{id}/profile: any subset; merge per top-level key, null clears."""

    overview: Overview | None = None
    templateVersion: int | None = Field(default=None, ge=1)
    project: Project | None = None
    enrichedAt: AwareDatetime | None = None
    enrichTaskId: UUID | None = None
    evaluation: Evaluation | None = None


# Contract (camelCase) key -> registry column.
PROFILE_COLUMNS = {
    "overview": "overview",
    "templateVersion": "template_version",
    "project": "project",
    "enrichedAt": "enriched_at",
    "enrichTaskId": "enrich_task_id",
    "evaluation": "evaluation",
}

# Contract (camelCase) key -> registry column, for PATCH /steward/tasks/{id}.
TASK_COLUMNS = {
    "status": "status",
    "sessionId": "session_id",
    "outcome": "outcome",
    "summary": "summary",
    "reportKey": "report_key",
    "invocationId": "invocation_id",
    "error": "error",
    "startedAt": "started_at",
    "finishedAt": "finished_at",
    "costUsd": "cost_usd",
    "tokens": "tokens",
}


def profile_changes(update: ProfileUpdate) -> dict[str, Any]:
    """A validated PUT body as registry column changes (only the keys it set)."""
    out: dict[str, Any] = {}
    for key in update.model_fields_set:
        value = getattr(update, key)
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        elif isinstance(value, datetime):
            value = value.astimezone(UTC)
        out[PROFILE_COLUMNS[key]] = value
    return out


@dataclass
class DraftTask:
    """A task not stored yet: what its `queued` fact describes before the insert."""

    id: UUID
    candybar_id: UUID
    action: str
    model: str
    created_at: datetime
    status: str = "queued"
    parent_task_id: UUID | None = None
    session_id: str | None = None
    outcome: str | None = None
    summary: str | None = None
    report_key: str | None = None
    invocation_id: UUID | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _str(value: Any) -> str | None:
    return str(value) if value is not None else None


def profile_json(candybar_id: UUID, row: Any | None) -> dict[str, Any]:
    """The contract Profile; an empty one (all nulls) when no row exists yet."""
    if row is None:
        return {
            "candybarId": str(candybar_id),
            "overview": None,
            "templateVersion": None,
            "project": None,
            "enrichedAt": None,
            "enrichTaskId": None,
            "evaluation": None,
            "updatedAt": None,
        }
    return {
        "candybarId": str(row.candybar_id),
        "overview": row.overview,
        "templateVersion": row.template_version,
        "project": row.project,
        "enrichedAt": _iso(row.enriched_at),
        "enrichTaskId": _str(row.enrich_task_id),
        "evaluation": row.evaluation,
        "updatedAt": _iso(row.updated_at),
    }


def task_json(row: Any) -> dict[str, Any]:
    """The contract Task shape of one registry row."""
    return {
        "id": str(row.id),
        "candybarId": str(row.candybar_id),
        "action": row.action,
        "status": row.status,
        "model": row.model,
        "parentTaskId": _str(row.parent_task_id),
        "sessionId": row.session_id,
        "outcome": row.outcome,
        "summary": row.summary,
        "reportKey": row.report_key,
        "invocationId": _str(row.invocation_id),
        "error": row.error,
        "createdAt": _iso(row.created_at),
        "startedAt": _iso(row.started_at),
        "finishedAt": _iso(row.finished_at),
        # The OpenCode session's spend (cost as the provider reports it; the
        # gateway may report 0) and token usage {input, output, reasoning,
        # cache: {read, write}}; null until a session ran.
        "costUsd": getattr(row, "cost_usd", None),
        "tokens": getattr(row, "tokens", None),
    }


def task_fact_data(row: Any, name: str, overrides: dict[str, Any] | None = None) -> StewardTaskData:
    """The bus payload of a task fact: the row with `overrides` (column names) applied.

    Facts are published before the write (the spine's state rule), so the
    caller passes the changes it is about to write.
    """
    fields = {
        "task_id": row.id,
        "candybar_id": row.candybar_id,
        "action": row.action,
        "status": row.status,
        "model": row.model,
        "parent_task_id": row.parent_task_id,
        "session_id": row.session_id,
        "outcome": row.outcome,
        "summary": row.summary,
        "report_key": row.report_key,
        "invocation_id": row.invocation_id,
        "error": row.error,
        "created_at": row.created_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }
    for key, value in (overrides or {}).items():
        if key in fields:  # REST-only columns (cost_usd, tokens) stay off the bus
            fields[key] = value
    return StewardTaskData(name=name, **fields)


def transition_problem(current: str, wanted: str) -> str | None:
    """Why a task cannot move from `current` to `wanted`, or None when it can."""
    if wanted == current:
        return None
    if wanted not in TRANSITIONS.get(current, frozenset()):
        return f"a {current} task cannot become {wanted}"
    return None


def stamp_transition(
    current: str, changes: dict[str, Any], now: datetime | None = None
) -> dict[str, Any]:
    """Fill `started_at` / `finished_at` for a status move the caller did not stamp."""
    status = changes.get("status")
    if status is None or status == current:
        return changes
    moment = now or datetime.now(UTC)
    out = dict(changes)
    if status == "running":
        out.setdefault("started_at", moment)
    if status in TERMINAL_STATUSES:
        out.setdefault("finished_at", moment)
    return out


__all__ = [
    "ACTIONS",
    "ACTIVE_STATUSES",
    "CANCELLED",
    "FIX_OUTCOMES",
    "FOLLOW_UP_FOR_OUTCOME",
    "FOLLOW_UP_STATUSES",
    "MODEL_FOR_ACTION",
    "MUTATING_ACTIONS",
    "OUTCOMES",
    "PROFILE_COLUMNS",
    "READ_ONLY_ACTIONS",
    "REFACTOR_OUTCOMES",
    "REQUESTABLE_ACTIONS",
    "STATUSES",
    "TASK_COLUMNS",
    "TERMINAL_STATUSES",
    "Axes",
    "Axis",
    "DraftTask",
    "Evaluation",
    "EvaluationVerdict",
    "Overview",
    "OverviewSource",
    "ProfileUpdate",
    "Project",
    "profile_changes",
    "profile_json",
    "stamp_transition",
    "task_fact_data",
    "task_json",
    "transition_problem",
]
