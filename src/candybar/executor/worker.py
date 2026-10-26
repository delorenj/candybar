"""Consume invoke commands: dedupe, claim, spawn, record, emit, ack (AD-5, AD-7, AD-10).

Effectively-once execution rests on the registry, not the bus. Just before
the child spawns, the executor commits a claim: the `invocations` row for the
command's unique `idempotency_key`, with `result` NULL. When the child exits,
the terminal outcome finalizes that row, then the terminal event publishes,
then the command is acked. A redelivered command whose key is recorded never
re-executes: it republishes the deterministic terminal event rebuilt from the
row and acks.

A redelivered command whose key is claimed but unfinished means the executor
lost the run: it was SIGKILLed or OOM-killed mid-run, or the terminal write
failed. Such a run is finalized `fail` as abandoned and is NOT executed again,
unless the candybar carries the `retry-on-abandon` tag. Then it re-runs, at
most until the command's delivery count passes ABANDON_RETRY_MAX_DELIVERIES.

Terminal/invoked event ids are UUIDv5 of `<phase>:<invocation_id>`, so
JetStream `Nats-Msg-Id` dedupe makes every republication safe. Every
child-supplied string is scrubbed of NUL and lone surrogates before it is
stored or published (`candybar.text.scrub_text`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
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
from ..registry.store import InvocationClaim, TerminalInvocation
from ..text import scrub_optional, scrub_text
from .metrics import EXECUTOR_METRICS, ExecutorMetrics
from .runner import ProcessRegistry, RunOutcome, run_invocation

log = logging.getLogger(__name__)

# Stable lifecycle-event ids survive redelivery and executor restarts.
EXEC_NAMESPACE = UUID("f0a3b7e1-2c4d-4e5f-8a9b-0c1d2e3f4a5b")


# Opt-in for re-running a run the executor lost (hard kill / OOM): a candybar tag.
RETRY_ON_ABANDON_TAG = "retry-on-abandon"
# Bounds that re-run, so a job that OOM-kills the executor cannot loop forever.
ABANDON_RETRY_MAX_DELIVERIES = 3
ABANDONED_MESSAGE = (
    "abandoned: the executor lost this run before recording its result "
    "(killed, OOM, or a failed terminal write); not re-executed"
)


def lifecycle_event_id(phase: str, invocation_id: UUID) -> UUID:
    return uuid5(EXEC_NAMESPACE, f"{phase}:{invocation_id}")


@dataclass(frozen=True)
class Resolved:
    """The terminal verdict for one run, mapped from the AD-8 protocol."""

    success: bool
    message: str | None
    metrics: SelfReportedMetrics  # what the child reported; measures live on RunOutcome


def _cap(message: str | None) -> str | None:
    """FailureDetail.message is max_length=500 — never exceed it. Child-supplied
    text may carry NUL or lone surrogates, which Postgres and UTF-8 reject."""
    if message is None:
        return None
    return scrub_text(message[:STDERR_TAIL_MAX])


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
        gpu_load=rec.gpu_load,
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
        gpu_load=row.gpu_load or 0.0,
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
        claim: Callable[[InvocationClaim], Awaitable[bool]] | None = None,
        retry_abandoned: Callable[[UUID], Awaitable[bool]] | None = None,
    ) -> None:
        """`claim` commits the pre-spawn running row (True) or reports the key
        already taken (False); without it no claim is written. `retry_abandoned`
        says whether a candybar opted into re-running a lost run."""
        self._claim = claim
        self._retry_abandoned = retry_abandoned
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
                gpu_load=rec.gpu_load,
                token_spend=rec.token_spend,
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
        """Dedupe, then execute — with `key` claimed in-process."""
        try:
            existing = await self._by_key(key)
        except Exception:
            # Uncaught, a crashed task leaves the command stalled until ack_wait.
            log.exception("dedupe read failed for %s; requesting redelivery", key)
            await msg.nak(delay=1)  # type: ignore[attr-defined]
            return
        if existing is None:
            await self._execute(envelope, key, msg, invocation_id=envelope.data.invocation_id)
        elif existing.result is None:
            await self._handle_lost_run(existing, envelope, key, msg)
        else:
            await self._republish_terminal(existing, envelope, msg)

    async def _handle_lost_run(
        self, row: Invocation, envelope: CandybarInvokeCommand, key: str, msg: object
    ) -> None:
        """A claimed-but-unfinished key on (re)delivery: the run was lost.

        In-process duplicates never get here (`_inflight`), so the claim's
        owner is gone: SIGKILL/OOM of the executor, or a terminal write that
        failed. Finalize it `fail` as abandoned, never re-executing, unless the
        candybar opted in with the `retry-on-abandon` tag.
        """
        try:
            retry = await self._retry_allowed(row.candybar_id, msg)
        except Exception:
            log.exception("retry opt-in read failed for %s; requesting redelivery", key)
            await msg.nak(delay=1)  # type: ignore[attr-defined]
            return
        if retry:
            log.warning("re-running lost invocation %s (%s opted in)", row.id, RETRY_ON_ABANDON_TAG)
            await self._execute(envelope, key, msg, invocation_id=row.id, claimed=True)
            return
        log.warning("invocation %s was lost mid-run; recording it abandoned", row.id)
        rec = TerminalInvocation(
            invocation_id=row.id,
            candybar_id=row.candybar_id,
            trigger=row.trigger,
            tick_id=row.tick_id,
            idempotency_key=key,
            result="fail",
            exit_code=None,
            runtime_ms=None,
            error_message=ABANDONED_MESSAGE,
            stderr_tail=None,
            token_spend=0,
            cost_usd=None,
            started_at=row.started_at,
            finished_at=datetime.now(UTC),
        )
        await self._finalize(rec, envelope, key, msg)

    async def _retry_allowed(self, candybar_id: UUID, msg: object) -> bool:
        if self._retry_abandoned is None:
            return False
        metadata = getattr(msg, "metadata", None)
        deliveries = getattr(metadata, "num_delivered", 1) or 1
        if deliveries > ABANDON_RETRY_MAX_DELIVERIES:
            return False
        return await self._retry_abandoned(candybar_id)

    async def _execute(
        self,
        envelope: CandybarInvokeCommand,
        key: str,
        msg: object,
        *,
        invocation_id: UUID,
        claimed: bool = False,
    ) -> None:
        """Look up, claim, spawn, then finalize. `claimed`: the row already exists."""
        try:
            entrypoint = await self._entrypoint(envelope.data.candybar_id)
        except Exception:
            log.exception("entrypoint lookup failed for %s; requesting redelivery", key)
            await msg.nak(delay=1)  # type: ignore[attr-defined]
            return
        if entrypoint is None:
            await self._fail_unknown_candybar(envelope, msg)
            return

        if self.registry.stopping:
            # Shutdown began before this command started (fetched in the
            # role's last poll, or still in its dedupe read): nothing has run,
            # so leave it on the stream for the next executor instead of
            # spawning a child into a stopping process.
            log.info("executor stopping; returning %s to the stream unexecuted", key)
            await msg.nak()  # type: ignore[attr-defined]
            return

        try:
            await self._publish(
                build_candybar_invoked(
                    candybar_id=envelope.data.candybar_id,
                    name=envelope.data.name,
                    invocation_id=invocation_id,
                    trigger=envelope.data.trigger,
                    tick_id=envelope.data.tick_id,
                    correlationid=envelope.correlationid,
                    causationid=envelope.command_id,
                    event_id=lifecycle_event_id("invoked", invocation_id),
                )
            )
        except Exception:
            log.exception("failed to publish invoked for %s; requesting redelivery", key)
            await msg.nak(delay=1)  # type: ignore[attr-defined]
            return

        if not claimed and self._claim is not None:
            # Hard-kill safety: the running row commits BEFORE the spawn, so a
            # redelivery after a SIGKILL/OOM finds it instead of re-running.
            try:
                won = await self._claim(
                    InvocationClaim(
                        invocation_id=invocation_id,
                        candybar_id=envelope.data.candybar_id,
                        trigger=envelope.data.trigger,
                        tick_id=envelope.data.tick_id,
                        idempotency_key=key,
                        started_at=datetime.now(UTC),
                    )
                )
            except Exception:
                # e.g. the candybar was deleted after the lookup (KeyError).
                log.exception("claim failed for %s; requesting redelivery", key)
                await msg.nak(delay=1)  # type: ignore[attr-defined]
                return
            if not won:
                await self._lost_claim_race(envelope, key, msg)
                return

        try:
            outcome = await self._run(
                entrypoint=entrypoint,
                invocation_id=invocation_id,
                candybar_id=envelope.data.candybar_id,
                trigger=envelope.data.trigger,
                tick_id=envelope.data.tick_id,
                registry=self.registry,
                on_wait=msg.in_progress,  # type: ignore[attr-defined]
            )
        except Exception:
            # With a claim, the redelivery records the run abandoned.
            log.exception("execution crashed for %s; requesting redelivery", key)
            await msg.nak(delay=1)  # type: ignore[attr-defined]
            return

        resolved = resolve_outcome(outcome, stopped=self.registry.stopping)
        rec = TerminalInvocation(
            invocation_id=invocation_id,
            candybar_id=envelope.data.candybar_id,
            trigger=envelope.data.trigger,
            tick_id=envelope.data.tick_id,
            idempotency_key=key,
            result="success" if resolved.success else "fail",
            exit_code=outcome.exit_code,
            runtime_ms=outcome.runtime_ms,
            error_message=resolved.message,
            # A NUL in stderr would fail the terminal write and, before the
            # claim row, re-execute the job on every redelivery.
            stderr_tail=scrub_optional(outcome.stderr_tail or None),
            token_spend=resolved.metrics.token_spend,
            cost_usd=resolved.metrics.cost_usd,
            started_at=outcome.started_at,
            finished_at=outcome.finished_at,
            memory_peak_bytes=outcome.memory_peak_bytes,
            cpu_load=outcome.cpu_load,
            gpu_load=outcome.gpu_load,
        )
        await self._finalize(rec, envelope, key, msg)

    async def _lost_claim_race(self, envelope: CandybarInvokeCommand, key: str, msg: object) -> None:
        """Another delivery claimed this key between our dedupe read and claim."""
        try:
            winner = await self._by_key(key)
        except Exception:
            log.exception("claim winner read failed for %s; requesting redelivery", key)
            await msg.nak(delay=1)  # type: ignore[attr-defined]
            return
        if winner is not None and winner.result is not None:
            await self._republish_terminal(winner, envelope, msg)
            return
        # The winner claimed milliseconds ago and is running it now: it owns
        # the outcome. Running a second copy is exactly what the claim prevents.
        log.info("%s claimed by a concurrent delivery; not executing twice", key)
        await msg.ack()  # type: ignore[attr-defined]

    async def _finalize(
        self, rec: TerminalInvocation, envelope: CandybarInvokeCommand, key: str, msg: object
    ) -> None:
        """Record the terminal row, observe, publish the terminal fact, ack."""
        try:
            inserted = await self._record(rec)
        except Exception:
            # e.g. the candybar was deleted between lookup and record (KeyError).
            log.exception("terminal record failed for %s; requesting redelivery", key)
            await msg.nak(delay=1)  # type: ignore[attr-defined]
            return
        if not inserted:
            # Lost a same-key race: the winner's row is the truth; republish it.
            try:
                winner = await self._by_key(key)
            except Exception:
                # Uncaught, the task dies with the command unacked until the
                # 1h ack_wait; the redelivery dedupes on the winner's row.
                log.exception("winner read failed for %s; requesting redelivery", key)
                await msg.nak(delay=1)  # type: ignore[attr-defined]
                return
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
    "ABANDONED_MESSAGE",
    "ABANDON_RETRY_MAX_DELIVERIES",
    "EXEC_NAMESPACE",
    "RETRY_ON_ABANDON_TAG",
    "Executor",
    "Resolved",
    "lifecycle_event_id",
    "record_from_row",
    "resolve_outcome",
    "terminal_event_for",
]
