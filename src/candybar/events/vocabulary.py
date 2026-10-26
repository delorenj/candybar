"""Locked Bloodbank `cron` vocabulary (AD-3).

The single source of truth for every type, subject, and schema reference the
daemon may emit. Registered in `bloodbank/docs/event-naming.md` with canonical
schemas at `bloodbank/schemas/bloodbank/cron/`; drafts mirror them in this
repo's `schemas/cron/`.
"""

from __future__ import annotations

import re
from uuid import UUID, uuid5

DOMAIN = "cron"

ENTITY_TICK = "tick"
ENTITY_CANDYBAR = "candybar"

ACTION_FIRED = "fired"
ACTION_CREATED = "created"
ACTION_UPDATED = "updated"
ACTION_INVOKED = "invoked"
ACTION_COMPLETED = "completed"
ACTION_FAILED = "failed"
ACTION_SKIPPED = "skipped"
ACTION_INVOKE = "invoke"

TYPE_TICK_FIRED = f"bloodbank.{DOMAIN}.{ENTITY_TICK}.{ACTION_FIRED}"
TYPE_CANDYBAR_CREATED = f"bloodbank.{DOMAIN}.{ENTITY_CANDYBAR}.{ACTION_CREATED}"
TYPE_CANDYBAR_UPDATED = f"bloodbank.{DOMAIN}.{ENTITY_CANDYBAR}.{ACTION_UPDATED}"
TYPE_CANDYBAR_INVOKED = f"bloodbank.{DOMAIN}.{ENTITY_CANDYBAR}.{ACTION_INVOKED}"
TYPE_CANDYBAR_COMPLETED = f"bloodbank.{DOMAIN}.{ENTITY_CANDYBAR}.{ACTION_COMPLETED}"
TYPE_CANDYBAR_FAILED = f"bloodbank.{DOMAIN}.{ENTITY_CANDYBAR}.{ACTION_FAILED}"
TYPE_CANDYBAR_SKIPPED = f"bloodbank.{DOMAIN}.{ENTITY_CANDYBAR}.{ACTION_SKIPPED}"
TYPE_CANDYBAR_INVOKE = f"bloodbank.{DOMAIN}.{ENTITY_CANDYBAR}.{ACTION_INVOKE}"

EVENT_TYPES: frozenset[str] = frozenset(
    {
        TYPE_TICK_FIRED,
        TYPE_CANDYBAR_CREATED,
        TYPE_CANDYBAR_UPDATED,
        TYPE_CANDYBAR_INVOKED,
        TYPE_CANDYBAR_COMPLETED,
        TYPE_CANDYBAR_FAILED,
        TYPE_CANDYBAR_SKIPPED,
    }
)
COMMAND_TYPES: frozenset[str] = frozenset({TYPE_CANDYBAR_INVOKE})
ALL_TYPES: frozenset[str] = EVENT_TYPES | COMMAND_TYPES

# Contract §5 tense: events past participle, commands imperative present.
EVENT_ACTIONS: frozenset[str] = frozenset(
    {
        ACTION_FIRED,
        ACTION_CREATED,
        ACTION_UPDATED,
        ACTION_INVOKED,
        ACTION_COMPLETED,
        ACTION_FAILED,
        ACTION_SKIPPED,
    }
)
COMMAND_ACTIONS: frozenset[str] = frozenset({ACTION_INVOKE})

_TYPE_RE = re.compile(r"^bloodbank\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_SUBJECT_RE = re.compile(
    r"^bloodbank\.(evt|cmd|rpy)\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"
)

KIND_MARKERS = {"event": "evt", "command": "cmd", "reply": "rpy"}

TICK_ID_RE = re.compile(r"^tick:[0-9]+$")
CANDYBAR_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

TRIGGERS: frozenset[str] = frozenset({"scheduled", "manual", "misfire"})


def type_tokens(type_: str) -> list[str]:
    """Split and validate a 4-token contract type."""
    if not _TYPE_RE.match(type_):
        raise ValueError(f"invalid contract type: {type_!r}")
    return type_.split(".")


def subject_for(type_: str, kind: str) -> str:
    """Derive the 5-token NATS subject: bloodbank.<kind-marker>.<domain>.<entity>.<action>."""
    tokens = type_tokens(type_)
    try:
        marker = KIND_MARKERS[kind]
    except KeyError:
        raise ValueError(f"unknown envelope kind: {kind!r}") from None
    return ".".join(["bloodbank", marker, *tokens[1:]])


def schemaref_for(type_: str) -> str:
    """Short schema reference (§13): '<type>.v1'."""
    type_tokens(type_)
    return f"{type_}.v1"


def dataschema_for(type_: str) -> str:
    """Apicurio registry URI (§13): 'apicurio://holyfields/<type>/versions/1'."""
    type_tokens(type_)
    return f"apicurio://holyfields/{type_}/versions/1"


def action_of(type_: str) -> str:
    return type_tokens(type_)[3]


def validate_tense(type_: str, kind: str) -> None:
    """Reject an event whose action is imperative or a command whose action is past (§5)."""
    action = action_of(type_)
    if kind == "event" and action in COMMAND_ACTIONS:
        raise ValueError(f"event type {type_!r} uses imperative action {action!r}")
    if kind == "command" and action in EVENT_ACTIONS:
        raise ValueError(f"command type {type_!r} uses past-tense action {action!r}")


def tick_id_for_minute(epoch_minute: int) -> str:
    """Deterministic tick identity (AD-4): tick:<unix_epoch_minute>."""
    if epoch_minute < 0:
        raise ValueError("epoch_minute must be non-negative")
    return f"tick:{epoch_minute}"


# Tick event ids are UUIDv5 of the tick_id (AD-4/AD-5): the CloudEvent id, and
# so the JetStream `Nats-Msg-Id`, is a pure function of the minute. A re-built
# or re-published tick for the same minute dedupes on the broker by tick_id.
TICK_NAMESPACE = UUID("5f0c8c3e-9a51-4c43-b1d6-2e7a0f4b9d17")


def tick_event_id(tick_id: str) -> UUID:
    """Deterministic CloudEvent id for a tick fact, keyed on tick_id only."""
    if not TICK_ID_RE.match(tick_id):
        raise ValueError(f"invalid tick_id {tick_id!r}")
    return uuid5(TICK_NAMESPACE, tick_id)


def candybar_ordering_key(candybar_id: object) -> str:
    return f"candybar:{candybar_id}"
