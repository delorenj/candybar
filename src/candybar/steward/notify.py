"""Consume finished steward facts durably; default to local receipts, not chat.

The steward task registry, reports and task facts remain the evidence. A configured
CANDYBAR_STEWARD_NOTIFY_TARGET explicitly opts into operator messages; absent or
`off` keeps delivery local while still draining the durable consumer. On opt-in,
only facts published after this notifier started can be sent: a backlog built
while the route was local must never turn into a burst of Telegram messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import OrderedDict
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nats.errors import (
    BadSubscriptionError,
    ConnectionClosedError,
    ConnectionDrainingError,
)
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

from ..events import parse_envelope
from ..events.envelope import (
    StewardTaskCompletedEvent,
    StewardTaskData,
    StewardTaskFailedEvent,
)
from ..transition_exec import notify as hermes_send
from .reports import DEFAULT_REPORTS_DIR, REPORTS_DIR_ENV

log = logging.getLogger(__name__)

EVENT_STREAM = "BLOODBANK_EVENTS"
NOTIFY_FILTER = "bloodbank.evt.cron.steward_task.>"
NOTIFY_DURABLE = "candybar-steward-notify"
NOTIFY_MAX_DELIVERIES = 6
NOTIFY_SEND_TIMEOUT_S = 30.0
ANNOUNCED_MAX = 512
ACK_WAIT_SECONDS = 120
REATTACH_S = 30

TARGET_ENV = "CANDYBAR_STEWARD_NOTIFY_TARGET"
PROFILE_ENV = "CANDYBAR_STEWARD_NOTIFY_PROFILE"
HOLOCENE_ENV = "CANDYBAR_HOLOCENE_URL"
DEFAULT_PROFILE = "33god-pm"
DEFAULT_HOLOCENE = "https://holocene.delo.sh/candybar"
CANCELLED = "cancelled by operator"
SUMMARY_MAX = 1500

# Outcomes that are good news; anything else that succeeded wants a look.
_GOOD = {"Nominal", "fixed", "done", "done_proven"}

Sender = Callable[[str, str, str, float], None]


@dataclass(frozen=True)
class NotifyConfig:
    target: str
    profile: str
    holocene: str
    reports_dir: Path

    @classmethod
    def from_env(cls) -> NotifyConfig:
        # An absent target is a local-only route, not an opt-out of consuming facts.
        target = os.environ.get(TARGET_ENV, "").strip()
        if target.lower() in {"off", "none", "0", "false"}:
            target = ""
        return cls(
            target=target,
            profile=os.environ.get(PROFILE_ENV, "").strip() or DEFAULT_PROFILE,
            holocene=(os.environ.get(HOLOCENE_ENV, "").strip() or DEFAULT_HOLOCENE).rstrip("/"),
            reports_dir=Path(os.environ.get(REPORTS_DIR_ENV) or DEFAULT_REPORTS_DIR).expanduser(),
        )


def _clip(text: str, limit: int = SUMMARY_MAX) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render(task: StewardTaskData, config: NotifyConfig) -> str | None:
    """The message for one finished task; None when it is not worth announcing."""
    if task.status == "failed" and (task.error or "").strip() == CANCELLED:
        return None
    if task.status == "succeeded":
        mark = "✅" if (task.outcome or "done") in _GOOD else "⚠️"
        verdict = task.outcome or "done"
    else:
        mark, verdict = "❌", "failed"
    head = f"{mark} Candybar {task.action} · {task.name} → {verdict}"
    lines = [head]
    if task.parent_task_id is not None:
        # An evaluate's fix / refactor, or the enrich a fix / refactor queues.
        lines.append(f"(follow-up of task {str(task.parent_task_id)[:8]})")
    body = task.summary if task.status == "succeeded" else (task.error or task.summary)
    if body:
        lines += ["", _clip(body)]
    lines.append("")
    if task.report_key:
        lines.append(f"Report: {config.reports_dir / task.report_key}")
    lines.append(f"Open: {config.holocene}?tab=definitions&open={task.candybar_id}")
    lines.append(f"Task: {task.task_id}")
    return "\n".join(lines)


class StewardNotifier:
    """Turns steward task facts into operator messages. Never raises from handle_message."""

    def __init__(self, config: NotifyConfig, sender: Sender = hermes_send,
                 *, route_started_at: datetime | None = None) -> None:
        self._config = config
        self._send = sender
        self._route_started_at = route_started_at
        # (task, status) already announced: a terminal write retried after its
        # fact went out republishes the same fact; say it once.
        self._announced: OrderedDict[tuple[str, str], None] = OrderedDict()

    async def handle_message(self, msg: Any) -> None:
        try:
            fact = parse_envelope(json.loads(msg.data))
        except Exception as exc:  # noqa: BLE001  (poison: never redeliver)
            log.warning("steward-notify: dropping an unparseable fact (%s: %s)", type(exc).__name__, exc)
            await msg.term()
            return
        if not isinstance(fact, (StewardTaskCompletedEvent, StewardTaskFailedEvent)):
            await msg.ack()  # queued / started: nothing to say yet
            return
        key = (str(fact.data.task_id), fact.data.status)
        # Ack only the notifier copy. The task registry, report and Bloodbank fact
        # remain intact. A route enabled later never replays the quiet backlog.
        if not self._config.target or (self._route_started_at is not None and
                                       fact.time < self._route_started_at):
            log.info("steward-notify: local task %s (%s %s %s)", fact.data.task_id,
                     fact.data.action, fact.data.name, fact.data.outcome or fact.data.status)
            await msg.ack()
            return
        text = render(fact.data, self._config)
        if text is None or key in self._announced:
            await msg.ack()
            return
        try:
            await asyncio.to_thread(
                self._send, self._config.profile, self._config.target, text, NOTIFY_SEND_TIMEOUT_S
            )
        except Exception as exc:  # noqa: BLE001  (transport: retry, bounded)
            deliveries = getattr(getattr(msg, "metadata", None), "num_delivered", 1) or 1
            if deliveries >= NOTIFY_MAX_DELIVERIES:
                log.error(
                    "steward-notify: task %s (%s %s) not delivered after %d attempts (%s); dropping",
                    fact.data.task_id, fact.data.action, fact.data.name, deliveries, exc,
                )
                await msg.ack()
                return
            delay = min(30 * 2 ** (deliveries - 1), 600)
            log.warning(
                "steward-notify: send for task %s failed (%s); retry %d in %ds",
                fact.data.task_id, exc, deliveries, delay,
            )
            await msg.nak(delay=delay)
            return
        self._announced[key] = None
        while len(self._announced) > ANNOUNCED_MAX:
            self._announced.popitem(last=False)
        log.info("steward-notify: announced task %s (%s %s %s)", fact.data.task_id, fact.data.action,
                 fact.data.name, fact.data.outcome or fact.data.status)
        await msg.ack()


async def run_notifier(js: Any, stop: asyncio.Event, config: NotifyConfig | None = None) -> None:
    """Attach the durable and announce finished tasks until stop. Logs, never raises."""
    config = config if config is not None else NotifyConfig.from_env()
    # This cutoff is established before attaching the existing durable. Events
    # queued under the former noisy policy (or while local-only) stay local.
    notifier = StewardNotifier(config, route_started_at=datetime.now(UTC))
    consumer = ConsumerConfig(
        durable_name=NOTIFY_DURABLE,
        filter_subject=NOTIFY_FILTER,
        # First attach announces from now on, never the week of history; the
        # durable then resumes where it left off across restarts.
        deliver_policy=DeliverPolicy.NEW,
        ack_policy=AckPolicy.EXPLICIT,
        ack_wait=ACK_WAIT_SECONDS,
    )
    while not stop.is_set():
        sub = None
        try:
            sub = await js.pull_subscribe(
                NOTIFY_FILTER, durable=NOTIFY_DURABLE, stream=EVENT_STREAM, config=consumer
            )
            log.info("steward-notify: %s via profile %s",
                     f"operator route {config.target}" if config.target else "local-only", config.profile)
            while not stop.is_set():
                try:
                    messages = await sub.fetch(1, timeout=1)
                except (NatsTimeoutError, TimeoutError):
                    continue
                for message in messages:
                    await notifier.handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never take the steward down with it; re-attach after a pause.
            log.exception("steward-notify: consumer failed; re-attaching in %ds", REATTACH_S)
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=REATTACH_S)
        finally:
            if sub is not None:
                with suppress(ConnectionClosedError, ConnectionDrainingError, BadSubscriptionError, Exception):
                    await sub.unsubscribe()
