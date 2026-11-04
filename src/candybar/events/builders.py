"""Typed envelope builders for the locked `bloodbank.cron.*` vocabulary (AD-3).

Builders are the only way the daemon constructs cron envelopes: they derive
subject/dataschema/schemaref from the type, stamp the scheduler actor, and
enforce the trigger/tick coupling of the invoke command (AD-5, AD-12).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

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
    CandybarStewardCommand,
    CandybarStewardData,
    CandybarUpdatedData,
    CandybarUpdatedEvent,
    FailureDetail,
    InvocationMetrics,
    StewardTaskCompletedEvent,
    StewardTaskData,
    StewardTaskFailedEvent,
    StewardTaskQueuedEvent,
    StewardTaskStartedEvent,
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
    timezone: str = "UTC",
    enabled: bool = True,
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
            timezone=timezone,
            noise=noise,
            enabled=enabled,
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
    metrics: InvocationMetrics | None = None,
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
            metrics=metrics or InvocationMetrics(),
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
    event_id: UUID | None = None,
) -> CandybarSkippedEvent:
    """Build the suppressed-fire fact. A deterministic `event_id` (e.g. the
    dispatcher's misfire coalescing) makes republication broker-dedupable."""
    eid = event_id or uuid4()
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
        idempotency_key = voc.scheduled_idempotency_key(candybar_id, tick_id)
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


# --------------------------------------------------------------------------
# Steward (steward-contract.md sections 3-4).
# --------------------------------------------------------------------------

# Task-fact ids are UUIDv5 of `<type>:<task_id>`: one fact per transition, so
# a republication (a retried write, a redelivered command) dedupes on the
# broker's Nats-Msg-Id.
STEWARD_NAMESPACE = UUID("3a7d2c1e-6b4f-4f0e-9c8a-5d2e1b0f7a64")

_STEWARD_FACT_MODELS = {
    voc.TYPE_STEWARD_TASK_QUEUED: StewardTaskQueuedEvent,
    voc.TYPE_STEWARD_TASK_STARTED: StewardTaskStartedEvent,
    voc.TYPE_STEWARD_TASK_COMPLETED: StewardTaskCompletedEvent,
    voc.TYPE_STEWARD_TASK_FAILED: StewardTaskFailedEvent,
}

# The fact a task status is announced with.
STEWARD_FACT_FOR_STATUS = {
    "queued": voc.TYPE_STEWARD_TASK_QUEUED,
    "running": voc.TYPE_STEWARD_TASK_STARTED,
    "succeeded": voc.TYPE_STEWARD_TASK_COMPLETED,
    "failed": voc.TYPE_STEWARD_TASK_FAILED,
}


def steward_fact_id(type_: str, task_id: UUID) -> UUID:
    return uuid5(STEWARD_NAMESPACE, f"{type_}:{task_id}")


def build_steward_task_fact(
    type_: str,
    data: StewardTaskData,
    *,
    correlationid: UUID | None = None,
    causationid: UUID | None = None,
) -> StewardTaskQueuedEvent | StewardTaskStartedEvent | StewardTaskCompletedEvent | StewardTaskFailedEvent:
    """Build one task lifecycle fact (`queued|started|completed|failed`) carrying the task.

    Correlates on the task id (a follow-up passes its evaluate's), ordered
    with every other fact of the candybar.
    """
    model = _STEWARD_FACT_MODELS.get(type_)
    if model is None:
        raise ValueError(f"not a steward task fact: {type_!r}")
    eid = steward_fact_id(type_, data.task_id)
    return model(
        id=eid,
        subject=voc.subject_for(type_, "event"),
        dataschema=voc.dataschema_for(type_),
        schemaref=voc.schemaref_for(type_),
        correlationid=correlationid or data.task_id,
        causationid=causationid,
        ordering_key=voc.candybar_ordering_key(data.candybar_id),
        data=data,
    )


def build_candybar_steward(
    *,
    task_id: UUID,
    candybar_id: UUID,
    action: str,
    correlationid: UUID | None = None,
    causationid: UUID | None = None,
) -> CandybarStewardCommand:
    """Build the steward command for one task (contract section 3).

    The task id is the envelope id, the command id and the idempotency key,
    so a re-sent publish dedupes on Nats-Msg-Id and a redelivery names the
    same task.
    """
    return CandybarStewardCommand(
        id=task_id,
        subject=voc.subject_for(voc.TYPE_CANDYBAR_STEWARD, "command"),
        dataschema=voc.dataschema_for(voc.TYPE_CANDYBAR_STEWARD),
        schemaref=voc.schemaref_for(voc.TYPE_CANDYBAR_STEWARD),
        correlationid=correlationid or task_id,
        causationid=causationid,
        command_id=task_id,
        idempotency_key=str(task_id),
        delivery="single_consumer",
        data=CandybarStewardData(
            task_id=task_id,
            candybar_id=candybar_id,
            action=action,  # type: ignore[arg-type]
        ),
    )
