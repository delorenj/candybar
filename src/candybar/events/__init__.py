"""Events: envelope builders/parsers for the bloodbank.cron.* vocabulary (AD-3).

The cron domain is registered in bloodbank/docs/event-naming.md with canonical
schemas at bloodbank/schemas/bloodbank/cron/ (drafts in this repo's
schemas/cron/). Builders construct typed pydantic envelopes; parse_envelope
validates wire dicts back into them.
"""

from .builders import (
    STDERR_TAIL_MAX,
    build_candybar_completed,
    build_candybar_created,
    build_candybar_failed,
    build_candybar_invoke,
    build_candybar_invoked,
    build_candybar_skipped,
    build_candybar_updated,
    build_tick_fired,
)
from .envelope import (
    Actor,
    CandybarInvokeCommand,
    CommandEnvelope,
    Envelope,
    EventEnvelope,
    FailureDetail,
    InvocationMetrics,
)
from .parse import parse_envelope
from .vocabulary import (
    ALL_TYPES,
    COMMAND_TYPES,
    DOMAIN,
    EVENT_TYPES,
    TYPE_CANDYBAR_COMPLETED,
    TYPE_CANDYBAR_CREATED,
    TYPE_CANDYBAR_FAILED,
    TYPE_CANDYBAR_INVOKE,
    TYPE_CANDYBAR_INVOKED,
    TYPE_CANDYBAR_SKIPPED,
    TYPE_CANDYBAR_UPDATED,
    TYPE_TICK_FIRED,
    tick_event_id,
)

__all__ = [
    "ALL_TYPES",
    "COMMAND_TYPES",
    "DOMAIN",
    "EVENT_TYPES",
    "STDERR_TAIL_MAX",
    "TYPE_CANDYBAR_COMPLETED",
    "TYPE_CANDYBAR_CREATED",
    "TYPE_CANDYBAR_FAILED",
    "TYPE_CANDYBAR_INVOKE",
    "TYPE_CANDYBAR_INVOKED",
    "TYPE_CANDYBAR_SKIPPED",
    "TYPE_CANDYBAR_UPDATED",
    "TYPE_TICK_FIRED",
    "Actor",
    "CandybarInvokeCommand",
    "CommandEnvelope",
    "Envelope",
    "EventEnvelope",
    "FailureDetail",
    "InvocationMetrics",
    "build_candybar_completed",
    "build_candybar_created",
    "build_candybar_failed",
    "build_candybar_invoke",
    "build_candybar_invoked",
    "build_candybar_skipped",
    "build_candybar_updated",
    "build_tick_fired",
    "parse_envelope",
    "tick_event_id",
]
