"""FastAPI app factory for the candybar API role.

Story 1.4 scope: `POST /candybars` (register). Story 1.7 adds `GET /metrics`
(Prometheus exposition of the process registry, OpenMetrics-negotiated so the
executor's `invocation_id` exemplars survive the scrape). The register flow follows the
spine's state rule — the `bloodbank.cron.candybar.created` fact is published
FIRST, then the registry row commits in the same request flow; a publish
failure means no row (503), a unique-violation race maps to 409.
Story 1.8 adds `POST /candybars/{id}/invoke` (AD-12): the api publishes one
`bloodbank.cmd.cron.candybar.invoke` command with `trigger=manual` and answers
202 — the executor runs it down the same path as a scheduled fire.
Story 3.3 adds the per-invocation record read (AD-9: "the full per-invoke
record ... stored in the `invocations` table, queryable via the API"):
`GET /invocations/{id}` resolves an `invocation_id` exemplar to its stored
row, and `GET /candybars/{id}/invocations?limit=N` lists a candybar's
recorded invocations newest first. Rows exist only once terminal, so an
in-flight invocation is a 404.
Story 5.1 completes definitions CRUD per holocene-view-contract.md:
`GET /candybars` (bare array of the definition shape), `GET /candybars/{id}`
(definition + `totalInvoked`/`totalSuccessful`), `POST /candybars` answering
that single shape, and `PATCH /candybars/{id}` — the row is locked, diffed,
and when anything changed one `bloodbank.cron.candybar.updated` fact
(`changes` = the changed fields only) is published FIRST, then the row change
commits in the same flow (spine state rule, as on register). Response shapes
are the contract's, field for field: `entrypoint` is executor-internal and is
not echoed.
Story 5.2 adds `GET /schedule?from=<ts>&to=<ts>`: the contract schedule rows
for every fire in the half-open UTC window, computed by `api/schedule.py`
with the dispatcher's croniter/skip helpers (AD-13) off the event loop, past
rows resolved to their recorded invocation's result by idempotency key.
Story 5.3 adds `GET /schedule/next`, the stream's poll fallback: the single
`current` row (the same row `/schedule` marks current), read from the
definitions and the future skips only, one `next_fire` per candybar; a 404
when nothing will fire.
Story 5.4 adds `GET /schedule/stream` (AD-11): the SSE channel of live
updates, served from the api role's `ScheduleStream` hub (`api/stream.py`),
optionally scoped to a `from`/`to` window and resumable with `Last-Event-ID`.
Story 5.5 adds the noise collection: `PUT`/`DELETE /candybars/{id}/noise`
add or remove one candybar, and `PUT /noise/batch` `{ids}` replaces the whole
collection atomically. Each changed candybar gets one `updated` fact
(`changes: {noise}`) FIRST, then the rows commit in one transaction, as on
PATCH. candybar stores only the explicit collection: there is no `<10m`
auto-classification here (that is the view's).
Story 5.6 adds the skip operations (contract "Skip operations"):
`POST /candybars/{id}/skip` `{fireTime}` stores one occurrence skip and
`POST /candybars/{id}/skip-rest-of-day` `{date}` stores one rest-of-day skip.
A target the schedule never produces is a 404. Each NEW skip publishes one
`bloodbank.cron.candybar.skipped` fact FIRST (reason = the skip kind), then
the row is stored in the same request flow; the dispatcher's per-tick skip
read honors it, and the SSE hub's existing `_on_skipped` turns the fact into
`skip.applied` with no stream changes.
Story 7.5 (AD-21) adds the `enabled` flag: `POST /candybars/{id}/enabled`
`{enabled}` sets it (the noise flow: lock, `updated` fact first, write), and
register accepts `enabled: false` so a migration cutover can register a job
before retiring its native scheduler. A disabled candybar is listed with
`enabled: false`, never dispatched, and has no upcoming `/schedule` rows.
The steward contract (steward-contract.md, 2026-11-03) adds: `POST
/candybars/{id}/archived` `{archived}` (archiving also disables, in one
`updated` fact; enabling an archived candybar is a 409), the per-candybar
profile (`GET`/`PUT /candybars/{id}/profile`, `GET /profiles`; overview and
evaluation validated against the contract shapes), and steward tasks: `POST
/candybars/{id}/steward` `{action}` publishes the
`bloodbank.cmd.cron.candybar.steward` command and the `steward_task.queued`
fact FIRST, then stores the queued task (202); `GET
/candybars/{id}/steward/tasks`, `GET /steward/tasks`, the worker's `PATCH
/steward/tasks/{taskId}`, which publishes the status fact before the write,
and `POST /steward/tasks/{taskId}/cancel` (failed, "cancelled by operator";
a cancelled follow-up settles its evaluation's `followUpStatus`).
A fix is a 409 while a fix or refactor is active for the candybar.
All errors are RFC 7807 problem+json. Epic 5 adds the rest of the CAP-8 read surface.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import nats.errors
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import REGISTRY, CollectorRegistry
from prometheus_client.exposition import choose_encoder
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from candybar.dispatcher.schedule import group_skips, widen_skip_window
from candybar.events.builders import (
    STEWARD_FACT_FOR_STATUS,
    build_candybar_created,
    build_candybar_invoke,
    build_candybar_skipped,
    build_candybar_steward,
    build_candybar_updated,
    build_steward_task_fact,
)
from candybar.events.vocabulary import tick_id_for_minute
from candybar.registry import (
    INVOCATION_LIST_MAX,
    SKIP_OCCURRENCE,
    SKIP_REST_OF_DAY,
    STEWARD_TASK_LIST_MAX,
    CandybarManifest,
    DuplicateActiveTaskError,
    DuplicateNameError,
    UnknownCandybarError,
    active_steward_task,
    add_occurrence_skip,
    add_rest_of_day_skip,
    fire_results,
    get_candybar,
    get_invocation,
    get_profile,
    get_steward_task,
    insert_candybar,
    insert_steward_task,
    list_candybars,
    list_invocations,
    list_profiles,
    list_skips,
    list_steward_tasks,
    lock_candybar,
    lock_noise_batch,
    name_taken,
    occurrence_skip_id,
    rest_of_day_skip_id,
    skip_stored,
    update_candybar,
    update_steward_task,
    upsert_profile,
    uuid7,
)
from candybar.steward.model import (
    CANCELLED,
    MODEL_FOR_ACTION,
    MUTATING_ACTIONS,
    TASK_COLUMNS,
    TERMINAL_STATUSES,
    DraftTask,
    ProfileUpdate,
    profile_changes,
    profile_json,
    settled_evaluation,
    stamp_transition,
    task_fact_data,
    task_json,
    transition_problem,
)

from .schedule import (
    WindowCandybar,
    WindowError,
    attach_results,
    fires_on_day,
    first_tick_after,
    is_fire_of,
    next_row,
    past_keys,
    skip_read_start,
    visible_rows,
    window_bounds,
    window_rows,
)
from .shapes import candybar_row_json, utc_iso
from .stream import HEADERS as STREAM_HEADERS
from .stream import MEDIA_TYPE as STREAM_MEDIA_TYPE
from .stream import ScheduleStream

log = logging.getLogger(__name__)

PROBLEM_JSON = "application/problem+json"

# Transport failures that mean "the command/fact never reached the bus".
BUS_ERRORS = (nats.errors.Error, OSError, TimeoutError)

SessionFactory = Callable[[], AbstractAsyncContextManager[Any]]


class RegisterRequest(BaseModel):
    """POST /candybars body — validated by the same rules as candybar.toml."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    tags: list[str] = []
    schedule: str
    timezone: str = "UTC"
    # Exactly one of the two (Story 7.3, AD-18); the manifest enforces it.
    entrypoint: str | None = None
    runner: dict[str, Any] | None = None
    noise: bool = False
    # Story 7.5 (AD-21): `candybar migrate cutover` registers disabled first.
    enabled: bool = True

    def to_manifest(self) -> CandybarManifest:
        # Reuses the manifest validators (slug name, 5-field cron); the
        # route then requires an absolute, existing entrypoint
        # (`entrypoint_problem`) — the CLI resolves relative paths first.
        return CandybarManifest.model_validate(self.model_dump())


