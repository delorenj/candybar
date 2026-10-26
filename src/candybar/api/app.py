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
All errors are RFC 7807 problem+json. Epic 5 adds the rest of the CAP-8 read surface.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import nats.errors
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import REGISTRY, CollectorRegistry
from prometheus_client.exposition import choose_encoder
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from candybar.events.builders import build_candybar_created, build_candybar_invoke
from candybar.registry import (
    INVOCATION_LIST_MAX,
    CandybarManifest,
    DuplicateNameError,
    get_candybar,
    get_invocation,
    insert_candybar,
    list_invocations,
    name_taken,
    uuid7,
)

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
    entrypoint: str
    noise: bool = False

    def to_manifest(self) -> CandybarManifest:
        # Reuses the manifest validators (slug name, 5-field cron); entrypoint
        # existence is a CLI-side check — the API stores the resolved path.
        return CandybarManifest.model_validate(self.model_dump())


def problem(status: int, title: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type=PROBLEM_JSON,
        content={
            "type": "about:blank",
            "title": title,
            "status": status,
            "detail": detail,
        },
    )


def utc_iso(value: datetime | None) -> str | None:
    """ISO 8601 in UTC (AD-13). timestamptz comes back in the DB session's
    zone (the live server's is America/New_York), so convert explicitly."""
    return value.astimezone(UTC).isoformat() if value is not None else None


def candybar_row_json(row: Any) -> dict[str, Any]:
    """The holocene-view-contract row shape (+ entrypoint), camelCase dates."""
    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description,
        "tags": list(row.tags),
        "schedule": row.schedule,
        "entrypoint": row.entrypoint,
        "noise": row.noise,
        "createdAt": utc_iso(row.created_at),
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
) -> FastAPI:
    app = FastAPI(title="candybar", docs_url=None, redoc_url=None, openapi_url=None)

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

        if not Path(manifest.entrypoint).is_file():
            return problem(
                422,
                "Invalid candybar definition",
                f"entrypoint is not an existing file: {manifest.entrypoint!r}",
            )

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
                    noise=manifest.noise,
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

        return JSONResponse(status_code=201, content=candybar_row_json(row))

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

    return app
