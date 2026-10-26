"""Envelope models for the Bloodbank `cron` vocabulary (AD-3).

Pydantic models are the typed contract: builders construct them, parsers
validate inbound dicts into them. Field shapes mirror
`bloodbank/schemas/_common/cloudevent_base.v1.json` plus the naming
contract's required extension fields (kind, actor, ordering_key for events;
command_id / idempotency_key / delivery for commands).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from . import vocabulary as voc

CandybarName = Annotated[str, StringConstraints(pattern=voc.CANDYBAR_NAME_RE.pattern)]
TickId = Annotated[str, StringConstraints(pattern=voc.TICK_ID_RE.pattern)]
NonBlank = Annotated[str, StringConstraints(min_length=1)]

PRODUCER = "candybar"
SERVICE = "candybar"
SOURCE = "urn:33god:service:candybar"


class Actor(BaseModel):
    """Contract §10 actor object; candybar always acts as the scheduler."""

    model_config = ConfigDict(extra="allow")

    type: NonBlank = "scheduler"
    agent_id: NonBlank = "candybar.scheduler"
    cli: str | None = None
    provider: str | None = None
    model: str | None = None


DEFAULT_ACTOR = Actor()


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Envelope(BaseModel):
    """CloudEvents 1.0 + Bloodbank extensions common to every cron message."""

    specversion: Literal["1.0"] = "1.0"
    id: UUID = Field(default_factory=uuid4)
    source: NonBlank = SOURCE
    type: str
    subject: NonBlank
    time: AwareDatetime = Field(default_factory=_utcnow)
    datacontenttype: Literal["application/json"] = "application/json"
    dataschema: str
    schemaref: NonBlank
    correlationid: UUID
    causationid: UUID | None = None
    producer: NonBlank = PRODUCER
    service: NonBlank = SERVICE
    domain: Literal["cron"] = "cron"
    kind: str
    actor: Actor = DEFAULT_ACTOR
    data: Any

    @model_validator(mode="after")
    def _contract_consistency(self) -> Envelope:
        tokens = voc.type_tokens(self.type)
        if tokens[1] != voc.DOMAIN:
            raise ValueError(f"type {self.type!r} is not in the cron domain")
        voc.validate_tense(self.type, self.kind)
        expected_subject = voc.subject_for(self.type, self.kind)
        if self.subject != expected_subject:
            raise ValueError(
                f"subject {self.subject!r} != derived {expected_subject!r} for kind {self.kind!r}"
            )
        if self.dataschema != voc.dataschema_for(self.type):
            raise ValueError(f"dataschema does not match type {self.type!r}")
        if self.schemaref != voc.schemaref_for(self.type):
            raise ValueError(f"schemaref does not match type {self.type!r}")
        return self


class EventEnvelope(Envelope):
    """kind=event: immutable fan-out fact with an ordering bucket (§11)."""

    kind: Literal["event"] = "event"
    ordering_key: NonBlank


class CommandEnvelope(Envelope):
    """kind=command: single-consumer work (§4, §11.2)."""

    kind: Literal["command"] = "command"
    command_id: UUID = Field(default_factory=uuid4)
    idempotency_key: NonBlank
    delivery: Literal["single_consumer"] = "single_consumer"


# --------------------------------------------------------------------------
# Data payloads (mirror bloodbank/schemas/bloodbank/cron/*.json)
# --------------------------------------------------------------------------


class TickFiredData(BaseModel):
    schema_version: Literal[1] = 1
    tick_id: TickId
    minute: AwareDatetime


class CandybarCreatedData(BaseModel):
    schema_version: Literal[1] = 1
    candybar_id: UUID
    name: CandybarName
    description: str
    tags: list[NonBlank]
    schedule: NonBlank
    noise: bool = False


class CandybarUpdatedData(BaseModel):
    schema_version: Literal[1] = 1
    candybar_id: UUID
    name: CandybarName
    changes: dict[str, Any] = Field(min_length=1)


class InvocationIdentity(BaseModel):
    """Fields shared by every invocation-lifecycle payload."""

    schema_version: Literal[1] = 1
    candybar_id: UUID
    name: CandybarName
    invocation_id: UUID
    trigger: Literal["scheduled", "manual", "misfire"]
    tick_id: TickId | None


class CandybarInvokedData(InvocationIdentity):
    pass


TOKEN_SPEND_MAX = 2**63 - 1  # invocations.token_spend is a Postgres BIGINT


class SelfReportedMetrics(BaseModel):
    """What a candybar may report about itself in its AD-8 result file."""

    model_config = ConfigDict(extra="forbid")

    # Bounded to what the registry stores (BIGINT) and JSON can carry (finite):
    # an out-of-range value is an invalid result file, never a failed insert
    # (which would nak and re-execute) or an unserializable API record.
    token_spend: int = Field(default=0, ge=0, le=TOKEN_SPEND_MAX)
    cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class InvocationMetrics(SelfReportedMetrics):
    """The per-invocation record on completed/failed events (AD-9).

    Adds the executor-measured values: cgroup memory/CPU (Story 3.1) and
    nvidia-smi pmon GPU load (Story 3.2); 0 when unmeasured.
    """

    memory_peak_bytes: int = Field(default=0, ge=0)
    cpu_load: float = Field(default=0.0, ge=0)
    gpu_load: float = Field(default=0.0, ge=0)


class CandybarCompletedData(InvocationIdentity):
    exit_code: int = Field(ge=0)
    runtime_ms: int = Field(ge=0)
    metrics: InvocationMetrics = Field(default_factory=InvocationMetrics)


class FailureDetail(BaseModel):
    """Redacted failure summary — spine error posture: tail <= 500 chars, marked."""

    model_config = ConfigDict(extra="forbid")

    message: str | None = Field(default=None, max_length=500)
    stderr_tail: str = Field(default="", max_length=500)
    redacted: Literal[True] = True


class CandybarFailedData(InvocationIdentity):
    exit_code: int | None
    runtime_ms: int | None = Field(default=None, ge=0)
    error: FailureDetail
    metrics: InvocationMetrics = Field(default_factory=InvocationMetrics)


class CandybarSkippedData(BaseModel):
    schema_version: Literal[1] = 1
    candybar_id: UUID
    name: CandybarName
    tick_id: TickId | None
    reason: NonBlank


class CandybarInvokeData(BaseModel):
    schema_version: Literal[1] = 1
    candybar_id: UUID
    name: CandybarName
    invocation_id: UUID
    trigger: Literal["scheduled", "manual", "misfire"]
    tick_id: TickId | None


# --------------------------------------------------------------------------
# Typed envelopes — one per locked type
# --------------------------------------------------------------------------


class TickFiredEvent(EventEnvelope):
    type: Literal["bloodbank.cron.tick.fired"] = voc.TYPE_TICK_FIRED  # type: ignore[assignment]
    data: TickFiredData


class CandybarCreatedEvent(EventEnvelope):
    type: Literal["bloodbank.cron.candybar.created"] = voc.TYPE_CANDYBAR_CREATED  # type: ignore[assignment]
    data: CandybarCreatedData


class CandybarUpdatedEvent(EventEnvelope):
    type: Literal["bloodbank.cron.candybar.updated"] = voc.TYPE_CANDYBAR_UPDATED  # type: ignore[assignment]
    data: CandybarUpdatedData


class CandybarInvokedEvent(EventEnvelope):
    type: Literal["bloodbank.cron.candybar.invoked"] = voc.TYPE_CANDYBAR_INVOKED  # type: ignore[assignment]
    data: CandybarInvokedData


class CandybarCompletedEvent(EventEnvelope):
    type: Literal["bloodbank.cron.candybar.completed"] = voc.TYPE_CANDYBAR_COMPLETED  # type: ignore[assignment]
    data: CandybarCompletedData


class CandybarFailedEvent(EventEnvelope):
    type: Literal["bloodbank.cron.candybar.failed"] = voc.TYPE_CANDYBAR_FAILED  # type: ignore[assignment]
    data: CandybarFailedData


class CandybarSkippedEvent(EventEnvelope):
    type: Literal["bloodbank.cron.candybar.skipped"] = voc.TYPE_CANDYBAR_SKIPPED  # type: ignore[assignment]
    data: CandybarSkippedData


class CandybarInvokeCommand(CommandEnvelope):
    type: Literal["bloodbank.cron.candybar.invoke"] = voc.TYPE_CANDYBAR_INVOKE  # type: ignore[assignment]
    data: CandybarInvokeData
