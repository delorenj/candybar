"""FastAPI app factory for the candybar API role.

Story 1.4 scope: `POST /candybars` (register) only. The flow follows the
spine's state rule — the `bloodbank.cron.candybar.created` fact is published
FIRST, then the registry row commits in the same request flow; a publish
failure means no row (503), a unique-violation race maps to 409.
All errors are RFC 7807 problem+json. Epic 5 adds the CAP-8 read surface.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import nats.errors
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from candybar.events.builders import build_candybar_created
from candybar.registry import (
    CandybarManifest,
    DuplicateNameError,
    insert_candybar,
    name_taken,
    uuid7,
)

log = logging.getLogger(__name__)

PROBLEM_JSON = "application/problem+json"

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
        "createdAt": row.created_at.isoformat(),
    }


def create_app(*, session_factory: SessionFactory, sink: Any) -> FastAPI:
    app = FastAPI(title="candybar", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(RequestValidationError)
    async def _validation_problem(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem(422, "Invalid request body", str(exc.errors())[:500])

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
                except (nats.errors.Error, OSError, TimeoutError) as exc:
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

    return app