class UpdateRequest(BaseModel):
    """PATCH /candybars/{id} body — any non-empty subset of the writable fields.

    Defaults are never validated, so an omitted field stays unset while an
    explicit `null` fails its type (422): "clear this field" is not a thing.
    Unknown and read-only keys (id, createdAt, counters) are refused.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default=None)  # type: ignore[assignment]
    description: str = Field(default=None)  # type: ignore[assignment]
    tags: list[str] = Field(default=None)  # type: ignore[assignment]
    schedule: str = Field(default=None)  # type: ignore[assignment]
    timezone: str = Field(default=None)  # type: ignore[assignment]
    entrypoint: str = Field(default=None)  # type: ignore[assignment]
    # A runner object replaces the entrypoint and vice versa: whichever the
    # patch names wins (Story 7.3).
    runner: dict[str, Any] = Field(default=None)  # type: ignore[assignment]
    noise: bool = Field(default=None)  # type: ignore[assignment]


# Upper bound on one `PUT /noise/batch` body. The batch carries the WHOLE
# collection (replace semantics), so this caps the registry's noise set too.
NOISE_BATCH_MAX = 10_000


class NoiseBatchRequest(BaseModel):
    """PUT /noise/batch body (contract: `{ ids: [] }`) — the complete noise collection."""

    model_config = ConfigDict(extra="forbid")

    ids: list[UUID] = Field(max_length=NOISE_BATCH_MAX)


class EnabledRequest(BaseModel):
    """POST /candybars/{id}/enabled body (Story 7.5, AD-21): the wanted state.

    A state, not a blind toggle, so a retried request (or a rollback re-run)
    converges instead of flipping the flag back.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class ArchivedRequest(BaseModel):
    """POST /candybars/{id}/archived body (steward contract section 1): the wanted state."""

    model_config = ConfigDict(extra="forbid")

    archived: bool


class StewardRequest(BaseModel):
    """POST /candybars/{id}/steward body. `refactor` is never requested (an evaluate queues it)."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["enrich", "fix", "evaluate"]


class TaskPatchRequest(BaseModel):
    """PATCH /steward/tasks/{taskId} body: any non-empty subset (worker-only by convention)."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["queued", "running", "succeeded", "failed"] = Field(default=None)  # type: ignore[assignment]
    sessionId: str | None = None
    outcome: str | None = None
    summary: str | None = None
    reportKey: str | None = None
    invocationId: UUID | None = None
    error: str | None = None
    startedAt: AwareDatetime | None = None
    finishedAt: AwareDatetime | None = None
    costUsd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tokens: dict[str, Any] | None = None


# Steward task list bounds (contract section 3).
CANDYBAR_TASKS_DEFAULT = 20
CANDYBAR_TASKS_MAX = 200
ALL_TASKS_DEFAULT = 100


class SkipRequest(BaseModel):
    """POST /candybars/{id}/skip body (contract: `{ fireTime: <ts> }`).

    AwareDatetime refuses a naive timestamp (422); any offset is converted
    to UTC. An off-minute fireTime is refused by the route below.
    """

    model_config = ConfigDict(extra="forbid")

    fireTime: AwareDatetime


class SkipDayRequest(BaseModel):
    """POST /candybars/{id}/skip-rest-of-day body (contract: `{ date: <YYYY-MM-DD> }`).

    pydantic's `date` accepts only the ISO date form — a datetime string or
    "2026-13-40" is a 422 — and arrives as a plain `date`, never a
    `datetime` (the store refuses those).
    """

    model_config = ConfigDict(extra="forbid")

    date: date


def problem(status: int, title: str, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type=PROBLEM_JSON,
        content={
            "type": "about:blank",
            "title": title,
            "status": status,
            "detail": detail,
            **extra,
        },
    )


