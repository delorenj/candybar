"""Consume invoke commands: dedupe, spawn, record, emit, ack (AD-5, AD-7, AD-10).

Effectively-once execution rests on the registry, not the bus: the terminal
`invocations` row (unique `idempotency_key`) commits FIRST, then the terminal
event publishes, then the command is acked. A redelivered command whose key
is recorded never re-executes — it republishes the deterministic terminal
event rebuilt from the row and acks. Terminal/invoked event ids are UUIDv5
of `<phase>:<invocation_id>`, so JetStream `Nats-Msg-Id` dedupe makes every
republication safe.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID, uuid5

from pydantic import ValidationError

from ..events import (
    STDERR_TAIL_MAX,
    build_candybar_completed,
    build_candybar_failed,
    build_candybar_invoked,
    parse_envelope,
)
from ..events.envelope import (
    CandybarFailedEvent,
    CandybarInvokeCommand,
    Envelope,
    InvocationMetrics,
    SelfReportedMetrics,
)
from ..registry.models import Invocation
from ..registry.store import TerminalInvocation
from .metrics import EXECUTOR_METRICS, ExecutorMetrics
from .runner import ProcessRegistry, RunOutcome, run_invocation

log = logging.getLogger(__name__)

# Stable lifecycle-event ids survive redelivery and executor restarts.
EXEC_NAMESPACE = UUID("f0a3b7e1-2c4d-4e5f-8a9b-0c1d2e3f4a5b")


def lifecycle_event_id(phase: str, invocation_id: UUID) -> UUID:
    return uuid5(EXEC_NAMESPACE, f"{phase}:{invocation_id}")


@dataclass(frozen=True)
class Resolved:
    """The terminal verdict for one run, mapped from the AD-8 protocol."""

    success: bool
    message: str | None
    metrics: SelfReportedMetrics  # what the child reported; measures live on RunOutcome


def _cap(message: str | None) -> str | None:
    """FailureDetail.message is max_length=500 — never exceed it."""
    if message is None:
        return None
    return message[:STDERR_TAIL_MAX]


def resolve_outcome(outcome: RunOutcome, *, stopped: bool = False) -> Resolved:
    """Exit code is primary; a valid result file refines; absence is the bare
    executable fallback (AD-8). See the spec's Design Notes for the precedence."""
    if stopped and outcome.exit_code not in (0, None):
        return Resolved(False, "executor stopped (SIGTERM)", InvocationMetrics())
    if outcome.spawn_error is not None:
        return Resolved(False, _cap(f"spawn failed: {outcome.spawn_error}"), InvocationMetrics())
    if outcome.exit_code is None:  # unreachable without spawn_error; stay safe
        return Resolved(False, "child never started", InvocationMetrics())
    if outcome.exit_code != 0:
        message = None
        if outcome.result is not None and outcome.result.message:
            message = outcome.result.message
        return Resolved(False, _cap(message or f"exit code {outcome.exit_code}"), _metrics(outcome))
    if outcome.result_error is not None:
        return Resolved(False, _cap(outcome.result_error), InvocationMetrics())
    if outcome.result is None:
        return Resolved(True, None, InvocationMetrics())  # bare executable: metrics zero
    if outcome.result.status == "success":
        return Resolved(True, None, outcome.result.metrics)
    return Resolved(
        False, _cap(outcome.result.message or "result file reports fail"), outcome.result.metrics
    )


def _metrics(outcome: RunOutcome) -> SelfReportedMetrics:
    return outcome.result.metrics if outcome.result is not None else SelfReportedMetrics()


def terminal_event_for(cmd: CandybarInvokeCommand, rec: TerminalInvocation) -> Envelope:
    """The completed/failed fact with its deterministic, republication-safe id."""
    event_id = lifecycle_event_id(rec.result, rec.invocation_id)
    # The full per-invocation record rides on BOTH terminal facts (AD-9).
    metrics = InvocationMetrics(
        token_spend=rec.token_spend,
        cost_usd=float(rec.cost_usd) if rec.cost_usd is not None else None,
        memory_peak_bytes=rec.memory_peak_bytes,
        cpu_load=rec.cpu_load,
    )
    common = {
        "candybar_id": rec.candybar_id,
        "name": cmd.data.name,
        "invocation_id": rec.invocation_id,
        "trigger": rec.trigger,
        "tick_id": rec.tick_id,
        "correlationid": cmd.correlationid,
        "causationid": cmd.command_id,
        "event_id": event_id,
    }
    if rec.result == "success":
        return build_candybar_completed(
            **common,  # type: ignore[arg-type]
            exit_code=rec.exit_code if rec.exit_code is not None else 0,
            runtime_ms=rec.runtime_ms if rec.runtime_ms is not None else 0,
            metrics=metrics,
        )
    return build_candybar_failed(
        **common,  # type: ignore[arg-type]
        exit_code=rec.exit_code,
        runtime_ms=rec.runtime_ms,
        message=rec.error_message,
        stderr=rec.stderr_tail or "",
        metrics=metrics,
    )


