"""Parse inbound `bloodbank.cron.*` envelopes into typed models (AD-3)."""

from __future__ import annotations

from typing import Any

from . import vocabulary as voc
from .envelope import (
    CandybarCompletedEvent,
    CandybarCreatedEvent,
    CandybarFailedEvent,
    CandybarInvokeCommand,
    CandybarInvokedEvent,
    CandybarSkippedEvent,
    CandybarUpdatedEvent,
    Envelope,
    TickFiredEvent,
)

_MODEL_BY_TYPE: dict[str, type[Envelope]] = {
    voc.TYPE_TICK_FIRED: TickFiredEvent,
    voc.TYPE_CANDYBAR_CREATED: CandybarCreatedEvent,
    voc.TYPE_CANDYBAR_UPDATED: CandybarUpdatedEvent,
    voc.TYPE_CANDYBAR_INVOKED: CandybarInvokedEvent,
    voc.TYPE_CANDYBAR_COMPLETED: CandybarCompletedEvent,
    voc.TYPE_CANDYBAR_FAILED: CandybarFailedEvent,
    voc.TYPE_CANDYBAR_SKIPPED: CandybarSkippedEvent,
    voc.TYPE_CANDYBAR_INVOKE: CandybarInvokeCommand,
}


def parse_envelope(raw: dict[str, Any]) -> Envelope:
    """Validate a wire dict into its typed envelope model.

    Raises TypeError for a missing/non-string `type`, ValueError for an
    unknown `type`; pydantic ValidationError for a known type whose envelope
    fails the contract (subject/kind marker mismatch, tense violation, bad
    payload, ...).
    """
    type_ = raw.get("type")
    if not isinstance(type_, str):
        raise TypeError("envelope has no string 'type'")
    model = _MODEL_BY_TYPE.get(type_)
    if model is None:
        raise ValueError(f"type {type_!r} is not in the locked cron vocabulary")
    return model.model_validate(raw)