def entrypoint_problem(entrypoint: str) -> JSONResponse | None:
    """422 unless `entrypoint` is an ABSOLUTE path to an existing file, else None.

    The API has no manifest directory to resolve a relative path against
    (the CLI resolves before sending). A relative path would be checked
    against the api process's CWD here, then exec'd by the executor from the
    entrypoint's own parent directory (or looked up on PATH when it has no
    slash) — a different file, or none: a candybar that can never run.
    """
    path = Path(entrypoint)
    if not path.is_absolute():
        return problem(
            422,
            "Invalid candybar definition",
            f"entrypoint must be an absolute path: {entrypoint!r}",
        )
    if not path.is_file():
        return problem(
            422,
            "Invalid candybar definition",
            f"entrypoint is not an existing file: {entrypoint!r}",
        )
    return None


def candybar_detail_json(row: Any) -> dict[str, Any]:
    """The single-definition shape: definition plus registry counters (GET /candybars/{id}).

    Contract-verbatim: the executor-facing `entrypoint`/`runner` are not in it
    (read them from `GET /candybars/{id}/runner`, Story 7.3).
    """
    return {
        **candybar_row_json(row),
        "totalInvoked": row.total_invoked,
        "totalSuccessful": row.total_successful,
    }


def definition_of(row: Any) -> dict[str, Any]:
    """A stored row's writable definition, in CandybarManifest field names."""
    return {
        "name": row.name,
        "description": row.description,
        "tags": list(row.tags),
        "schedule": row.schedule,
        "timezone": row.timezone,
        # A runner row's entrypoint column is only a label: the definition is
        # the runner. At most one of the two is ever set.
        "entrypoint": None if row.runner is not None else row.entrypoint,
        "runner": row.runner,
        "noise": row.noise,
    }


def invocation_row_json(row: Any) -> dict[str, Any]:
    """The full per-invoke record (AD-9): identity, outcome, all five measures, token fields."""
    return {
        "id": str(row.id),
        "candybarId": str(row.candybar_id),
        "trigger": row.trigger,
        "tickId": row.tick_id,
        "idempotencyKey": row.idempotency_key,
        "result": row.result,
        "exitCode": row.exit_code,
        "errorMessage": row.error_message,
        "stderrTail": row.stderr_tail,
        "runtimeMs": row.runtime_ms,
        "memoryPeakBytes": row.memory_peak_bytes or 0,
        "cpuLoad": row.cpu_load or 0.0,
        "gpuLoad": row.gpu_load or 0.0,
        "tokenSpend": row.token_spend or 0,
        "costUsd": float(row.cost_usd) if row.cost_usd is not None else None,
        "startedAt": utc_iso(row.started_at),
        "finishedAt": utc_iso(row.finished_at),
    }


