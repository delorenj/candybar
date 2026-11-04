"""Run one steward task end to end (steward-contract.md section 4).

Per consumed `bloodbank.cmd.cron.candybar.steward` command: mark the task
`running`, gather the context bundle and working directory, open an
OpenCode session with the task's model (enrich and evaluate sessions deny
file edits), send the rendered template, wait (enrich 15 min, fix and
refactor 30 min, evaluate 20 min; then interrupt and fail), read the final
```json block, validate it (one repair turn in the same session when it
does not validate), apply it, and mark the task `succeeded` or `failed`.
Every status move publishes its `bloodbank.cron.steward_task.*` fact before
the write.

Robustness, so no task stays queued or running forever:

- any exception fails the task with an `error`, and the session is
  interrupted on every non-success exit;
- the command is acked only once the terminal state is recorded; when that
  write fails the command is nak'd and the redelivery retries the same
  write (bounded; past that the startup reaper settles it);
- a command whose task row is not visible yet (the API publishes before it
  commits) is nak'd briefly and dropped after a few deliveries;
- a task is claimed only from `queued` (running -> running is refused) and
  a redelivery of a task this process is still running is left to the live
  run; one whose task is `running` with no live run here was lost (crash,
  kill): it is failed as abandoned, never re-run (a fix may already have
  pushed commits);
- on startup (`reap`) every task left `running` is failed (one steward
  instance: a restart orphaned it) and every `queued` task older than the
  command stream's max_age (its command expired) too;
- an operator cancel (`candybar steward cancel`, the row turns `failed`) is
  noticed by the keepalive, which stops the run and its session;
- a shutdown interrupts the OpenCode session and fails the task.

Fix and refactor change things, so they run one at a time per working
directory (an asyncio lock keyed by the resolved workdir) on top of the
API's one-per-candybar rule; a refactor applies the evaluation that queued
it (its parent's), never a later one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..api.app import candybar_detail_json, invocation_row_json
from ..events import STEWARD_FACT_FOR_STATUS, build_steward_task_fact, parse_envelope
from ..events.builders import (
    build_candybar_invoke,
    build_candybar_steward,
    build_candybar_updated,
)
from ..events.envelope import CandybarStewardCommand, Envelope
from ..registry import (
    STEWARD_TASK_LIST_MAX,
    active_steward_task,
    get_candybar,
    get_invocation,
    get_profile,
    get_steward_task,
    insert_steward_task,
    list_invocations,
    list_profiles,
    list_steward_tasks,
    lock_candybar,
    update_candybar,
    update_steward_task,
    upsert_profile,
    uuid7,
)
from .context import ContextBundle, build_context, fence, protection, redact
from .jev import Classification, Jev, JevError, classify_project_async
from .model import (
    FOLLOW_UP_FOR_OUTCOME,
    MODEL_FOR_ACTION,
    MUTATING_ACTIONS,
    READ_ONLY_ACTIONS,
    TERMINAL_STATUSES,
    DraftTask,
    Evaluation,
    EvaluationVerdict,
    Overview,
    OverviewSource,
    profile_json,
    stamp_transition,
    task_fact_data,
    task_json,
    transition_problem,
)
from .opencode import READ_ONLY_PERMISSIONS, OpenCodeClient, OpenCodeError
from .prompting import AnswerError, Template, last_json_block, load_template
from .reports import (
    ReportError,
    render_markdown,
    report_json_path,
    report_key,
    write_and_upload_async,
)

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[Any]]
Publish = Callable[[Envelope], Awaitable[None]]

# OpenCode budgets per action (contract section 4, step 3).
TIMEOUTS_S = {"enrich": 15 * 60, "fix": 30 * 60, "refactor": 30 * 60, "evaluate": 20 * 60}
# A repair turn (invalid answer) gets what is left, at most this, at least the floor.
REPAIR_MAX_S = 5 * 60
REPAIR_MIN_S = 45
# Fix / refactor proof: the runner's timeout, at most an hour (contract section 4, fix).
PROOF_MAX_S = 3600
PROOF_POLL_S = 5.0
# A command whose task row is not visible yet: retry this many deliveries.
MISSING_TASK_DELIVERIES = 6
# A terminal write that failed: the command is nak'd this long, this many times.
PENDING_RETRY_S = 60
PENDING_MAX_DELIVERIES = 30
KEEPALIVE_S = 60.0
FACT_ATTEMPTS = 3
ERROR_MAX = 2000
INVOCATION_CONTEXT = 30
# The command stream's max_age when it cannot be read (BLOODBANK_COMMANDS: 24 h).
DEFAULT_COMMAND_MAX_AGE_S = 86_400.0
# A profile classified as this project by the old ledger mapping (its ledger
# entry lives in this repo) is re-checked on startup.
OWN_PROJECT_ID = "candybar"

ABANDONED = (
    "abandoned: the steward lost this task mid-run (restart or crash) and does not re-run it; "
    "request it again"
)
STOPPED = "the steward stopped mid-task (shutdown); the OpenCode session was interrupted"
ORPHANED = (
    "orphaned: the task was running when the steward restarted (there is one steward, so nothing "
    "else owns it); it is not re-run, request it again"
)
EXPIRED = (
    "expired: queued for longer than the command stream keeps commands (max_age {hours:g} h), so "
    "its command is gone and it would never start; request it again"
)

FOLLOW_UP_PLAN = {
    "none": "nothing to do (Nominal)",
    "refactor": "a refactor task applies the recommendation and proves it with one manual run (Suboptimal)",
    "fix": "a fix task repairs it and proves the repair with one manual run (Drifted)",
    "archive": "the candybar is archived and disabled; `candybar unarchive` reverses it (Orphaned)",
}

REPAIR_PROMPT = """Your final answer did not pass validation:

{errors}

Do not redo the work. Reply with ONLY one corrected fenced ```json block that follows the required shape from my first message exactly (every field present, correct types, no extra keys, no empty strings in arrays)."""


class StewardError(RuntimeError):
    """A task cannot proceed; the message becomes the task's error."""


class AlreadyClaimed(StewardError):
    """The task is not `queued` any more: another delivery runs it (or it ended)."""


class FixAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    repaired: bool
    summary: str = Field(min_length=1)
    commits: list[str] = []


class RefactorAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    applied: bool
    summary: str = Field(min_length=1)
    commits: list[str] = []


@dataclass(frozen=True)
class FollowUp:
    """What an evaluate's follow-up did: a note for the summary and its stored status."""

    note: str
    status: str | None  # queued | done | flagged | failed; None for "none"
    task_id: UUID | None = None


def _known(model: type[BaseModel], data: Any) -> Any:
    """`data` with only the keys `model` declares (the agent may add commentary keys)."""
    if not isinstance(data, dict):
        return data
    return {k: v for k, v in data.items() if k in model.model_fields}


def validate_overview(data: dict[str, Any], runner_kind: str | None = None) -> Overview:
    """The overview; `source.runner` is set to the candybar's real runner kind when given."""
    body = data.get("overview", data)
    body = _known(Overview, body)
    if isinstance(body, dict) and isinstance(body.get("source"), dict):
        body["source"] = _known(OverviewSource, body["source"])
        if runner_kind is not None:
            body["source"]["runner"] = runner_kind
    return Overview.model_validate(body)


def validate_verdict(data: dict[str, Any]) -> EvaluationVerdict:
    return EvaluationVerdict.model_validate(_known(EvaluationVerdict, data.get("evaluation", data)))


def validate_fix(data: dict[str, Any]) -> FixAnswer:
    return FixAnswer.model_validate(data.get("fix", data))


def validate_refactor(data: dict[str, Any]) -> RefactorAnswer:
    return RefactorAnswer.model_validate(data.get("refactor", data))


def follow_up_status(action: str, outcome: str | None, *, failed: bool = False) -> str:
    """The evaluation's followUpStatus once its fix / refactor task ended."""
    if failed:
        return "failed"
    if action == "fix":
        return "done" if outcome == "fixed" else "failed"
    return {"done_proven": "done", "not_applied": "flagged"}.get(outcome or "", "failed")


def _brief(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        lines = [
            f"- {'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
            for err in exc.errors()[:12]
        ]
        return "\n".join(lines)
    return f"- {exc}"


def _error_text(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    if not isinstance(exc, (StewardError, OpenCodeError, AnswerError, ReportError, JevError)):
        text = f"{type(exc).__name__}: {text}"
    return text[:ERROR_MAX]


class Steward:
    """Executes steward tasks. One instance per role; `handle_message` per command."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        publish: Publish,
        opencode: OpenCodeClient | None = None,
        jev: Jev | None = None,
        stop: asyncio.Event | None = None,
        templates_dir: Path | None = None,
        reports_dir: Path | None = None,
        timeouts: dict[str, float] | None = None,
        context: Callable[..., Awaitable[ContextBundle]] = build_context,
        classify: Callable[..., Awaitable[Classification]] = classify_project_async,
        reports: Callable[..., Awaitable[Any]] = write_and_upload_async,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        proof_poll_s: float = PROOF_POLL_S,
        keepalive_s: float = KEEPALIVE_S,
        pending_retry_s: float = PENDING_RETRY_S,
    ) -> None:
        self._session = session_factory
        self._publish_raw = publish
        self._opencode = opencode or OpenCodeClient()
        self._jev = jev
        self._stop = stop or asyncio.Event()
        self._templates_dir = templates_dir
        self._reports_dir = reports_dir
        self._timeouts = {**TIMEOUTS_S, **(timeouts or {})}
        self._context = context
        self._classify = classify
        self._reports = reports
        self._clock = clock
        self._now = now
        self._proof_poll_s = proof_poll_s
        self._keepalive_s = keepalive_s
        self._pending_retry_s = pending_retry_s
        # Tasks this process is running, and the subset writing its terminal state.
        self._active: set[UUID] = set()
        self._closing: set[UUID] = set()
        # Runs stopped because the operator cancelled the task.
        self._cancelled: set[UUID] = set()
        # Terminal writes that failed, retried on the command's redelivery.
        self._pending: dict[UUID, tuple[str, dict[str, Any]]] = {}
        # Session spend per task (cost_usd, tokens), written with the terminal state.
        self._usage: dict[UUID, dict[str, Any]] = {}
        # One mutating task at a time per resolved working directory.
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # The command.

    async def handle_message(self, msg: Any) -> None:
        """Consume one steward command. Never raises (except cancellation)."""
        try:
            command = parse_envelope(json.loads(msg.data))
            if not isinstance(command, CandybarStewardCommand):
                raise TypeError(f"not a steward command: {command.type}")
        except Exception as exc:  # noqa: BLE001  (poison: never redeliver)
            log.error("steward: dropping an unparseable command (%s: %s)", type(exc).__name__, exc)
            await msg.term()
            return
        task_id = command.data.task_id
        if task_id in self._active:
            # Redelivered while the run is live here (a missed keepalive): the
            # live run acks the command when it ends.
            log.warning("steward: task %s redelivered while it runs here; leaving it to the live run", task_id)
            with suppress(Exception):
                await msg.in_progress()
            return
        try:
            async with self._session() as session:
                task = await get_steward_task(session, task_id)
                status = task.status if task is not None else None
        except Exception as exc:  # noqa: BLE001  (a registry blip: redeliver later)
            log.warning("steward: registry read for task %s failed (%s); retrying", task_id, exc)
            await msg.nak(delay=10)
            return
        if task_id in self._pending:
            # An earlier terminal write failed: retry exactly that write.
            if await self._flush(task_id):
                await msg.ack()
            else:
                await self._retry_later(msg, task_id)
            return
        if status is None:
            deliveries = self._deliveries(msg)
            if deliveries < MISSING_TASK_DELIVERIES:
                await msg.nak(delay=2)  # the API publishes before its row commits
                return
            log.warning("steward: task %s was never stored; dropping its command", task_id)
            await msg.ack()
            return
        if status in TERMINAL_STATUSES:
            await msg.ack()  # a redelivery after the task finished (or was cancelled)
            return
        if status == "running":
            log.warning("steward: task %s was running when redelivered; failing it as abandoned", task_id)
            if await self._fail(task_id, ABANDONED):
                await msg.ack()
            else:
                await self._retry_later(msg, task_id)
            return
        self._active.add(task_id)
        work = asyncio.create_task(self.run(task_id), name=f"candybar-steward-{task_id}")
        keepalive = asyncio.create_task(self._keepalive(msg, task_id, work))
        try:
            recorded = await work
        except asyncio.CancelledError:
            # Shutdown: run() recorded the failure (or the startup reaper will).
            with suppress(Exception):
                await asyncio.shield(msg.ack())
            raise
        finally:
            self._active.discard(task_id)
            self._closing.discard(task_id)
            self._cancelled.discard(task_id)
            keepalive.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await keepalive
        if recorded:
            await msg.ack()
        else:
            await self._retry_later(msg, task_id)

    @staticmethod
    def _deliveries(msg: Any) -> int:
        return getattr(getattr(msg, "metadata", None), "num_delivered", 1) or 1

    async def _retry_later(self, msg: Any, task_id: UUID) -> None:
        """The terminal write failed: nak for a retry, a bounded number of times."""
        if self._deliveries(msg) >= PENDING_MAX_DELIVERIES:
            log.error(
                "steward: task %s still unrecorded after %d deliveries; giving up (the startup reaper "
                "fails it on the next start)", task_id, self._deliveries(msg),
            )
            self._pending.pop(task_id, None)
            await msg.ack()
            return
        await msg.nak(delay=self._pending_retry_s)

    async def _flush(self, task_id: UUID) -> bool:
        status, changes = self._pending[task_id]
        return await self._finish(task_id, status, changes, refuse_terminal=status != "failed")

    async def _keepalive(self, msg: Any, task_id: UUID, work: asyncio.Task[Any]) -> None:
        """Keep the command alive, and stop the run when the operator cancelled its task."""
        while True:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._keepalive_s)
                return
            except TimeoutError:
                pass
            with suppress(Exception):
                await msg.in_progress()
            if work.done() or task_id in self._closing:
                continue
            try:
                async with self._session() as session:
                    row = await get_steward_task(session, task_id)
                    status = row.status if row is not None else None
            except Exception as exc:  # noqa: BLE001  (a registry blip: check again next beat)
                log.debug("steward: status check of task %s failed: %s", task_id, exc)
                continue
            if status in TERMINAL_STATUSES and task_id not in self._closing and not work.done():
                log.warning("steward: task %s turned %s under its run (cancelled); stopping it", task_id, status)
                self._cancelled.add(task_id)
                work.cancel()
                return

    # ------------------------------------------------------------------
    # One task.

    async def run(self, task_id: UUID) -> bool:
        """Run the task to a terminal status; True once that status is recorded.

        Failures become the task's error. False means the terminal write
        failed and is pending (the caller naks the command for a retry).
        """
        task: Any = None
        try:
            async with self._session() as session:
                task = await get_steward_task(session, task_id)
                candybar = await get_candybar(session, task.candybar_id) if task is not None else None
            if task is None:
                raise StewardError("the task vanished")
            if candybar is None:
                raise StewardError("the candybar is no longer registered")
            await self._transition(task_id, "running", {}, claim=True)
            log.info("steward: %s %s started (task %s, model %s)", task.action, candybar.name, task_id, task.model)
            handler = {
                "enrich": self._enrich,
                "evaluate": self._evaluate,
                "fix": self._fix,
                "refactor": self._refactor,
            }[task.action]
            changes = await handler(task, candybar)
        except AlreadyClaimed as exc:
            log.warning("steward: task %s not run: %s", task_id, exc)
            return True
        except asyncio.CancelledError:
            if task_id in self._cancelled:
                log.info("steward: task %s cancelled by the operator; its session was interrupted", task_id)
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                self._usage.pop(task_id, None)
                if task is not None:
                    await self._settle_follow_up(task, "failed")
                return True
            await asyncio.shield(self._fail(task_id, STOPPED))
            raise
        except Exception as exc:
            log.exception("steward: task %s failed", task_id)
            recorded = await self._fail(task_id, _error_text(exc))
            if task is not None and task.action in MUTATING_ACTIONS:
                await self._settle_follow_up(task, "failed")
            return recorded
        self._closing.add(task_id)
        recorded = await self._finish(task_id, "succeeded", changes)
        if task.action in MUTATING_ACTIONS:
            await self._settle_follow_up(task, follow_up_status(task.action, changes.get("outcome")))
        log.info("steward: %s %s succeeded: %s", task.action, candybar.name, changes.get("outcome"))
        return recorded

    async def _fail(self, task_id: UUID, error: str) -> bool:
        """Mark the task failed (with `error`). Never raises (the consumer must survive)."""
        return await self._finish(task_id, "failed", {"error": error[:ERROR_MAX]}, refuse_terminal=False)

    async def _finish(
        self, task_id: UUID, status: str, changes: dict[str, Any], *, refuse_terminal: bool = True
    ) -> bool:
        """Write a terminal status (with the session's spend). False when the write failed."""
        changes = {**changes, **self._usage.pop(task_id, {})}
        try:
            await self._transition(task_id, status, changes, refuse_terminal=refuse_terminal)
        except StewardError as exc:
            # Refused (already terminal: cancelled by the operator) or gone:
            # nothing is left to record.
            log.warning("steward: task %s not marked %s: %s", task_id, status, exc)
            self._pending.pop(task_id, None)
            return True
        except Exception:
            log.exception("steward: could not record task %s as %s; its command is retried", task_id, status)
            self._pending[task_id] = (status, changes)
            return False
        self._pending.pop(task_id, None)
        return True

    async def _transition(
        self,
        task_id: UUID,
        status: str,
        changes: dict[str, Any],
        *,
        refuse_terminal: bool = True,
        claim: bool = False,
    ) -> None:
        """Move a task to `status` with `changes`: the fact first, then the write.

        `claim` (the move to running) only succeeds from `queued`; a move to
        the status the task already has is refused (no running -> running).
        """
        async with self._session() as session:
            row = await get_steward_task(session, task_id, lock=True)
            if row is None:
                raise StewardError("the task vanished")
            if row.status in TERMINAL_STATUSES and not refuse_terminal:
                return  # already terminal: a late failure changes nothing
            if claim and row.status != "queued":
                raise AlreadyClaimed(f"it is already {row.status}")
            if row.status == status:
                raise StewardError(f"the task is already {status}")
            refusal = transition_problem(row.status, status)
            if refusal is not None:
                raise StewardError(refusal)
            stamped = stamp_transition(row.status, {"status": status, **changes}, self._now())
            candybar = await get_candybar(session, row.candybar_id)
            name = candybar.name if candybar is not None else "unknown"
            fact = build_steward_task_fact(
                STEWARD_FACT_FOR_STATUS[status],
                task_fact_data(row, name, stamped),
                correlationid=row.parent_task_id or row.id,
                causationid=row.parent_task_id,
            )
            await self._publish_fact(fact)
            await update_steward_task(session, row, stamped)

    async def _patch(self, task_id: UUID, changes: dict[str, Any]) -> None:
        """A write with no status move (session id, proving invocation): no fact."""
        async with self._session() as session:
            row = await get_steward_task(session, task_id, lock=True)
            if row is not None and row.status not in TERMINAL_STATUSES:
                await update_steward_task(session, row, changes)

    async def _publish_fact(self, fact: Envelope) -> None:
        """Publish a task fact, retrying; a bus that stays down never wedges the task."""
        for attempt in range(1, FACT_ATTEMPTS + 1):
            try:
                await self._publish_raw(fact)
                return
            except Exception as exc:  # noqa: BLE001  (any bus failure: retry, then write anyway)
                if attempt == FACT_ATTEMPTS:
                    log.error("steward: %s for task %s not published (%s); writing anyway", fact.type, fact.data.task_id, exc)  # type: ignore[attr-defined]
                    return
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=attempt)

    # ------------------------------------------------------------------
    # Startup chores.

    async def reap(self, *, max_age_s: float = DEFAULT_COMMAND_MAX_AGE_S) -> list[dict[str, str]]:
        """Fail tasks no command will ever finish: every `running` one, and `queued` past max_age."""
        cutoff = self._now() - timedelta(seconds=max_age_s)
        async with self._session() as session:
            rows = await list_steward_tasks(session, active=True, limit=STEWARD_TASK_LIST_MAX)
            snapshot = [(r.id, r.status, r.action, r.candybar_id, r.created_at) for r in rows]
        reaped: list[dict[str, str]] = []
        for task_id, status, action, candybar_id, created_at in snapshot:
            if task_id in self._active:
                continue
            if status == "running":
                error = ORPHANED
            elif created_at < cutoff:
                error = EXPIRED.format(hours=round(max_age_s / 3600, 2))
            else:
                continue
            if await self._fail(task_id, error):
                reaped.append({"taskId": str(task_id), "action": action, "was": status, "error": error})
                if action in MUTATING_ACTIONS:
                    await self._settle_follow_up(SimpleNamespace(id=task_id, candybar_id=candybar_id), "failed")
        return reaped

    async def reclassify_misattributed(self) -> list[dict[str, Any]]:
        """Re-run the project step (no agent) for profiles the old ledger mapping called this repo's.

        A ledger entry living in this repo once made enrich classify the job
        as the candybar project at confidence 1. With the job's real checkout
        the answer is the job's own project, a Jev match, or none; a job that
        really lives here keeps its project.
        """
        async with self._session() as session:
            profiles = await list_profiles(session)
            targets = [
                (p.candybar_id, p.project, p.overview)
                for p in profiles
                if isinstance(p.project, dict)
                and p.project.get("method") == "ledger"
                and p.project.get("id") == OWN_PROJECT_ID
            ]
        changed: list[dict[str, Any]] = []
        for candybar_id, project, overview in targets:
            try:
                async with self._session() as session:
                    row = await get_candybar(session, candybar_id)
                if row is None:
                    continue
                bundle = await self._bundle(row)
                source = (overview or {}).get("source") or {}
                found = await self._classify(
                    definition=bundle.definition,
                    overview=overview,
                    paths=list(source.get("paths") or []),
                    checkout=bundle.checkout,
                    jev=self._jev,
                )
            except Exception as exc:  # noqa: BLE001  (Jev or registry down: keep it, retry next start)
                log.warning("steward: reclassifying %s failed (%s); kept as %s", candybar_id, exc, project)
                continue
            if found.project == project:
                continue
            async with self._session() as session:
                await upsert_profile(session, candybar_id, {"project": found.project})
            changed.append(
                {
                    "name": row.name,
                    "from": project.get("id"),
                    "to": (found.project or {}).get("id"),
                    "note": found.note,
                }
            )
        return changed

    # ------------------------------------------------------------------
    # Shared steps.

    async def _bundle(self, candybar: Any) -> ContextBundle:
        async with self._session() as session:
            row = await get_candybar(session, candybar.id)
            if row is None:
                raise StewardError("the candybar is no longer registered")
            definition = candybar_detail_json(row)
            runner = row.runner
            entrypoint = None if row.runner is not None else row.entrypoint
            runs = [invocation_row_json(r) for r in await list_invocations(session, row.id, INVOCATION_CONTEXT)]
            stored = await get_profile(session, row.id)
            profile = profile_json(row.id, stored) if stored is not None else None
        return await self._context(
            definition=definition, runner=runner, entrypoint=entrypoint, invocations=runs, profile=profile
        )

    def _template(self, action: str) -> Template:
        return load_template(action, self._templates_dir)

    def _render(self, template: Template, task: Any, bundle: ContextBundle, evaluation: dict[str, Any] | None = None) -> str:
        recommendation = (evaluation or {}).get("recommendation")
        values = {
            "name": bundle.name,
            "candybar_id": str(task.candybar_id),
            "task_id": str(task.id),
            "action": task.action,
            "runner_kind": bundle.runner_kind,
            "workdir": str(bundle.workdir),
            "repo": bundle.repo_slug or "(none recorded)",
            "template_version": str(template.version),
            # Fix and refactor quote their own evaluation (the parent's), not the profile's latest.
            "context": bundle.render(include_evaluation=task.action not in MUTATING_ACTIONS),
            # Agent-written text: fenced like any other evidence.
            "evaluation": fence(redact(json.dumps(evaluation, indent=2)), "json") if evaluation else "(no stored evaluation)",
            "recommendation": fence(redact(str(recommendation))) if recommendation else "(no stored recommendation)",
        }
        return template.render(values)

    async def _evaluation_for(self, task: Any, bundle: ContextBundle) -> dict[str, Any] | None:
        """The evaluation a task works from: its parent's when it has one, else the latest stored.

        The profile holds only the latest evaluation; when a later evaluate
        replaced the parent's, the parent's report `.json` still has it.
        """
        stored = (bundle.profile or {}).get("evaluation")
        parent = task.parent_task_id
        if parent is None:
            return stored if isinstance(stored, dict) else None
        if isinstance(stored, dict) and str(stored.get("taskId")) == str(parent):
            return stored
        async with self._session() as session:
            parent_row = await get_steward_task(session, parent)
            key = getattr(parent_row, "report_key", None) if parent_row is not None else None
        if not key:
            return None
        path = report_json_path(key, self._reports_dir)
        try:
            data = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
        except (OSError, ValueError):
            return None
        found = data.get("evaluation") if isinstance(data, dict) else None
        if isinstance(found, dict) and str(found.get("taskId")) == str(parent):
            return found
        return None

    def _workdir_lock(self, workdir: Path) -> asyncio.Lock:
        try:
            key = str(Path(workdir).resolve())
        except OSError:
            key = str(workdir)
        return self._locks.setdefault(key, asyncio.Lock())

    async def _converse(
        self, task: Any, bundle: ContextBundle, prompt: str, validate: Callable[[dict[str, Any]], Any]
    ) -> Any:
        """One OpenCode session: the prompt, then at most one repair turn. Returns the validated answer.

        The session is interrupted on every exit that is not a valid answer
        (timeout, failure, invalid answer, cancellation), and its spend is
        recorded for the task either way.
        """
        budget = float(self._timeouts[task.action])
        deadline = self._clock() + budget
        permissions = [dict(rule) for rule in READ_ONLY_PERMISSIONS] if task.action in READ_ONLY_ACTIONS else None
        endpoint, session_id = await self._opencode.start(
            title=f"candybar steward: {task.action} {bundle.name}",
            model=task.model,
            directory=str(bundle.workdir),
            permissions=permissions,
        )
        try:
            await self._patch(task.id, {"session_id": session_id})
            turn = await self._opencode.turn(endpoint, session_id, prompt, budget)
            try:
                return validate(last_json_block(turn.answers))
            except (AnswerError, ValidationError, ValueError) as exc:
                remaining = deadline - self._clock()
                if remaining < REPAIR_MIN_S:
                    raise StewardError(f"invalid answer and no time left to repair it:\n{_brief(exc)}") from exc
                log.info("steward: task %s answer invalid; asking for a repair", task.id)
                repair = REPAIR_PROMPT.format(errors=_brief(exc))
                turn = await self._opencode.turn(endpoint, session_id, repair, min(remaining, REPAIR_MAX_S))
                try:
                    return validate(last_json_block(turn.answers))
                except (AnswerError, ValidationError, ValueError) as again:
                    raise StewardError(f"invalid answer after one repair turn:\n{_brief(again)}") from again
        except BaseException:
            with suppress(Exception):
                await asyncio.shield(self._opencode.stop(endpoint, session_id))
            raise
        finally:
            with suppress(Exception):
                cost, tokens = await asyncio.shield(self._opencode.spend(endpoint, session_id))
                usage = {k: v for k, v in (("cost_usd", cost), ("tokens", tokens)) if v is not None}
                if usage:
                    self._usage[task.id] = usage

    # ------------------------------------------------------------------
    # Actions.

    async def _enrich(self, task: Any, candybar: Any) -> dict[str, Any]:
        bundle = await self._bundle(candybar)
        template = self._template("enrich")
        overview: Overview = await self._converse(
            task, bundle, self._render(template, task, bundle),
            lambda data: validate_overview(data, bundle.runner_kind),
        )
        changes: dict[str, Any] = {
            "overview": overview.model_dump(mode="json"),
            "template_version": template.version,
            "enriched_at": self._now(),
            "enrich_task_id": task.id,
        }
        stored = (bundle.profile or {}).get("project")
        if stored and stored.get("method") == "manual":
            note = f"project kept: {stored['id']} (manual override)"
        else:
            try:
                found = await self._classify(
                    definition=bundle.definition,
                    overview=changes["overview"],
                    paths=list(overview.source.paths),
                    checkout=bundle.checkout,
                    jev=self._jev,
                )
                changes["project"] = found.project
                note = f"project: {found.project['id'] if found.project else 'none'} ({found.note})"
            except (JevError, OSError) as exc:
                # The overview stands; an ingress outage must not erase a
                # previous classification.
                note = f"project unchanged (classification failed: {exc})"
        async with self._session() as session:
            await upsert_profile(session, candybar.id, changes)
        return {"outcome": "done", "summary": f"{overview.summary} {note[0].upper()}{note[1:]}."}

    async def _evaluate(self, task: Any, candybar: Any) -> dict[str, Any]:
        bundle = await self._bundle(candybar)
        template = self._template("evaluate")
        verdict: EvaluationVerdict = await self._converse(
            task, bundle, self._render(template, task, bundle), validate_verdict
        )
        moment = self._now()
        follow_up = FOLLOW_UP_FOR_OUTCOME[verdict.outcome]
        protected = protection(bundle.definition) if follow_up == "archive" else None
        evaluation = Evaluation(
            **verdict.model_dump(exclude={"followUp"}),
            followUp=follow_up,
            reportKey=report_key(bundle.name, moment, task.id),
            evaluatedAt=moment,
            taskId=task.id,
        )
        payload = evaluation.model_dump(mode="json")
        async with self._session() as session:
            row = await get_steward_task(session, task.id)
            task_view = task_json(row) if row is not None else {"id": str(task.id), "model": task.model}
        plan = (
            f"flagged for the operator, not archived: {bundle.name} is protected ({protected})"
            if protected
            else FOLLOW_UP_PLAN[follow_up]
        )
        markdown = render_markdown(
            name=bundle.name,
            evaluation=payload,
            definition=bundle.definition,
            runner=bundle.runner,
            task=task_view,
            runs=bundle.invocations,
            follow_up_note=plan,
        )
        await self._reports(
            name=bundle.name,
            evaluation=payload,
            markdown=markdown,
            payload={
                "candybar": {
                    k: bundle.definition.get(k) for k in ("id", "name", "schedule", "timezone", "enabled", "archived")
                },
                "runner": bundle.runner,
                "evaluation": payload,
                "task": task_view,
                "invocations": bundle.invocations,
            },
        )
        async with self._session() as session:
            await upsert_profile(session, candybar.id, {"evaluation": payload})
        await self._patch(task.id, {"outcome": verdict.outcome, "report_key": evaluation.reportKey})
        try:
            result = await self._follow_up(task, candybar.id, follow_up, bundle.definition)
        except Exception as exc:
            await self._mark_evaluation(candybar.id, task.id, "failed")
            raise StewardError(
                f"evaluation stored ({verdict.outcome}, {evaluation.reportKey}) but the {follow_up} "
                f"follow-up failed: {exc}"
            ) from exc
        if result.status is not None:
            await self._mark_evaluation(candybar.id, task.id, result.status, result.task_id)
        return {
            "outcome": verdict.outcome,
            "summary": f"{verdict.summary} Follow-up: {result.note}",
            "report_key": evaluation.reportKey,
        }

    async def _fix(self, task: Any, candybar: Any) -> dict[str, Any]:
        if getattr(candybar, "archived", False):
            raise StewardError("archived candybars are not fixed; unarchive it first")
        bundle = await self._bundle(candybar)
        evaluation = await self._evaluation_for(task, bundle)
        template = self._template("fix")
        lock = self._workdir_lock(bundle.workdir)
        if lock.locked():
            log.info("steward: fix %s waits for another mutating task in %s", task.id, bundle.workdir)
        async with lock:
            answer: FixAnswer = await self._converse(
                task, bundle, self._render(template, task, bundle, evaluation), validate_fix
            )
            commits = f" Commits: {', '.join(answer.commits[:5])}." if answer.commits else ""
            if not answer.repaired:
                result = {"outcome": "not_fixed", "summary": f"{answer.summary}{commits}"}
            else:
                invocation_id, row = await self._prove(task, candybar)
                outcome, verdict = self._proof_verdict(row)
                result = {
                    "outcome": "fixed" if outcome else "not_fixed",
                    "summary": f"{answer.summary}{commits} Proving run {invocation_id}: {verdict}.",
                    "invocation_id": invocation_id,
                }
        if answer.commits:
            refresh = await self._queue(task, candybar.id, "enrich")
            result["summary"] += f" Overview refresh: {refresh.note}"
        return result

    async def _refactor(self, task: Any, candybar: Any) -> dict[str, Any]:
        if getattr(candybar, "archived", False):
            raise StewardError("archived candybars are not refactored; unarchive it first")
        if task.parent_task_id is None:
            raise StewardError("a refactor applies the evaluation that queued it, and this one has no parent task")
        bundle = await self._bundle(candybar)
        evaluation = await self._evaluation_for(task, bundle)
        if evaluation is None:
            raise StewardError(
                f"the evaluation of parent task {task.parent_task_id} is no longer available (a later evaluate "
                "replaced it and its report file is gone); nothing was applied"
            )
        template = self._template("refactor")
        lock = self._workdir_lock(bundle.workdir)
        if lock.locked():
            log.info("steward: refactor %s waits for another mutating task in %s", task.id, bundle.workdir)
        async with lock:
            answer: RefactorAnswer = await self._converse(
                task, bundle, self._render(template, task, bundle, evaluation), validate_refactor
            )
            commits = f" Commits: {', '.join(answer.commits[:5])}." if answer.commits else ""
            if not answer.applied:
                result: dict[str, Any] = {"outcome": "not_applied", "summary": f"{answer.summary} (not applied){commits}"}
            else:
                invocation_id, row = await self._prove(task, candybar)
                proven, verdict = self._proof_verdict(row)
                result = {
                    "outcome": "done_proven" if proven else "done_not_proven",
                    "summary": f"{answer.summary}{commits} Proving run {invocation_id}: {verdict}.",
                    "invocation_id": invocation_id,
                }
        if answer.commits:
            refresh = await self._queue(task, candybar.id, "enrich")
            result["summary"] += f" Overview refresh: {refresh.note}"
        return result

    @staticmethod
    def _proof_verdict(row: Any | None) -> tuple[bool, str]:
        if row is None:
            return False, "did not finish in time (or was skipped as an overlap)"
        return row.result == "success", f"{row.result} (exit {row.exit_code})"

    # ------------------------------------------------------------------
    # Follow-ups and the proof.

    async def _follow_up(self, parent: Any, candybar_id: UUID, follow_up: str, definition: dict[str, Any]) -> FollowUp:
        if follow_up == "none":
            return FollowUp("none.", None)
        if follow_up == "archive":
            reason = protection(definition)
            if reason is not None:
                name = definition.get("name")
                return FollowUp(
                    f"flagged, not archived: {name} is protected ({reason}); archive it by hand "
                    f"(`candybar archive {name}`) if it really is unused.",
                    "flagged",
                )
            return FollowUp(await self._archive(candybar_id), "done")
        return await self._queue(parent, candybar_id, follow_up)

    async def _queue(self, parent: Any, candybar_id: UUID, action: str) -> FollowUp:
        """Queue a follow-up task: command and `queued` fact first, then the row (the API's flow).

        A fix or refactor waits its turn: none is queued while the candybar
        has a fix or refactor queued or running (one change at a time).
        """
        async with self._session() as session:
            row = await lock_candybar(session, candybar_id)
            if row is None:
                return FollowUp(f"{action} skipped: the candybar is gone.", "flagged")
            if action in MUTATING_ACTIONS and getattr(row, "archived", False):
                return FollowUp(f"{action} skipped: the candybar is archived.", "flagged")
            for other in MUTATING_ACTIONS if action in MUTATING_ACTIONS else (action,):
                active = await active_steward_task(session, candybar_id, other)
                if active is None:
                    continue
                if other == action:
                    return FollowUp(f"{action} already {active.status} as task {active.id}.", "queued", active.id)
                return FollowUp(
                    f"{action} not queued: a {other} is {active.status} as task {active.id} (one change at a "
                    "time); request it again once that ends.",
                    "flagged",
                )
            draft = DraftTask(
                id=uuid7(),
                candybar_id=candybar_id,
                action=action,
                model=MODEL_FOR_ACTION[action],
                created_at=self._now(),
                parent_task_id=parent.id,
            )
            root = parent.parent_task_id or parent.id
            command = build_candybar_steward(
                task_id=draft.id, candybar_id=candybar_id, action=action, correlationid=root, causationid=parent.id
            )
            fact = build_steward_task_fact(
                STEWARD_FACT_FOR_STATUS["queued"],
                task_fact_data(draft, row.name),
                correlationid=root,
                causationid=parent.id,
            )
            await self._publish_raw(command)
            await self._publish_raw(fact)
            await insert_steward_task(
                session,
                task_id=draft.id,
                candybar_id=candybar_id,
                action=action,
                model=draft.model,
                parent_task_id=parent.id,
                created_at=draft.created_at,
            )
        return FollowUp(f"queued {action} task {draft.id}.", "queued", draft.id)

    async def _mark_evaluation(
        self, candybar_id: UUID, evaluate_task_id: UUID, status: str, follow_task_id: UUID | None = None
    ) -> None:
        """Record the follow-up's status (and task) on the evaluation that evaluate task stored."""
        try:
            async with self._session() as session:
                if await lock_candybar(session, candybar_id) is None:
                    return
                stored = await get_profile(session, candybar_id)
                evaluation = getattr(stored, "evaluation", None) if stored is not None else None
                if not isinstance(evaluation, dict) or str(evaluation.get("taskId")) != str(evaluate_task_id):
                    return
                marked = {
                    **evaluation,
                    "followUpStatus": status,
                    "followUpTaskId": str(follow_task_id) if follow_task_id else None,
                }
                await upsert_profile(session, candybar_id, {"evaluation": marked})
        except Exception:
            log.exception("steward: could not mark the follow-up of evaluation %s", evaluate_task_id)

    async def _settle_follow_up(self, task: Any, status: str) -> None:
        """A follow-up task ended: update the evaluation that points at it (followUpTaskId)."""
        try:
            async with self._session() as session:
                if await lock_candybar(session, task.candybar_id) is None:
                    return
                stored = await get_profile(session, task.candybar_id)
                evaluation = getattr(stored, "evaluation", None) if stored is not None else None
                if not isinstance(evaluation, dict) or str(evaluation.get("followUpTaskId")) != str(task.id):
                    return
                await upsert_profile(session, task.candybar_id, {"evaluation": {**evaluation, "followUpStatus": status}})
        except Exception:
            log.exception("steward: could not settle the follow-up status for task %s", task.id)

    async def _archive(self, candybar_id: UUID) -> str:
        """Archive (and disable) in one `updated` fact and one write, as the API does."""
        async with self._session() as session:
            row = await lock_candybar(session, candybar_id)
            if row is None:
                return "archive skipped: the candybar is gone."
            wanted = {"archived": True, "enabled": False}
            changes = {k: v for k, v in wanted.items() if getattr(row, k, None) != v}
            if not changes:
                return "already archived."
            await self._publish_raw(
                build_candybar_updated(candybar_id=candybar_id, name=row.name, changes=changes)
            )
            await update_candybar(session, row, changes)
        return "archived and disabled (`candybar unarchive` reverses it)."

    async def _prove(self, task: Any, candybar: Any) -> tuple[UUID, Any | None]:
        """One manual invoke through the executor's command, waited on (the runner's timeout, max 1 h)."""
        command = build_candybar_invoke(candybar_id=candybar.id, name=candybar.name, trigger="manual")
        invocation_id = command.data.invocation_id
        await self._patch(task.id, {"invocation_id": invocation_id})
        await self._publish_raw(command)
        runner = candybar.runner or {}
        limit = runner.get("timeout_s") if isinstance(runner.get("timeout_s"), (int, float)) else None
        timeout = min(float(limit) + 120 if limit else PROOF_MAX_S, PROOF_MAX_S)
        deadline = self._clock() + timeout
        while True:
            async with self._session() as session:
                row = await get_invocation(session, invocation_id)
                if row is not None and row.result is not None:
                    return invocation_id, row
            if self._stop.is_set():
                raise StewardError(f"the steward stopped while waiting for proving run {invocation_id}")
            if self._clock() >= deadline:
                return invocation_id, None
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._proof_poll_s)
