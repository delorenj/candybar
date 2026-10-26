"""Live schedule updates over SSE: `GET /schedule/stream` (Story 5.4; AD-11).

holocene-view-contract.md "Live updates": an event whenever an invocation
starts, an invocation completes (with result), a skip is applied, a
definition changes, or the current row advances, each carrying enough of the
`/schedule` row shape to patch the view in place without a refetch.

The api role runs one `ScheduleStream` hub. It is fed by an ephemeral,
ordered JetStream consumer on `bloodbank.evt.cron.>` (`api/role.py`); live
view updates are consumers of bus facts (spine paradigm). Ticks are its only
clock (AD-1), and it dedupes them on `tick_id` (AD-5). It never acks,
publishes or evaluates dispatch. Each fact becomes at most two stream events
(a definition change can also move the current row), and every row in them
comes from `api/schedule.py`'s row computation, so it is the row a refetch
of `/schedule` would return for that fire.

Delivery: each event gets the id `<boot>-<n>`, with `n` monotonic within one
hub. Events are kept in a bounded ring buffer, and a reconnect whose
`Last-Event-ID` is still in the buffer replays exactly the events after it.
Any other id gets a `reset`: the client refetches `/schedule`. Payloads are
absolute state under a stable patch `key`, so applying one twice changes
nothing. `seq` counts within one boot only: after a `reset` the client also
forgets the seqs it applied, since a restarted hub counts from 1 again. The
stream event names are API-level only, NOT Bloodbank types.

Client patch rule: on `current.advanced`, every row with `fireTime < cut` is
`past`, the row matching `current` is `current`, and the rest are
`upcoming`. On a row event, set the row at its key. On `definition.changed`,
set `name`, `schedule` and `noise` on that candybar's rows, or replace them
with `rows` when the stream was opened with a window.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from ..dispatcher.schedule import NO_SKIPS, group_skips
from ..events import parse_envelope
from ..events import vocabulary as voc
from ..events.envelope import Envelope
from ..registry import (
    SKIP_KINDS,
    fire_results,
    get_candybar,
    list_candybars,
    list_skips,
)
from .schedule import (
    CurrentKey,
    WindowCandybar,
    attach_results,
    candybar_rows,
    first_tick_after,
    is_fire_of,
    next_row,
    past_keys,
    schedule_row,
    tick_minute,
)
from .shapes import candybar_row_json, utc_iso

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[Any]]

EVENT_INVOCATION_STARTED = "invocation.started"
EVENT_INVOCATION_COMPLETED = "invocation.completed"
EVENT_SKIP_APPLIED = "skip.applied"
EVENT_DEFINITION_CHANGED = "definition.changed"
EVENT_CURRENT_ADVANCED = "current.advanced"
EVENT_READY = "ready"  # a fresh connection: the resume cursor and the current row
EVENT_RESET = "reset"  # the client must refetch /schedule

# The contract's "Live updates" triggers, verbatim, and the event each one is.
# tests/contract_shapes.py parses the phrases out of the contract file.
STREAM_TRIGGERS: dict[str, str] = {
    "an invocation starts": EVENT_INVOCATION_STARTED,
    "an invocation completes (with result)": EVENT_INVOCATION_COMPLETED,
    "a skip is applied": EVENT_SKIP_APPLIED,
    "a definition changes": EVENT_DEFINITION_CHANGED,
    'the "current" row advances': EVENT_CURRENT_ADVANCED,
}

# The contract definition fields a `definition.changed` may report: the
# entrypoint is executor-internal and is never echoed (Story 5.1).
VISIBLE_FIELDS = frozenset({"name", "description", "tags", "schedule", "noise"})

BUFFER_SIZE = 1024  # events kept for Last-Event-ID replay
QUEUE_SIZE = 1024  # undelivered events per subscriber before it is closed
RETRY_MS = 3000  # the EventSource reconnect delay the stream asks for

MEDIA_TYPE = "text/event-stream"
HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

Window = tuple[datetime, datetime]


def row_key(candybar_id: UUID, fire: datetime) -> str:
    """A row's patch key: its identity in `/schedule` (candybar, fire time)."""
    return f"{candybar_id}@{fire.astimezone(UTC).isoformat()}"