def create_app(
    *,
    session_factory: SessionFactory,
    sink: Any,
    metrics_registry: CollectorRegistry = REGISTRY,
    now: Callable[[], datetime] | None = None,
    stream: ScheduleStream | None = None,
) -> FastAPI:
    # The wall clock decides which schedule rows are past/current (Story 5.2);
    # reading it is not owning a timer (AD-1). Injectable for tests.
    clock = now or (lambda: datetime.now(UTC))
    # The live-update hub (Story 5.4). The api role passes the one it feeds
    # from the bus; without one, the stream serves `ready` and nothing else.
    live = stream or ScheduleStream(session_factory=session_factory, now=clock)
    app = FastAPI(title="candybar", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.stream = live

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        # Liveness for the docker healthcheck / Traefik. Deliberately does
        # not touch the DB or bus: a dependency blip must not restart-loop
        # the whole role composition.
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        # Prometheus asks for application/openmetrics-text when it can store
        # exemplars; anything else gets the classic text format 0.0.4.
        encoder, content_type = choose_encoder(request.headers.get("accept", ""))
        return Response(content=encoder(metrics_registry), media_type=content_type)

    @app.exception_handler(RequestValidationError)
    async def _validation_problem(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem(422, "Invalid request body", str(exc.errors())[:500])

    @app.exception_handler(StarletteHTTPException)
    async def _http_problem(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Unknown routes / wrong methods speak RFC 7807 too, not starlette's {"detail"}.
        response = problem(exc.status_code, str(exc.detail), f"{exc.status_code} {exc.detail}")
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(Exception)
    async def _unhandled_problem(_request: Request, exc: Exception) -> JSONResponse:
        # The API speaks RFC 7807 on every failure — never starlette's plain 500.
        log.error("unhandled API error (%s): %s", type(exc).__name__, exc)
        return problem(500, "Internal error", f"unexpected {type(exc).__name__}")

    @app.post("/candybars", status_code=201)
    async def register(body: RegisterRequest) -> JSONResponse:
        try:
            manifest = body.to_manifest()
        except ValueError as exc:
            # pydantic wraps the field validators; keep the message readable.
            return problem(422, "Invalid candybar definition", str(exc)[:500])

        # A runner names argv/workdir/unit on the HOST the executor runs on,
        # which this (containerized) process cannot see: only the exec default
        # is checked against this filesystem.
        if manifest.entrypoint is not None:
            invalid_entrypoint = entrypoint_problem(manifest.entrypoint)
            if invalid_entrypoint is not None:
                return invalid_entrypoint

        try:
            async with session_factory() as session:
                if await name_taken(session, manifest.name):
                    return problem(
                        409, "Candybar name already registered", f"name {manifest.name!r} is taken"
                    )

                candybar_id = uuid7()
                event = build_candybar_created(
                    candybar_id=candybar_id,
                    name=manifest.name,
                    description=manifest.description,
                    tags=list(manifest.tags),
                    schedule=manifest.schedule,
                    timezone=manifest.timezone,
                    noise=manifest.noise,
                    enabled=manifest.enabled,
                )
                try:
                    await sink.publish(event)
                except BUS_ERRORS as exc:
                    log.error("failed to publish candybar.created for %s: %s", manifest.name, exc)
                    return problem(
                        503,
                        "Event bus unavailable",
                        "could not publish candybar.created; no row written",
                    )

                row = await insert_candybar(session, manifest, candybar_id=candybar_id)
        except DuplicateNameError:
            # Unique-violation race: another register won between the
            # name_taken check and the flush (session_scope rolled back).
            return problem(
                409, "Candybar name already registered", f"name {manifest.name!r} is taken"
            )

        return JSONResponse(status_code=201, content=candybar_detail_json(row))

    @app.get("/candybars")
    async def definitions() -> JSONResponse:
        """Every registered candybar in the contract definition shape, oldest first."""
        async with session_factory() as session:
            body = [candybar_row_json(row) for row in await list_candybars(session)]
        return JSONResponse(content=body)

    @app.get("/candybars/{candybar_id}")
    async def definition(candybar_id: UUID) -> JSONResponse:
        """One definition plus its registry counters."""
        async with session_factory() as session:
            row = await get_candybar(session, candybar_id)
            body = candybar_detail_json(row) if row is not None else None
        if body is None:
            return problem(404, "Candybar not found", f"no candybar registered with id {candybar_id}")
        return JSONResponse(content=body)

    @app.get("/candybars/{candybar_id}/runner")
    async def runner_of(candybar_id: UUID) -> JSONResponse:
        """How the executor starts this candybar (Story 7.3, AD-18): outside the contract.

        `{runner}` is the stored runner object, or null with `{entrypoint}` set
        for the bare-executable default. Exactly one of the two is non-null.
        """
        async with session_factory() as session:
            row = await get_candybar(session, candybar_id)
            body = (
                {
                    "runner": row.runner,
                    "entrypoint": None if row.runner is not None else row.entrypoint,
                }
                if row is not None
                else None
            )
        if body is None:
            return problem(404, "Candybar not found", f"no candybar registered with id {candybar_id}")
        return JSONResponse(content=body)

    @app.patch("/candybars/{candybar_id}")
    async def update(candybar_id: UUID, body: UpdateRequest) -> JSONResponse:
        """Change a definition: lock, diff, publish `updated` (changes only), write."""
        patch = body.model_dump(exclude_unset=True)
        if not patch:
            return problem(422, "Invalid candybar update", "no fields to update")

        try:
            async with session_factory() as session:
                row = await lock_candybar(session, candybar_id)
                if row is None:
                    return problem(
                        404, "Candybar not found", f"no candybar registered with id {candybar_id}"
                    )
                current = definition_of(row)
                base = dict(current)
                if patch.get("runner") is not None and "entrypoint" not in patch:
                    base["entrypoint"] = None  # the runner replaces the entrypoint
                if patch.get("entrypoint") is not None and "runner" not in patch:
                    base["runner"] = None  # the entrypoint replaces the runner
                try:
                    merged = CandybarManifest.model_validate({**base, **patch})
                except ValueError as exc:
                    return problem(422, "Invalid candybar definition", str(exc)[:500])
                wanted = merged.model_dump()
                changes = {k: wanted[k] for k in patch if wanted[k] != current[k]}
                if not changes:
                    # Nothing differs from the stored row: no fact, no write.
                    return JSONResponse(content=candybar_detail_json(row))
                if "entrypoint" in changes and current["runner"] is not None:
                    changes["runner"] = None  # the fact reports the implicit switch too

                if "entrypoint" in changes and merged.entrypoint is not None:
                    invalid_entrypoint = entrypoint_problem(merged.entrypoint)
                    if invalid_entrypoint is not None:
                        return invalid_entrypoint
                if "name" in changes and await name_taken(session, merged.name):
                    return problem(
                        409, "Candybar name already registered", f"name {merged.name!r} is taken"
                    )

                event = build_candybar_updated(
                    candybar_id=candybar_id, name=merged.name, changes=changes
                )
                try:
                    await sink.publish(event)
                except BUS_ERRORS as exc:
                    log.error("failed to publish candybar.updated for %s: %s", candybar_id, exc)
                    return problem(
                        503,
                        "Event bus unavailable",
                        "could not publish candybar.updated; the definition is unchanged",
                    )

                row = await update_candybar(session, row, changes)
                result = candybar_detail_json(row)
        except DuplicateNameError:
            # Rename race: another writer took the name between the check and
            # the flush (session_scope rolled back; the fact may already be out).
            return problem(
                409, "Candybar name already registered", f"name {patch.get('name')!r} is taken"
            )
        return JSONResponse(content=result)

    async def set_flag(candybar_id: UUID, flag: str, value: bool) -> JSONResponse:
        """Set one boolean definition flag: `noise` (Story 5.5) or `enabled` (Story 7.5).

        The PATCH flow for the one field: lock, compare, publish `updated`
        with `changes: {<flag>}`, then write in the same transaction.
        """
        async with session_factory() as session:
            row = await lock_candybar(session, candybar_id)
            if row is None:
                return problem(
                    404, "Candybar not found", f"no candybar registered with id {candybar_id}"
                )
            if flag == "enabled" and value and getattr(row, "archived", False):
                # Steward contract section 1: unarchive first.
                return problem(
                    409,
                    "Candybar is archived",
                    f"{row.name} is archived; unarchive it before enabling it",
                )
            if getattr(row, flag) == value:
                # Already in that state: no fact, no write.
                return JSONResponse(content=candybar_detail_json(row))
            event = build_candybar_updated(
                candybar_id=candybar_id, name=row.name, changes={flag: value}
            )
            try:
                await sink.publish(event)
            except BUS_ERRORS as exc:
                log.error("failed to publish candybar.updated (%s) for %s: %s", flag, candybar_id, exc)
                return problem(
                    503,
                    "Event bus unavailable",
                    f"could not publish candybar.updated; the {flag} flag is unchanged",
                )
            row = await update_candybar(session, row, {flag: value})
            result = candybar_detail_json(row)
        return JSONResponse(content=result)

    async def set_noise(candybar_id: UUID, noise: bool) -> JSONResponse:
        """Put one candybar in (`noise=True`) or out of the noise collection (Story 5.5)."""
        return await set_flag(candybar_id, "noise", noise)

    @app.post("/candybars/{candybar_id}/enabled")
    async def set_enabled(candybar_id: UUID, body: EnabledRequest) -> JSONResponse:
        """Enable or disable a candybar (Story 7.5, AD-21). Outside the Holocene contract.

        A disabled candybar stays registered and listed (with `enabled:
        false`) but is never dispatched and has no upcoming `/schedule` rows.
        `candybar migrate` drives it: cutover registers disabled, retires the
        native scheduler, then enables; rollback disables.
        """
        return await set_flag(candybar_id, "enabled", body.enabled)

    @app.put("/candybars/{candybar_id}/noise")
    async def add_noise(candybar_id: UUID) -> JSONResponse:
        """Add one candybar to the noise collection."""
        return await set_noise(candybar_id, True)

    @app.delete("/candybars/{candybar_id}/noise")
    async def remove_noise(candybar_id: UUID) -> JSONResponse:
        """Remove one candybar from the noise collection."""
        return await set_noise(candybar_id, False)

    @app.put("/noise/batch")
    async def noise_batch(body: NoiseBatchRequest) -> JSONResponse:
        """Replace the noise collection: afterwards exactly `ids` are noise. Atomic.

        The rows that can change (the ids plus today's noise rows) are locked
        in one ordered statement; any unknown id refuses the whole batch. Every
        `updated` fact is published before any row is written, and the writes
        commit as one transaction, so a failure anywhere changes no flag.
        """
        wanted = set(body.ids)
        async with session_factory() as session:
            rows = await lock_noise_batch(session, wanted)
            unknown = wanted - {row.id for row in rows}
            if unknown:
                listed = ", ".join(sorted(str(candybar_id) for candybar_id in unknown))
                return problem(
                    422,
                    "Unknown candybar ids",
                    f"{len(unknown)} id(s) not registered; nothing changed: {listed}"[:500],
                )
            changed = [row for row in rows if row.noise != (row.id in wanted)]
            for row in changed:
                event = build_candybar_updated(
                    candybar_id=row.id, name=row.name, changes={"noise": row.id in wanted}
                )
                try:
                    await sink.publish(event)
                except BUS_ERRORS as exc:
                    log.error("failed to publish candybar.updated (noise batch) for %s: %s", row.id, exc)
                    # Nothing is written yet. Facts already out for earlier
                    # rows are superseded by the retry, which replace
                    # semantics make converge.
                    return problem(
                        503,
                        "Event bus unavailable",
                        "the bus did not confirm every candybar.updated; no noise flag was "
                        "changed, retry the batch",
                    )
            for row in changed:
                await update_candybar(session, row, {"noise": row.id in wanted})
            members = sorted(
                (row for row in rows if row.id in wanted), key=lambda r: (r.created_at, r.id)
            )
            result = {"ids": [str(row.id) for row in members]}
        return JSONResponse(content=result)

    async def _store_skip(
        candybar_id: UUID,
        *,
        fire_time: datetime | None,
        day: date | None,
    ) -> JSONResponse:
        """One skip write (Story 5.6): 404 check, fact FIRST, then the row.

        Shared by both skip routes: `fire_time` is set for an occurrence skip,
        `day` for a rest-of-day skip (exactly one is set). The 404 rule names
        only targets the schedule never produces; a real fire that already
        passed stores inertly (the Epic 2 precedent). The published fact is
        the same `skipped` fact the dispatcher emits for the fire — the SSE
        hub turns it into `skip.applied` with no stream changes — and its id
        is the store's deterministic skip id, so a concurrent twin that slips
        past the repeat check broker-dedupes against it. A repeat of a stored
        skip publishes nothing and keeps the first row.
        """
        async with session_factory() as session:
            row = await get_candybar(session, candybar_id)
            if row is None:
                return problem(
                    404, "Candybar not found", f"no candybar registered with id {candybar_id}"
                )
            candybar = WindowCandybar.of(row)
            if fire_time is not None:
                assert day is None
                fire = fire_time.astimezone(UTC)
                if fire.second or fire.microsecond:
                    return problem(
                        422, "Invalid fireTime", "fireTime must be aligned to a whole minute"
                    )
                if not await asyncio.to_thread(is_fire_of, candybar, fire):
                    return problem(
                        404,
                        "No such fire",
                        f"{row.name} never fires at {utc_iso(fire)}",
                    )
                kind = SKIP_OCCURRENCE
                target = fire
                skip_id = occurrence_skip_id(candybar_id, fire)
            else:
                assert day is not None
                first = await asyncio.to_thread(fires_on_day, candybar, day)
                if first is None:
                    return problem(
                        404,
                        "No such fire",
                        f"{row.name} never fires on {day.isoformat()}",
                    )
                kind = SKIP_REST_OF_DAY
                target = first
                skip_id = rest_of_day_skip_id(candybar_id, day)
            if await skip_stored(session, skip_id):
                # A repeat of a stored skip: 200, nothing published, nothing
                # written — the first row and its note stand. (A concurrent
                # twin that slips past this check republishes the SAME
                # deterministic event id, which the broker dedupes.)
                body = {"skipped": True}
                if fire_time is not None:
                    body["fireTime"] = utc_iso(fire)
                else:
                    body["date"] = day.isoformat()
                return JSONResponse(content=body)
            event = build_candybar_skipped(
                candybar_id=candybar_id,
                name=row.name,
                tick_id=tick_id_for_minute(int(target.timestamp()) // 60),
                reason=kind,
                event_id=skip_id,
            )
            try:
                await sink.publish(event)
            except BUS_ERRORS as exc:
                log.error("failed to publish candybar.skipped (%s) for %s: %s", kind, candybar_id, exc)
                return problem(
                    503,
                    "Event bus unavailable",
                    "could not publish candybar.skipped; the skip is not stored, retry the request",
                )
            if fire_time is not None:
                await add_occurrence_skip(session, candybar_id, fire)
            else:
                await add_rest_of_day_skip(session, candybar_id, day)
            body: dict[str, Any] = {"skipped": True}
            if fire_time is not None:
                body["fireTime"] = utc_iso(fire)
            else:
                body["date"] = day.isoformat()
        return JSONResponse(content=body)

    @app.post("/candybars/{candybar_id}/skip")
    async def skip_occurrence(candybar_id: UUID, body: SkipRequest) -> JSONResponse:
        """Skip one occurrence of a candybar: the fire `fireTime` never dispatches."""
        return await _store_skip(candybar_id, fire_time=body.fireTime, day=None)

    @app.post("/candybars/{candybar_id}/skip-rest-of-day")
    async def skip_rest_of_day(candybar_id: UUID, body: SkipDayRequest) -> JSONResponse:
        """Skip the rest of a candybar-local date: no fire of that day dispatches (AD-16)."""
        return await _store_skip(candybar_id, fire_time=None, day=body.date)

    @app.post("/candybars/{candybar_id}/invoke", status_code=202)
    async def invoke(candybar_id: UUID) -> JSONResponse:
        """Manual invoke (AD-12): publish one trigger=manual command, answer 202."""
        async with session_factory() as session:
            row = await get_candybar(session, candybar_id)
            name = row.name if row is not None else None
        if name is None:
            return problem(404, "Candybar not found", f"no candybar registered with id {candybar_id}")

        # A fresh command per request: two runs are two invocations. The
        # envelope id doubles as the command id (as in the dispatcher), so a
        # re-sent publish dedupes on Nats-Msg-Id.
        command_id = uuid4()
        command = build_candybar_invoke(
            candybar_id=candybar_id,
            name=name,
            trigger="manual",
            command_id=command_id,
            event_id=command_id,
        )
        try:
            await sink.publish(command)
        except BUS_ERRORS as exc:
            # A timed-out publish may still have been stored: say so rather
            # than promise "nothing happened" (a blind retry is a 2nd run).
            log.error("failed to publish manual invoke for %s: %s", candybar_id, exc)
            return problem(
                503,
                "Event bus unavailable",
                "the bus did not confirm the invoke command; it may still run",
            )

        log.info(
            "manual invoke of %s (%s) accepted: invocation %s",
            name,
            candybar_id,
            command.data.invocation_id,
        )
        return JSONResponse(
            status_code=202,
            content={
                "candybarId": str(candybar_id),
                "name": name,
                "invocationId": str(command.data.invocation_id),
                "commandId": str(command.command_id),
                "idempotencyKey": command.idempotency_key,
                "trigger": command.data.trigger,
            },
        )

    @app.get("/invocations/{invocation_id}")
    async def invocation(invocation_id: UUID) -> JSONResponse:
        """The stored record behind an `invocation_id` exemplar."""
        async with session_factory() as session:
            row = await get_invocation(session, invocation_id)
            body = invocation_row_json(row) if row is not None else None
        if body is None:
            return problem(
                404, "Invocation not found", f"no recorded invocation with id {invocation_id}"
            )
        return JSONResponse(content=body)

    @app.get("/candybars/{candybar_id}/invocations")
    async def candybar_invocations(
        candybar_id: UUID, limit: int = Query(50, ge=1, le=INVOCATION_LIST_MAX)
    ) -> JSONResponse:
        """A candybar's recorded invocations, newest first."""
        async with session_factory() as session:
            if await get_candybar(session, candybar_id) is None:
                return problem(
                    404, "Candybar not found", f"no candybar registered with id {candybar_id}"
                )
            rows = await list_invocations(session, candybar_id, limit)
            body = [invocation_row_json(r) for r in rows]
        return JSONResponse(content={"candybarId": str(candybar_id), "invocations": body})

    @app.get("/schedule")
    async def schedule_window(
        from_: Annotated[AwareDatetime, Query(alias="from")],
        to: Annotated[AwareDatetime, Query()],
    ) -> JSONResponse:
        """Contract rows for every fire in `from <= fireTime < to`, ordered by fire time."""
        try:
            start, end = window_bounds(from_, to)
        except WindowError as exc:
            return problem(422, "Invalid schedule window", str(exc))
        instant = clock()
        async with session_factory() as session:
            candybars = [WindowCandybar.of(row) for row in await list_candybars(session)]
            # Day rows are candybar-local dates: read the widened window and
            # let each candybar's SkipSet (with its zone) decide precisely.
            skips = group_skips(
                await list_skips(session, *widen_skip_window(skip_read_start(start, instant), end)),
                {c.id: c.tz for c in candybars},
            )
        try:
            # croniter is CPU-bound and the api shares the daemon's event loop
            # with the heartbeat and dispatcher: compute off the loop.
            rows = await asyncio.to_thread(
                window_rows, candybars, start, end, now=instant, skips=skips
            )
        except WindowError as exc:
            return problem(422, "Invalid schedule window", str(exc))
        keys = past_keys(rows)
        if keys:
            async with session_factory() as session:
                rows = attach_results(rows, await fire_results(session, keys))
        return JSONResponse(content=[row.to_json() for row in visible_rows(rows)])

    @app.get("/schedule/next")
    async def schedule_next() -> JSONResponse:
        """The single `current` row (the poll fallback for the stream), or a 404.

        Bounded: one definitions read plus the skips at or after `cut` (never
        history, never invocations), then one `next_fire` lookup per candybar.
        """
        instant = clock()
        async with session_factory() as session:
            candybars = [WindowCandybar.of(row) for row in await list_candybars(session)]
            skips = group_skips(
                await list_skips(session, widen_skip_window(first_tick_after(instant), None)[0]),
                {c.id: c.tz for c in candybars},
            )
        row = await asyncio.to_thread(next_row, candybars, now=instant, skips=skips)
        if row is None:
            return problem(
                404, "No upcoming fire", "no registered candybar has an upcoming fire"
            )
        return JSONResponse(content=row.to_json())

    @app.get("/schedule/stream")
    async def schedule_stream(
        request: Request,
        from_: Annotated[AwareDatetime | None, Query(alias="from")] = None,
        to: Annotated[AwareDatetime | None, Query()] = None,
    ) -> Response:
        """SSE live updates (AD-11): row-shaped patches, resumable by `Last-Event-ID`.

        With a `from`/`to` window (validated as on `/schedule`), row events
        outside it are not dispatched and `definition.changed` carries the
        candybar's rows in it.
        """
        window = None
        if (from_ is None) != (to is None):
            return problem(422, "Invalid schedule window", "give both from and to, or neither")
        if from_ is not None and to is not None:
            try:
                window = window_bounds(from_, to)
            except WindowError as exc:
                return problem(422, "Invalid schedule window", str(exc))
        frames = live.frames(last_event_id=request.headers.get("last-event-id"), window=window)
        return StreamingResponse(frames, media_type=STREAM_MEDIA_TYPE, headers=STREAM_HEADERS)

    # ------------------------------------------------------------------
    # Steward contract (steward-contract.md): archive, profiles, tasks.

    @app.post("/candybars/{candybar_id}/archived")
    async def set_archived(candybar_id: UUID, body: ArchivedRequest) -> JSONResponse:
        """Archive (also disables, one write) or unarchive (stays disabled) a candybar."""
        async with session_factory() as session:
            row = await lock_candybar(session, candybar_id)
            if row is None:
                return problem(
                    404, "Candybar not found", f"no candybar registered with id {candybar_id}"
                )
            wanted = {"archived": True, "enabled": False} if body.archived else {"archived": False}
            changes = {k: v for k, v in wanted.items() if getattr(row, k, None) != v}
            if not changes:
                return JSONResponse(content=candybar_detail_json(row))
            event = build_candybar_updated(candybar_id=candybar_id, name=row.name, changes=changes)
            try:
                await sink.publish(event)
            except BUS_ERRORS as exc:
                log.error("failed to publish candybar.updated (archived) for %s: %s", candybar_id, exc)
                return problem(
                    503,
                    "Event bus unavailable",
                    "could not publish candybar.updated; the archived flag is unchanged",
                )
            row = await update_candybar(session, row, changes)
            result = candybar_detail_json(row)
        return JSONResponse(content=result)

    @app.get("/candybars/{candybar_id}/profile")
    async def profile_of(candybar_id: UUID) -> JSONResponse:
        """The candybar's profile, or an empty one (all nulls) when none exists yet."""
        async with session_factory() as session:
            if await get_candybar(session, candybar_id) is None:
                return problem(
                    404, "Candybar not found", f"no candybar registered with id {candybar_id}"
                )
            body = profile_json(candybar_id, await get_profile(session, candybar_id))
        return JSONResponse(content=body)

    @app.put("/candybars/{candybar_id}/profile")
    async def put_profile(candybar_id: UUID, body: ProfileUpdate) -> JSONResponse:
        """Merge per top-level key; `overview` and `evaluation` are contract-validated (422)."""
        changes = profile_changes(body)
        if not changes:
            return problem(422, "Invalid profile update", "no fields to update")
        try:
            async with session_factory() as session:
                if await get_candybar(session, candybar_id) is None:
                    return problem(
                        404, "Candybar not found", f"no candybar registered with id {candybar_id}"
                    )
                row = await upsert_profile(session, candybar_id, changes)
                result = profile_json(candybar_id, row)
        except UnknownCandybarError:
            return problem(404, "Candybar not found", f"no candybar registered with id {candybar_id}")
        return JSONResponse(content=result)

    @app.get("/profiles")
    async def profiles() -> JSONResponse:
        """Every stored profile, in one call for list views."""
        async with session_factory() as session:
            body = [profile_json(row.candybar_id, row) for row in await list_profiles(session)]
        return JSONResponse(content={"profiles": body})

    @app.post("/candybars/{candybar_id}/steward", status_code=202)
    async def request_steward(candybar_id: UUID, body: StewardRequest) -> JSONResponse:
        """Queue one steward task: command and `queued` fact first, then the row (202)."""
        action = body.action
        try:
            async with session_factory() as session:
                # The candybar lock serializes requests for one candybar, so the
                # active-task check below cannot race a twin request.
                row = await lock_candybar(session, candybar_id)
                if row is None:
                    return problem(
                        404, "Candybar not found", f"no candybar registered with id {candybar_id}"
                    )
                if action == "fix" and getattr(row, "archived", False):
                    return problem(
                        409,
                        "Candybar is archived",
                        f"{row.name} is archived; archived candybars may be enriched and "
                        "evaluated, not fixed",
                    )
                # One task per action; and a fix waits for any fix or refactor
                # (one change to a candybar's job at a time).
                for other in MUTATING_ACTIONS if action in MUTATING_ACTIONS else (action,):
                    active = await active_steward_task(session, candybar_id, other)
                    if active is not None:
                        return problem(
                            409,
                            "Steward task already active",
                            f"{row.name} already has a {active.status} {other} task",
                            taskId=str(active.id),
                        )
                task_id = uuid7()
                draft = DraftTask(
                    id=task_id,
                    candybar_id=candybar_id,
                    action=action,
                    model=MODEL_FOR_ACTION[action],
                    created_at=datetime.now(UTC),
                )
                command = build_candybar_steward(
                    task_id=task_id, candybar_id=candybar_id, action=action
                )
                fact = build_steward_task_fact(
                    STEWARD_FACT_FOR_STATUS["queued"], task_fact_data(draft, row.name)
                )
                try:
                    # The command first: a failed fact then leaves only a
                    # command for a task that was never stored, which the
                    # steward drops.
                    await sink.publish(command)
                    await sink.publish(fact)
                except BUS_ERRORS as exc:
                    log.error("failed to publish the steward command for %s: %s", candybar_id, exc)
                    return problem(
                        503,
                        "Event bus unavailable",
                        "could not publish the steward command; no task was queued",
                    )
                task = await insert_steward_task(
                    session,
                    task_id=task_id,
                    candybar_id=candybar_id,
                    action=action,
                    model=draft.model,
                    created_at=draft.created_at,
                )
                result = task_json(task)
        except DuplicateActiveTaskError:
            async with session_factory() as session:
                active = await active_steward_task(session, candybar_id, action)
            return problem(
                409,
                "Steward task already active",
                f"a {action} task is already active for {candybar_id}",
                taskId=str(active.id) if active is not None else None,
            )
        except UnknownCandybarError:
            return problem(404, "Candybar not found", f"no candybar registered with id {candybar_id}")
        log.info("steward %s of %s queued: task %s", action, candybar_id, result["id"])
        return JSONResponse(status_code=202, content=result)

    @app.get("/candybars/{candybar_id}/steward/tasks")
    async def candybar_tasks(
        candybar_id: UUID,
        limit: int = Query(CANDYBAR_TASKS_DEFAULT, ge=1, le=CANDYBAR_TASKS_MAX),
    ) -> JSONResponse:
        """A candybar's steward tasks, newest first."""
        async with session_factory() as session:
            if await get_candybar(session, candybar_id) is None:
                return problem(
                    404, "Candybar not found", f"no candybar registered with id {candybar_id}"
                )
            rows = await list_steward_tasks(session, candybar_id=candybar_id, limit=limit)
            body = [task_json(row) for row in rows]
        return JSONResponse(content={"candybarId": str(candybar_id), "tasks": body})

    @app.get("/steward/tasks")
    async def all_tasks(
        active: bool = False,
        limit: int = Query(ALL_TASKS_DEFAULT, ge=1, le=STEWARD_TASK_LIST_MAX),
    ) -> JSONResponse:
        """`active=1`: every queued and running task; else the newest `limit` across all."""
        async with session_factory() as session:
            rows = await list_steward_tasks(
                session, active=active, limit=STEWARD_TASK_LIST_MAX if active else limit
            )
            body = [task_json(row) for row in rows]
        return JSONResponse(content={"tasks": body})

    @app.post("/steward/tasks/{task_id}/cancel")
    async def cancel_task(task_id: UUID) -> JSONResponse:
        """Cancel a queued or running task: `failed` with error "cancelled by operator" (fact first).

        The fact correlates like the steward's own (a follow-up on its
        parent's chain). A follow-up's evaluation is settled too
        (`followUpStatus: failed`, `followUpNote: "cancelled by operator"`),
        so a cancelled queued refactor never reads "queued" forever. The
        steward notices a running task within a minute and stops the run;
        the CLI also interrupts the OpenCode session at once (the API
        cannot reach it).
        """
        async with session_factory() as session:
            task = await get_steward_task(session, task_id, lock=True)
            if task is None:
                return problem(404, "Steward task not found", f"no steward task with id {task_id}")
            if task.status in TERMINAL_STATUSES:
                return problem(
                    409, "Steward task already finished", f"task {task_id} is already {task.status}"
                )
            changes = stamp_transition(task.status, {"status": "failed", "error": CANCELLED})
            candybar = await get_candybar(session, task.candybar_id)
            name = candybar.name if candybar is not None else "unknown"
            fact = build_steward_task_fact(
                STEWARD_FACT_FOR_STATUS["failed"],
                task_fact_data(task, name, changes),
                correlationid=task.parent_task_id or task.id,
                causationid=task.parent_task_id,
            )
            try:
                await sink.publish(fact)
            except BUS_ERRORS as exc:
                log.error("failed to publish the failed fact of task %s: %s", task_id, exc)
                return problem(
                    503, "Event bus unavailable", "could not publish the failed fact; the task is unchanged"
                )
            task = await update_steward_task(session, task, changes)
            result = task_json(task)
            candybar_id = task.candybar_id
        # Its own transaction, after the task's (the steward locks the candybar,
        # never a task, before a profile write).
        try:
            async with session_factory() as session:
                if await lock_candybar(session, candybar_id) is not None:
                    stored = await get_profile(session, candybar_id)
                    settled = settled_evaluation(
                        getattr(stored, "evaluation", None), task_id, "failed", CANCELLED
                    )
                    if settled is not None:
                        await upsert_profile(session, candybar_id, {"evaluation": settled})
        except Exception:  # the cancel stands; a running task's steward settles it again as it stops
            log.exception("could not settle the follow-up of cancelled task %s", task_id)
        log.info("steward task %s cancelled by operator", task_id)
        return JSONResponse(content=result)

    @app.patch("/steward/tasks/{task_id}")
    async def patch_task(task_id: UUID, body: TaskPatchRequest) -> JSONResponse:
        """Update a task (worker-only by convention); a status move publishes its fact first."""
        patch = body.model_dump(exclude_unset=True)
        if not patch:
            return problem(422, "Invalid task update", "no fields to update")
        if "status" in patch and patch["status"] is None:
            return problem(422, "Invalid task update", "status cannot be null")
        changes = {TASK_COLUMNS[key]: value for key, value in patch.items()}
        async with session_factory() as session:
            task = await get_steward_task(session, task_id, lock=True)
            if task is None:
                return problem(404, "Steward task not found", f"no steward task with id {task_id}")
            status = changes.get("status", task.status)
            refusal = transition_problem(task.status, status)
            if refusal is not None:
                return problem(409, "Invalid task transition", refusal)
            changes = stamp_transition(task.status, changes)
            if status != task.status:
                candybar = await get_candybar(session, task.candybar_id)
                name = candybar.name if candybar is not None else "unknown"
                fact = build_steward_task_fact(
                    STEWARD_FACT_FOR_STATUS[status], task_fact_data(task, name, changes)
                )
                try:
                    await sink.publish(fact)
                except BUS_ERRORS as exc:
                    log.error("failed to publish the %s fact of task %s: %s", status, task_id, exc)
                    return problem(
                        503,
                        "Event bus unavailable",
                        f"could not publish the {status} fact; the task is unchanged",
                    )
            task = await update_steward_task(session, task, changes)
            result = task_json(task)
        return JSONResponse(content=result)

    return app
