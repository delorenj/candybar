"""Typed envelope builders for the locked `bloodbank.cron.*` vocabulary (AD-3).

Builders are the only way the daemon constructs cron envelopes: they derive
subject/dataschema/schemaref from the type, stamp the scheduler actor, and
enforce the trigger/tick coupling of the invoke command (AD-5, AD-12).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from . import vocabulary as voc
from .envelope import (
    CandybarCompletedData,
    CandybarCompletedEvent,
    CandybarCreatedData,
    CandybarCreatedEvent,
    CandybarFailedData,
    CandybarFailedEvent,
    CandybarInvokeCommand,
    CandybarInvokeData,
    CandybarInvokedData,
    CandybarInvokedEvent,
    CandybarSkippedData,
    CandybarSkippedEvent,
    CandybarUpdatedData,
    CandybarUpdatedEvent,
    FailureDetail,
    InvocationMetrics,
    TickFiredData,
    TickFiredEvent,
)

STDERR_TAIL_MAX = 500


def _root_lineage(event_id: UUID, correlationid: UUID | None) -> tuple[UUID, None]:
    """Root facts correlate on themselves and have no cause (§11)."""
    return (correlationid or event_id, None)


def build_tick_fired(
    minute: datetime,
    *,
    event_id: UUID | None = None,
    correlationid: UUID | None = None,
) -> TickFiredEvent:
    """Build the minute-heartbeat fact (AD-4).

    `minute` must be UTC and exactly on a minute boundary; the tick_id is
    derived from it, never from receipt time. The event id defaults to
    `tick_event_id(tick_id)` so broker dedupe keys on tick_id (AD-5); an
    explicit `event_id` still wins (tests that must not collide with the
    live heartbeat's minute).
    """
    if minute.tzinfo is None or minute.utcoffset() != UTC.utcoffset(None):
        raise ValueError("minute must be timezone-aware UTC")
    if minute.second != 0 or minute.microsecond != 0:
        raise ValueError(f"minute must be on a minute boundary, got {minute.isoformat()}")
    epoch_minute = int(minute.timestamp()) // 60
    tick_id = voc.tick_id_for_minute(epoch_minute)
    eid = event_id or voc.tick_event_id(tick_id)
    corr, cause = _root_lineage(eid, correlationid)
    return TickFiredEvent(
        id=eid,
        subject=voc.subject_for(voc.TYPE_TICK_FIRED, "event"),
        dataschema=voc.dataschema_for(voc.TYPE_TICK_FIRED),
        schemaref=voc.schemaref_for(voc.TYPE_TICK_FIRED),
        correlationid=corr,
        causationid=cause,
        ordering_key=tick_id,
        data=TickFiredData(tick_id=tick_id, minute=minute),
    )


def build_candybar_created(
    *,
    candybar_id: UUID,
    name: str,
    description: str,
    tags: list[str],
    schedule: str,
    noise: bool = False,
    correlationid: UUID | None = None,
    causationid: UUID | None = None,
) -> CandybarCreatedEvent:
    eid = uuid4()
    corr = correlationid or eid
    return CandybarCreatedEvent(
        id=eid,
        subject=voc.subject_for(voc.TYPE_CANDYBAR_CREATED, "event"),
        dataschema=voc.dataschema_for(voc.TYPE_CANDYBAR_CREATED),
        schemaref=voc.schemaref_for(voc.TYPE_CANDYBAR_CREATED),
        correlationid=corr,
        causationid=causationid,
        ordering_key=voc.candybar_ordering_key(candybar_id),
        data=CandybarCreatedData(
            candybar_id=candybar_id,
            name=name,
            description=description,
            tags=tags,
            schedule=schedule,
            noise=noise,
        ),
    )


def build_candybar_updated(
    *,
    candybar_id: UUID,
    name: str,
    changes: dict[str, object],
    correlationid: UUID | None = None,
    causationid: UUID | None = None,
) -> CandybarUpdatedEvent:
    eid = uuid4()
    return CandybarUpdatedEvent(
        id=eid,
        subject=voc.subject_for(voc.TYPE_CANDYBAR_UPDATED, "event"),
        dataschema=voc.dataschema_for(voc.TYPE_CANDYBAR_UPDATED),
        schemaref=voc.schemaref_for(voc.TYPE_CANDYBAR_UPDATED),
        correlationid=correlationid or eid,
        causationid=causationid,
        ordering_key=voc.candybar_ordering_key(candybar_id),
        data=CandybarUpdatedData(candybar_id=candybar_id, name=name, changes=dict(changes)),
    )


def build_candybar_invoked(
    *,
    candybar_id: UUID,
    name: str,
    invocation_id: UUID,
    trigger: str,
    tick_id: str | None,
    correlationid: UUID | None = None,
    causationid: UUID | None = None,
    event_id: UUID | None = None,
) -> CandybarInvokedEvent:
    eid = event_id or uuid4()
    return CandybarInvokedEvent(
        id=eid,
        subject=voc.subject_for(voc.TYPE_CANDYBAR_INVOKED, "event"),
        dataschema=voc.dataschema_for(voc.TYPE_CANDYBAR_INVOKED),
        schemaref=voc.schemaref_for(voc.TYPE_CANDYBAR_INVOKED),
        correlationid=correlationid or eid,
        causationid=causationid,
        ordering_key=voc.candybar_ordering_key(candybar_id),
        data=CandybarInvokedData(
            candybar_id=candybar_id,
            name=name,
            invocation_id=invocation_id,
            trigger=trigger,  # type: ignore[arg-type]
            tick_id=tick_id,
        ),
    )


def build_candybar_completed(
    *,
    candybar_id: UUID,
    name: str,
    invocation_id: UUID,
    trigger: str,
    tick_id: str | None,
    exit_code: int,
    runtime_ms: int,
    metrics: InvocationMetrics | None = None,
    correlationid: UUID | None = None,
    causationid: UUID | None = None,
    event_id: UUID | None = None,
) -> CandybarCompletedEvent:
    eid = event_id or uuid4()
    return CandybarCompletedEvent(
        id=eid,
        subject=voc.subject_for(voc.TYPE_CANDYBAR_COMPLETED, "event"),
        dataschema=voc.dataschema_for(voc.TYPE_CANDYBAR_COMPLETED),
        schemaref=voc.schemaref_for(voc.TYPE_CANDYBAR_COMPLETED),
        correlationid=correlationid or eid,
        causationid=causationid,
        ordering_key=voc.candybar_ordering_key(candybar_id),
        data=CandybarCompletedData(
            candybar_id=candybar_id,
            name=name,
            invocation_id=invocation_id,
            trigger=trigger,  # type: ignore[arg-type]
            tick_id=tick_id,
            exit_code=exit_code,
            runtime_ms=runtime_ms,
            metrics=metrics or InvocationMetrics(),
        ),
    )


def build_candybar_failed(
    *,
    candybar_id: UUID,
    name: str,
    invocation_id: UUID,
    trigger: str,
    tick_id: str | None,
    exit_code: int | None,
    runtime_ms: int | None = None,
    message: str | None = None,
    stderr: str = "",
    correlationid: UUID | None = None,
    causationid: UUID | None = None,
    event_id: UUID | None = None,
) -> CandybarFailedEvent:
    """Build the terminal failure fact; stderr is truncated to its last 500 chars."""
    eid = event_id or uuid4()
    detail = FailureDetail(
        message=message,
        stderr_tail=stderr[-STDERR_TAIL_MAX:] if stderr else "",
        redacted=True,
    )
    return CandybarFailedEvent(
        id=eid,
        subject=voc.subject_for(voc.TYPE_CANDYBAR_FAILED, "event"),
        dataschema=voc.dataschema_for(voc.TYPE_CANDYBAR_FAILED),
        schemaref=voc.schemaref_for(voc.TYPE_CANDYBAR_FAILED),
        correlationid=correlationid or eid,
        causationid=causationid,
        ordering_key=voc.candybar_ordering_key(candybar_id),
        data=CandybarFailedData(
            candybar_id=candybar_id,
            name=name,
            invocation_id=invocation_id,
            trigger=trigger,  # type: ignore[arg-type]
            tick_id=tick_id,
            exit_code=exit_code,
            runtime_ms=runtime_ms,
            error=detail,
        ),
    )


def build_candybar_skipped(
    *,
    candybar_id: UUID,
    name: str,
    tick_id: str | None,
    reason: str,
    correlationid: UUID | None = None,
    causationid: UUID | None = None,
) -> CandybarSkippedEvent:
    eid = uuid4()
    return CandybarSkippedEvent(
        id=eid,
        subject=voc.subject_for(voc.TYPE_CANDYBAR_SKIPPED, "event"),
        dataschema=voc.dataschema_for(voc.TYPE_CANDYBAR_SKIPPED),
        schemaref=voc.schemaref_for(voc.TYPE_CANDYBAR_SKIPPED),
        correlationid=correlationid or eid,
        causationid=causationid,
        ordering_key=voc.candybar_ordering_key(candybar_id),
        data=CandybarSkippedData(
            candybar_id=candybar_id, name=name, tick_id=tick_id, reason=reason
        ),
    )


def build_candybar_invoke(
    *,
    candybar_id: UUID,
    name: str,
    trigger: str,
    tick_id: str | None = None,
    invocation_id: UUID | None = None,
    command_id: UUID | None = None,
    event_id: UUID | None = None,
    issued_at: datetime | None = None,
) -> CandybarInvokeCommand:
    """Build the dispatch command (AD-5, AD-12).

    scheduled/misfire dispatches require the causing tick_id; manual invokes
    carry no tick. Idempotency keys follow the spine:
    `candybar:<id>:tick:<tick_id>` (scheduled/misfire),
    `candybar:<id>:manual:<command_id>` (manual, stable across retries).
    Root-issued: correlationid == command_id.
    """
    cmd_id = command_id or uuid4()
    if trigger in ("scheduled", "misfire"):
        if tick_id is None:
            raise ValueError(f"trigger {trigger!r} requires a tick_id")
        if not voc.TICK_ID_RE.match(tick_id):
            raise ValueError(f"invalid tick_id: {tick_id!r}")
        idempotency_key = f"candybar:{candybar_id}:tick:{tick_id}"
    elif trigger == "manual":
        if tick_id is not None:
            raise ValueError("manual invokes carry no tick_id")
        idempotency_key = f"candybar:{candybar_id}:manual:{cmd_id}"
    else:
        raise ValueError(f"unknown trigger: {trigger!r} (expected one of {sorted(voc.TRIGGERS)})")
    return CandybarInvokeCommand(
        id=event_id or uuid4(),
        **({"time": issued_at} if issued_at is not None else {}),
        subject=voc.subject_for(voc.TYPE_CANDYBAR_INVOKE, "command"),
        dataschema=voc.dataschema_for(voc.TYPE_CANDYBAR_INVOKE),
        schemaref=voc.schemaref_for(voc.TYPE_CANDYBAR_INVOKE),
        correlationid=cmd_id,
        causationid=None,
        command_id=cmd_id,
        idempotency_key=idempotency_key,
        delivery="single_consumer",
        data=CandybarInvokeData(
            candybar_id=candybar_id,
            name=name,
            invocation_id=invocation_id or uuid4(),
            trigger=trigger,  # type: ignore[arg-type]
            tick_id=tick_id,
        ),
    )