def record_from_row(row: Invocation) -> TerminalInvocation:
    """Rebuild the terminal record from the stored row (redelivery path)."""
    return TerminalInvocation(
        invocation_id=row.id,
        candybar_id=row.candybar_id,
        trigger=row.trigger,
        tick_id=row.tick_id,
        idempotency_key=row.idempotency_key,
        result=row.result or "fail",
        exit_code=row.exit_code,
        runtime_ms=row.runtime_ms,
        error_message=row.error_message,
        stderr_tail=row.stderr_tail,
        token_spend=row.token_spend,
        cost_usd=row.cost_usd,
        started_at=row.started_at,
        finished_at=row.finished_at or row.created_at,
        memory_peak_bytes=row.memory_peak_bytes or 0,
        cpu_load=row.cpu_load or 0.0,
    )


class Executor:
    """One invoke command in, one execution, one terminal record + fact out."""

    def __init__(
        self,
        *,
        by_key: Callable[[str], Awaitable[Invocation | None]],
        entrypoint: Callable[[UUID], Awaitable[str | None]],
        record: Callable[[TerminalInvocation], Awaitable[bool]],
        publish: Callable[[Envelope], Awaitable[None]],
        run: Callable[..., Awaitable[RunOutcome]] = run_invocation,
        registry: ProcessRegistry | None = None,
        metrics: ExecutorMetrics | None = None,
    ) -> None:
        self._by_key = by_key
        self._entrypoint = entrypoint
        self._record = record
        self._publish = publish
        self._run = run
        self.registry = registry or ProcessRegistry()
        self.metrics = metrics if metrics is not None else EXECUTOR_METRICS
        self._inflight: set[str] = set()

    def _observe(self, rec: TerminalInvocation) -> None:
        """Metrics never fail an invocation: log and carry on."""
        try:
            self.metrics.observe(
                candybar_id=rec.candybar_id,
                result=rec.result,
                invocation_id=rec.invocation_id,
                runtime_ms=rec.runtime_ms or 0,
                memory_peak_bytes=rec.memory_peak_bytes,
                cpu_load=rec.cpu_load,
            )
        except Exception:
            log.exception("metric recording failed for invocation %s", rec.invocation_id)

    async def handle_message(self, msg: object) -> None:
        """Terminate poison payloads; nak operational failures; ack terminal states."""
        try:
            envelope = parse_envelope(json.loads(msg.data))  # type: ignore[attr-defined]
            if not isinstance(envelope, CandybarInvokeCommand):
                raise TypeError("not an invoke command")
        except (ValueError, TypeError, AttributeError, ValidationError) as exc:
            log.warning("discarding invalid invoke command: %s", exc)
            await msg.term()  # type: ignore[attr-defined]
            return

        key = envelope.idempotency_key
        if key in self._inflight:
            # Same key already claimed in this process: retry after it records.
            await msg.nak(delay=5)  # type: ignore[attr-defined]
            return
        # Claim synchronously — before the first await. Claiming later (it
        # used to happen just before spawn) let two concurrent deliveries of
        # one key both pass the dedupe read and both execute (Story 2.1).
        self._inflight.add(key)
        try:
            await self._handle_claimed(envelope, key, msg)
        finally:
            self._inflight.discard(key)

    async def _handle_claimed(self, envelope: CandybarInvokeCommand, key: str, msg: object) -> None:
        """Dedupe, execute, record, publish, ack — with `key` claimed in-process."""
        try:
            existing = await self._by_key(key)
        except Exception:
            # Uncaught, a crashed task leaves the command stalled until ack_wait.
            log.exception("dedupe read failed for %s; requesting redelivery", key)
            await msg.nak(delay=1)  # type: ignore[attr-defined]
            return
        if existing is not None:
            await self._republish_terminal(existing, envelope, msg)
            return

        try:
            entrypoint = await self._entrypoint(envelope.data.candybar_id)
        except Exception:
            log.exception("entrypoint lookup failed for %s; requesting redelivery", key)
            await msg.nak(delay=1)  # type: ignore[attr-defined]
            return
        if entrypoint is None:
            await self._fail_unknown_candybar(envelope, msg)
            return

        try:
            await self._publish(
                build_candybar_invoked(
                    candybar_id=envelope.data.candybar_id,
                    name=envelope.data.name,
                    invocation_id=envelope.data.invocation_id,
                    trigger=envelope.data.trigger,
                    tick_id=envelope.data.tick_id,
                    correlationid=envelope.correlationid,
                    causationid=envelope.command_id,
                    event_id=lifecycle_event_id("invoked", envelope.data.invocation_id),
                )
            )
        except Exception:
            log.exception("failed to publish invoked for %s; requesting redelivery", key)
            await msg.nak(delay=1)  # type: ignore[attr-defined]
            return

        try:
            outcome = await self._run(
                entrypoint=entrypoint,
                invocation_id=envelope.data.invocation_id,
                candybar_id=envelope.data.candybar_id,
                trigger=envelope.data.trigger,
                tick_id=envelope.data.tick_id,
                registry=self.registry,
                on_wait=msg.in_progress,  # type: ignore[attr-defined]
            )
        except Exception:
            log.exception("execution crashed for %s; requesting redelivery", key)
            await msg.nak(delay=1)  # type: ignore[attr-defined]
            return

        resolved = resolve_outcome(outcome, stopped=self.registry.stopping)
        rec = TerminalInvocation(
            invocation_id=envelope.data.invocation_id,
            candybar_id=envelope.data.candybar_id,
            trigger=envelope.data.trigger,
            tick_id=envelope.data.tick_id,
            idempotency_key=key,
            result="success" if resolved.success else "fail",
            exit_code=outcome.exit_code,
            runtime_ms=outcome.runtime_ms,
            error_message=resolved.message,
            stderr_tail=outcome.stderr_tail or None,
            token_spend=resolved.metrics.token_spend,
            cost_usd=resolved.metrics.cost_usd,
            started_at=outcome.started_at,
            finished_at=outcome.finished_at,
            memory_peak_bytes=outcome.memory_peak_bytes,
            cpu_load=outcome.cpu_load,
        )
        try:
            inserted = await self._record(rec)
        except Exception:
            # e.g. the candybar was deleted between lookup and record (KeyError).
            log.exception("terminal record failed for %s; requesting redelivery", key)
            await msg.nak(delay=1)  # type: ignore[attr-defined]
            return
        if not inserted:
            # Lost a same-key race: the winner's row is the truth; republish it.
            winner = await self._by_key(key)
            if winner is not None:
                await self._republish_terminal(winner, envelope, msg)
            else:  # pragma: no cover - winner rolled back too; retry cleanly
                await msg.nak(delay=1)  # type: ignore[attr-defined]
            return

        # The committed row is the once-per-execution anchor: metrics are
        # observed here and never on the redelivery/republish path (AD-9).
        self._observe(rec)

        try:
            await self._publish(terminal_event_for(envelope, rec))
        except Exception:
            # Row committed: redelivery republishes from it. Never re-execute.
            log.exception("terminal event publish failed for %s; requesting redelivery", key)
            await msg.nak(delay=1)  # type: ignore[attr-defined]
            return
        await msg.ack()  # type: ignore[attr-defined]
        log.info(
            "invocation %s (%s) %s in %dms",
            rec.invocation_id,
            envelope.data.name,
            rec.result,
            rec.runtime_ms or 0,
        )

    async def _republish_terminal(
        self, row: Invocation, cmd: CandybarInvokeCommand, msg: object
    ) -> None:
        """Redelivery with a recorded key: republish the terminal fact, ack."""
        if row.result is None:
            await msg.ack()  # type: ignore[attr-defined]
            return
        try:
            await self._publish(terminal_event_for(cmd, record_from_row(row)))
        except Exception:
            log.exception(
                "terminal republish failed for %s; requesting redelivery", row.idempotency_key
            )
            await msg.nak(delay=1)  # type: ignore[attr-defined]
            return
        await msg.ack()  # type: ignore[attr-defined]
        log.info("redelivered %s already recorded as %s; no second execution", row.idempotency_key, row.result)

    async def _fail_unknown_candybar(self, cmd: CandybarInvokeCommand, msg: object) -> None:
        """No registry row: record impossible (FK), emit the failure fact, term."""
        event: CandybarFailedEvent = build_candybar_failed(
            candybar_id=cmd.data.candybar_id,
            name=cmd.data.name,
            invocation_id=cmd.data.invocation_id,
            trigger=cmd.data.trigger,
            tick_id=cmd.data.tick_id,
            exit_code=None,
            runtime_ms=None,
            message=f"unknown candybar: {cmd.data.candybar_id} is not registered",
            correlationid=cmd.correlationid,
            causationid=cmd.command_id,
            event_id=lifecycle_event_id("fail", cmd.data.invocation_id),
        )
        try:
            await self._publish(event)
        except Exception:
            log.exception("failed to publish unknown-candybar failure; requesting redelivery")
            await msg.nak(delay=1)  # type: ignore[attr-defined]
            return
        await msg.term()  # type: ignore[attr-defined]


__all__ = [
    "EXEC_NAMESPACE",
    "Executor",
    "Resolved",
    "lifecycle_event_id",
    "record_from_row",
    "resolve_outcome",
    "terminal_event_for",
]