def sse_frame(event_id: str | None, event: str | None, data: dict[str, Any] | None) -> str:
    """One SSE frame. `json.dumps` escapes newlines, so `data` is one line."""
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if event is not None:
        lines.append(f"event: {event}")
    if data is not None:
        lines.append("data: " + json.dumps(data, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


@dataclass(frozen=True)
class StreamEvent:
    """One buffered event: its window-independent payload plus what rendering needs."""

    seq: int
    type: str
    body: dict[str, Any]
    fire: datetime | None = None  # a row event's fire time: the window filter
    candybar: WindowCandybar | None = None  # definition.changed: rows render per window


_PING = object()  # a tick reached the hub: keep idle connections open
_CLOSE = object()  # end this subscriber's stream


class _Subscriber:
    def __init__(self, window: Window | None, queue_size: int) -> None:
        self.window = window
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_size)

    def close(self, *, drop_pending: bool = False) -> None:
        """End this stream once its queued events are written.

        An overflowing subscriber drops its backlog instead, since it was not
        reading. It resumes from the buffer on reconnect.
        """
        if not drop_pending:
            try:
                self.queue.put_nowait(_CLOSE)
                return
            except asyncio.QueueFull:
                pass
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(_CLOSE)


class ScheduleStream:
    """The api role's live-update hub: facts in, SSE frames out."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        now: Callable[[], datetime] | None = None,
        buffer_size: int = BUFFER_SIZE,
        queue_size: int = QUEUE_SIZE,
    ) -> None:
        self._session_factory = session_factory
        self._clock = now or (lambda: datetime.now(UTC))
        self.boot = uuid4().hex[:12]
        self._seq = 0
        self._buffer: deque[StreamEvent] = deque(maxlen=buffer_size)
        self._queue_size = queue_size
        self._subscribers: set[_Subscriber] = set()
        self._lock = asyncio.Lock()
        self._closed = False
        self._degraded = False
        self._last_tick: datetime | None = None
        self._current: CurrentKey = None
        self._current_json: dict[str, Any] | None = None
        self._handlers = {
            voc.TYPE_TICK_FIRED: self._on_tick,
            voc.TYPE_CANDYBAR_INVOKED: self._on_invocation,
            voc.TYPE_CANDYBAR_COMPLETED: self._on_invocation,
            voc.TYPE_CANDYBAR_FAILED: self._on_invocation,
            voc.TYPE_CANDYBAR_SKIPPED: self._on_skipped,
            voc.TYPE_CANDYBAR_CREATED: self._on_definition,
            voc.TYPE_CANDYBAR_UPDATED: self._on_definition,
        }

    # ------------------------------------------------------------------
    # State.

    @property
    def last_id(self) -> str:
        return self._id(self._seq)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _id(self, seq: int) -> str:
        return f"{self.boot}-{seq}"

    def _now(self) -> datetime:
        # The latest handled tick counts as reached even if the wall clock lags it.
        instant = self._clock()
        if self._last_tick is not None and instant < self._last_tick:
            return self._last_tick
        return instant

    def _status(self) -> dict[str, Any]:
        return {"cut": utc_iso(first_tick_after(self._now())), "current": self._current_json}

    def _notice(self, type_: str, seq: int, **extra: Any) -> dict[str, Any]:
        """A `ready`/`reset` payload: no patch, but the same envelope fields as one."""
        return {
            "type": type_,
            "seq": seq,
            "key": type_,
            "at": utc_iso(self._clock()),
            **extra,
            **self._status(),
        }

    # ------------------------------------------------------------------
    # Facts in.

    async def start(self) -> None:
        """Compute the current row before the first tick (no event). Never raises."""
        try:
            async with self._lock:
                await self._refresh_current(at=None)
        except Exception:
            log.exception("live stream: initial current row unavailable")

    async def handle(self, fact: Envelope) -> None:
        """Turn one bus fact into stream events. Never raises: the feed must survive."""
        handler = self._handlers.get(fact.type)
        if handler is None or self._closed:
            return
        async with self._lock:
            try:
                await handler(fact)
            except Exception:
                log.exception("live stream: could not handle %s %s", fact.type, fact.id)
                if not self._degraded:
                    self._degraded = True
                    notice = self._notice(EVENT_RESET, 0, reason="update_failed")
                    del notice["type"], notice["seq"]  # _emit stamps both
                    self._emit(EVENT_RESET, notice)
                return
            self._degraded = False

    async def _definition(self, candybar_id: UUID) -> WindowCandybar | None:
        async with self._session_factory() as session:
            row = await get_candybar(session, candybar_id)
            return WindowCandybar.of(row) if row is not None else None

    async def _refresh_current(
        self, *, at: datetime | None, overlay: WindowCandybar | None = None
    ) -> None:
        """Recompute the global current row; emit `current.advanced` when it changed.

        The same read and computation as `/schedule/next`. `overlay` is a
        definition the triggering fact already announced, which the registry
        may not have committed yet (the spine's event-first rule).
        """
        instant = self._now()
        cut = first_tick_after(instant)
        async with self._session_factory() as session:
            candybars = {row.id: WindowCandybar.of(row) for row in await list_candybars(session)}
            skips = group_skips(await list_skips(session, cut))
        if overlay is not None:
            candybars[overlay.id] = overlay
        row = await asyncio.to_thread(next_row, list(candybars.values()), now=instant, skips=skips)
        body = row.to_json() if row is not None else None
        self._current = (row.fire_time, row.candybar_id) if row is not None else None
        if body != self._current_json:
            self._current_json = body
            if at is not None:
                self._emit(
                    EVENT_CURRENT_ADVANCED,
                    {"key": "current", "at": utc_iso(at), "cut": utc_iso(cut), "current": body},
                )

    async def _on_tick(self, fact: Any) -> None:
        minute = fact.data.minute.astimezone(UTC)
        if self._last_tick is not None and minute <= self._last_tick:
            return  # a duplicate or replayed tick (AD-5: dedupe on tick_id)
        self._last_tick = minute
        self._broadcast(_PING)
        await self._refresh_current(at=fact.time)

    async def _fact_row(
        self, candybar_id: UUID, tick_id: str | None, *, covered: bool, last_result: str | None
    ) -> tuple[dict[str, Any] | None, datetime | None]:
        """The `/schedule` row a fact names, or (None, None) when it names none."""
        if tick_id is None:
            return None, None  # a manual run has no schedule row
        fire = tick_minute(tick_id)
        candybar = await self._definition(candybar_id)
        if candybar is None or not is_fire_of(candybar, fire):
            return None, None
        row = schedule_row(
            candybar,
            fire,
            cut=first_tick_after(self._now()),
            current=self._current,
            covered=covered,
            last_result=last_result,
        )
        return row.to_json(), fire

    async def _on_invocation(self, fact: Any) -> None:
        data = fact.data
        terminal = fact.type != voc.TYPE_CANDYBAR_INVOKED
        result = None
        if terminal:
            result = "success" if fact.type == voc.TYPE_CANDYBAR_COMPLETED else "fail"
        # A dispatched fire was not suppressed: its row is never `skipped`.
        row, fire = await self._fact_row(
            data.candybar_id, data.tick_id, covered=False, last_result=result
        )
        body: dict[str, Any] = {
            "key": row_key(data.candybar_id, fire) if fire else f"invocation:{data.invocation_id}",
            "at": utc_iso(fact.time),
            "candybarId": str(data.candybar_id),
            "invocationId": str(data.invocation_id),
            "trigger": data.trigger,
        }
        if terminal:
            body["result"] = result
        body["row"] = row
        self._emit(EVENT_INVOCATION_COMPLETED if terminal else EVENT_INVOCATION_STARTED, body, fire=fire)

    async def _on_skipped(self, fact: Any) -> None:
        data = fact.data
        # Only operator skips read `skipped: true` in /schedule; a coalesced
        # misfire is a past fire that simply did not run.
        row, fire = await self._fact_row(
            data.candybar_id, data.tick_id, covered=data.reason in SKIP_KINDS, last_result=None
        )
        body = {
            "key": row_key(data.candybar_id, fire) if fire else f"skip:{fact.id}",
            "at": utc_iso(fact.time),
            "candybarId": str(data.candybar_id),
            "reason": data.reason,
            "row": row,
        }
        self._emit(EVENT_SKIP_APPLIED, body, fire=fire)

    async def _on_definition(self, fact: Any) -> None:
        data = fact.data
        async with self._session_factory() as session:
            stored = await get_candybar(session, data.candybar_id)
            fields = (
                {
                    "id": stored.id,
                    "name": stored.name,
                    "description": stored.description,
                    "tags": list(stored.tags),
                    "schedule": stored.schedule,
                    "noise": stored.noise,
                    "created_at": stored.created_at,
                }
                if stored is not None
                else None
            )
        changes: dict[str, Any] | None = None
        if fact.type == voc.TYPE_CANDYBAR_CREATED:
            change = "created"
            if fields is None:  # published before the row commits: build it from the fact
                fields = {
                    "id": data.candybar_id,
                    "name": data.name,
                    "description": data.description,
                    "tags": list(data.tags),
                    "schedule": data.schedule,
                    "noise": data.noise,
                    "created_at": fact.time,
                }
        else:
            change = "updated"
            if fields is None:
                log.warning("live stream: updated fact for unknown candybar %s", data.candybar_id)
                return
            changes = {k: v for k, v in data.changes.items() if k in VISIBLE_FIELDS}
            fields.update(changes)  # a no-op when the row already committed
        definition = SimpleNamespace(**fields)
        candybar = WindowCandybar.of(definition)
        await self._refresh_current(at=fact.time, overlay=candybar)
        body: dict[str, Any] = {
            "key": f"definition:{data.candybar_id}",
            "at": utc_iso(fact.time),
            "candybarId": str(data.candybar_id),
            "change": change,
            "definition": candybar_row_json(definition),
        }
        if changes is not None:
            body["changes"] = changes
        self._emit(EVENT_DEFINITION_CHANGED, body, candybar=candybar)

    # ------------------------------------------------------------------
    # Fan-out.

    def _emit(self, type_: str, body: dict[str, Any], **extra: Any) -> StreamEvent:
        self._seq += 1
        event = StreamEvent(
            seq=self._seq, type=type_, body={"type": type_, "seq": self._seq, **body}, **extra
        )
        self._buffer.append(event)
        self._broadcast(event)
        return event

    def _broadcast(self, item: Any) -> None:
        for sub in list(self._subscribers):
            try:
                sub.queue.put_nowait(item)
            except asyncio.QueueFull:
                # A client that stopped reading: close it. It reconnects with
                # Last-Event-ID and resumes from the buffer (or gets a reset).
                log.warning("live stream: closing a subscriber that fell %d events behind", self._queue_size)
                self._subscribers.discard(sub)
                sub.close(drop_pending=True)

    def close(self) -> None:
        """End every stream (role stop) after its queued events: uvicorn can then shut down."""
        self._closed = True
        for sub in list(self._subscribers):
            sub.close()
        self._subscribers.clear()

    # ------------------------------------------------------------------
    # Frames out.

    def _resume_after(self, last_event_id: str) -> int | None:
        """The seq to replay after, or None when the id cannot be resumed exactly."""
        boot, sep, n = last_event_id.strip().rpartition("-")
        # ASCII digits only: str.isdigit() also accepts e.g. "²", which int() refuses.
        if not sep or boot != self.boot or not (n.isascii() and n.isdigit()):
            return None
        seq = int(n)
        floor = self._buffer[0].seq - 1 if self._buffer else self._seq
        return seq if floor <= seq <= self._seq else None

    def _attach(
        self, last_event_id: str | None, window: Window | None
    ) -> tuple[_Subscriber, str, list[StreamEvent]]:
        """Register a subscriber and take its replay in one step (no await): no gap, no dupe."""
        sub = _Subscriber(window, self._queue_size)
        self._subscribers.add(sub)
        opening_id = self._id(self._seq)
        if last_event_id is None:
            body = self._notice(EVENT_READY, self._seq)
            return sub, sse_frame(opening_id, EVENT_READY, body), []
        after = self._resume_after(last_event_id)
        if after is None:
            body = self._notice(EVENT_RESET, self._seq, reason="unresumable")
            return sub, sse_frame(opening_id, EVENT_RESET, body), []
        return sub, "", [event for event in self._buffer if event.seq > after]

    async def frames(
        self, *, last_event_id: str | None = None, window: Window | None = None
    ) -> AsyncIterator[str]:
        """One client's SSE frames: the opening or the replay, then live events."""
        if self._closed:
            return
        sub, opening, replay = self._attach(last_event_id, window)
        try:
            yield f"retry: {RETRY_MS}\n\n" + opening
            for event in replay:
                yield await self._render(event, sub.window)
            while True:
                item = await sub.queue.get()
                if item is _CLOSE:
                    return
                if item is _PING:
                    yield ": keepalive\n\n"
                    continue
                yield await self._render(item, sub.window)
        finally:
            self._subscribers.discard(sub)

    async def _render(self, event: StreamEvent, window: Window | None) -> str:
        event_id = self._id(event.seq)
        if window is not None and event.fire is not None and not window[0] <= event.fire < window[1]:
            # Outside the client's window: advance its cursor, dispatch nothing.
            return sse_frame(event_id, None, None)
        body = event.body
        if event.candybar is not None and window is not None:
            try:
                body = {**body, "rows": await self._rows(event.candybar, window)}
            except Exception:
                log.exception("live stream: could not render rows for %s", event.body.get("key"))
                reset = self._notice(EVENT_RESET, event.seq, reason="render_failed")
                return sse_frame(event_id, EVENT_RESET, reset)
        return sse_frame(event_id, event.type, body)

    async def _rows(self, candybar: WindowCandybar, window: Window) -> list[dict[str, Any]]:
        """That candybar's complete rows in the window, exactly as `/schedule` returns them."""
        start, end = window
        instant = self._now()
        async with self._session_factory() as session:
            skips = group_skips(await list_skips(session, start, end)).get(candybar.id, NO_SKIPS)
        rows = await asyncio.to_thread(
            candybar_rows, candybar, start, end, now=instant, skips=skips, current=self._current
        )
        keys = past_keys(rows)
        if keys:
            async with self._session_factory() as session:
                rows = attach_results(rows, await fire_results(session, keys))
        return [row.to_json() for row in rows]


def parse_fact(payload: bytes) -> Envelope | None:
    """A bus message as a typed cron envelope, or None when it is not one."""
    try:
        return parse_envelope(json.loads(payload))
    except (TypeError, ValueError, AttributeError):  # not JSON / not an object / not cron
        return None
