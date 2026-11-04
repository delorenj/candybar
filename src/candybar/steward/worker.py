"""Run one steward task end to end (steward-contract.md section 4).

Per consumed `bloodbank.cmd.cron.candybar.steward` command: mark the task
`running`, gather the context bundle and working directory, open an
OpenCode session with the task's model, send the rendered template, wait
(enrich 15 min, fix and refactor 30 min, evaluate 20 min; then interrupt and
fail), read the final ```json block, validate it (one repair turn in the
same session when it does not validate), apply it, and mark the task
`succeeded` or `failed`. Every status move publishes its
`bloodbank.cron.steward_task.*` fact before the write.

Robustness: any exception fails the task with an `error` and the command is
acked, so a bad task never wedges the consumer. A command whose task row is
not visible yet (the API publishes before it commits) is nak'd briefly and
dropped after a few deliveries; a redelivered command whose task is already
`running` means the steward lost it (crash, kill): it is failed as
abandoned, never re-run (a fix may already have pushed commits). A shutdown
interrupts the OpenCode session and fails the task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, suppress
from datetime import UTC, datetime
from pathlib import Path
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
    active_steward_task,
    get_candybar,
    get_invocation,
    get_profile,
    get_steward_task,
    insert_steward_task,
    list_invocations,
    lock_candybar,
    update_candybar,
    update_steward_task,
    upsert_profile,
    uuid7,
)
from .context import ContextBundle, build_context
from .jev import Classification, Jev, JevError, classify_project_async
from .model import (
    FOLLOW_UP_FOR_OUTCOME,
    MODEL_FOR_ACTION,
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
from .opencode import OpenCodeClient, OpenCodeError
from .prompting import AnswerError, Template, last_json_block, load_template
from .reports import ReportError, render_markdown, report_key, write_and_upload_async

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[Any]]
Publish = Callable[[Envelope], Awaitable[None]]

# OpenCode budgets per action (contract section 4, step 3).
TIMEOUTS_S = {"enrich": 15 * 60, "fix": 30 * 60, "refactor": 30 * 60, "evaluate": 20 * 60}
# A repair turn (invalid answer) gets what is left, at most this, at least the floor.
REPAIR_MAX_S = 5 * 60
REPAIR_MIN_S = 45
# Fix proof: the runner's timeout, at most an hour (contract section 4, fix).
PROOF_MAX_S = 3600
PROOF_POLL_S = 5.0
# A command whose task row is not visible yet: retry this many deliveries.
MISSING_TASK_DELIVERIES = 6
KEEPALIVE_S = 60.0
FACT_ATTEMPTS = 3
ERROR_MAX = 2000
INVOCATION_CONTEXT = 30

ABANDONED = (
    "abandoned: the steward lost this task mid-run (restart or crash) and does not re-run it; "
    "request it again"
)
STOPPED = "the steward stopped mid-task (shutdown); the OpenCode session was interrupted"

FOLLOW_UP_PLAN = {
    "none": "nothing to do (Nominal)",
    "refactor": "a refactor task applies the recommendation (Suboptimal)",
    "fix": "a fix task repairs it and proves the repair with one manual run (Drifted)",
    "archive": "the candybar is archived and disabled; `candybar unarchive` reverses it (Orphaned)",
}

REPAIR_PROMPT = """Your final answer did not pass validation:

{errors}

Do not redo the work. Reply with ONLY one corrected fenced ```json block that follows the required shape from my first message exactly (every field present, correct types, no extra keys)."""


class StewardError(RuntimeError):
    """A task cannot proceed; the message becomes the task's error."""


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


def _known(model: type[BaseModel], data: Any) -> Any:
    """`data` with only the keys `model` declares (the agent may add commentary keys)."""
    if not isinstance(data, dict):
        return data
    return {k: v for k, v in data.items() if k in model.model_fields}


def validate_overview(data: dict[str, Any]) -> Overview:
    body = data.get("overview", data)
    body = _known(Overview, body)
    if isinstance(body, dict) and isinstance(body.get("source"), dict):
        body["source"] = _known(OverviewSource, body["source"])
    return Overview.model_validate(body)


def validate_verdict(data: dict[str, Any]) -> EvaluationVerdict:
    return EvaluationVerdict.model_validate(_known(EvaluationVerdict, data.get("evaluation", data)))


def validate_fix(data: dict[str, Any]) -> FixAnswer:
    return FixAnswer.model_validate(data.get("fix", data))


def validate_refactor(data: dict[str, Any]) -> RefactorAnswer:
    return RefactorAnswer.model_validate(data.get("refactor", data))


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
        timeouts: dict[str, float] | None = None,
        context: Callable[..., Awaitable[ContextBundle]] = build_context,
        classify: Callable[..., Awaitable[Classification]] = classify_project_async,
        reports: Callable[..., Awaitable[Any]] = write_and_upload_async,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        proof_poll_s: float = PROOF_POLL_S,
        keepalive_s: float = KEEPALIVE_S,
    ) -> None:
        self._session = session_factory
        self._publish_raw = publish
        self._opencode = opencode or OpenCodeClient()
        self._jev = jev
        self._stop = stop or asyncio.Event()
        self._templates_dir = templates_dir
        self._timeouts = {**TIMEOUTS_S, **(timeouts or {})}
        self._context = context
        self._classify = classify
        self._reports = reports
        self._clock = clock
        self._now = now
        self._proof_poll_s = proof_poll_s
        self._keepalive_s = keepalive_s

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
        try:
            async with self._session() as session:
                task = await get_steward_task(session, task_id)
                status = task.status if task is not None else None
        except Exception as exc:  # noqa: BLE001  (a registry blip: redeliver later)
            log.warning("steward: registry read for task %s failed (%s); retrying", task_id, exc)
            await msg.nak(delay=10)
            return
        if status is None:
            deliveries = getattr(getattr(msg, "metadata", None), "num_delivered", 1) or 1
            if deliveries < MISSING_TASK_DELIVERIES:
                await msg.nak(delay=2)  # the API publishes before its row commits
                return
            log.warning("steward: task %s was never stored; dropping its command", task_id)
            await msg.ack()
            return
        if status in TERMINAL_STATUSES:
            await msg.ack()  # a redelivery after the task finished
            return
        if status == "running":
            log.warning("steward: task %s was running when redelivered; failing it as abandoned", task_id)
            await self._fail(task_id, ABANDONED)
            await msg.ack()
            return
        keepalive = asyncio.create_task(self._keepalive(msg))
        try:
            await self.run(task_id)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(msg.ack())
            raise
        finally:
            keepalive.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await keepalive
        await msg.ack()

    async def _keepalive(self, msg: Any) -> None:
        while True:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._keepalive_s)
                return
            except TimeoutError:
                pass
            with suppress(Exception):
                await msg.in_progress()

    # ------------------------------------------------------------------
    # One task.

    async def run(self, task_id: UUID) -> None:
        """Run the task to a terminal status. Failures become the task's error."""
        try:
            async with self._session() as session:
                task = await get_steward_task(session, task_id)
                candybar = await get_candybar(session, task.candybar_id) if task is not None else None
            if task is None:
                raise StewardError("the task vanished")
            if candybar is None:
                raise StewardError("the candybar is no longer registered")
            await self._transition(task_id, "running", {})
            log.info("steward: %s %s started (task %s, model %s)", task.action, candybar.name, task_id, task.model)
            handler = {
                "enrich": self._enrich,
                "evaluate": self._evaluate,
                "fix": self._fix,
                "refactor": self._refactor,
            }[task.action]
            changes = await handler(task, candybar)
            await self._transition(task_id, "succeeded", changes)
            log.info("steward: %s %s succeeded: %s", task.action, candybar.name, changes.get("outcome"))
        except asyncio.CancelledError:
            await asyncio.shield(self._fail(task_id, STOPPED))
            raise
        except Exception as exc:
            log.exception("steward: task %s failed", task_id)
            await self._fail(task_id, _error_text(exc))

    async def _fail(self, task_id: UUID, error: str) -> None:
        """Mark the task failed (with `error`). Never raises: the consumer must survive."""
        try:
            await self._transition(task_id, "failed", {"error": error[:ERROR_MAX]}, refuse_terminal=False)
        except Exception:
            log.exception("steward: could not record the failure of task %s", task_id)

    async def _transition(
        self, task_id: UUID, status: str, changes: dict[str, Any], *, refuse_terminal: bool = True
    ) -> None:
        """Move a task to `status` with `changes`: the fact first, then the write."""
        async with self._session() as session:
            row = await get_steward_task(session, task_id, lock=True)
            if row is None:
                raise StewardError("the task vanished")
            if row.status in TERMINAL_STATUSES and not refuse_terminal:
                return  # already terminal: a late failure changes nothing
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

    def _render(self, template: Template, task: Any, bundle: ContextBundle) -> str:
        evaluation = (bundle.profile or {}).get("evaluation")
        values = {
            "name": bundle.name,
            "candybar_id": str(task.candybar_id),
            "task_id": str(task.id),
            "action": task.action,
            "runner_kind": bundle.runner_kind,
            "workdir": str(bundle.workdir),
            "repo": bundle.repo_slug or "(none recorded)",
            "template_version": str(template.version),
            "context": bundle.render(),
            "evaluation": json.dumps(evaluation, indent=2) if evaluation else "(no stored evaluation)",
            "recommendation": (evaluation or {}).get("recommendation") or "(no stored recommendation)",
        }
        return template.render(values)

    async def _converse(
        self, task: Any, bundle: ContextBundle, prompt: str, validate: Callable[[dict[str, Any]], Any]
    ) -> Any:
        """One OpenCode session: the prompt, then at most one repair turn. Returns the validated answer."""
        budget = float(self._timeouts[task.action])
        deadline = self._clock() + budget
        endpoint, session_id = await self._opencode.start(
            title=f"candybar steward: {task.action} {bundle.name}", model=task.model, directory=str(bundle.workdir)
        )
        await self._patch(task.id, {"session_id": session_id})
        try:
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
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(self._opencode.stop(endpoint, session_id))
            raise

    # ------------------------------------------------------------------
    # Actions.

    async def _enrich(self, task: Any, candybar: Any) -> dict[str, Any]:
        bundle = await self._bundle(candybar)
        template = self._template("enrich")
        overview: Overview = await self._converse(task, bundle, self._render(template, task, bundle), validate_overview)
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
                    checkout=bundle.local_repo,
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
        evaluation = Evaluation(
            **verdict.model_dump(exclude={"followUp"}),
            followUp=follow_up,
            reportKey=report_key(bundle.name, moment),
            evaluatedAt=moment,
            taskId=task.id,
        )
        payload = evaluation.model_dump(mode="json")
        async with self._session() as session:
            row = await get_steward_task(session, task.id)
            task_view = task_json(row) if row is not None else {"id": str(task.id), "model": task.model}
        markdown = render_markdown(
            name=bundle.name,
            evaluation=payload,
            definition=bundle.definition,
            runner=bundle.runner,
            task=task_view,
            runs=bundle.invocations,
            follow_up_note=FOLLOW_UP_PLAN[follow_up],
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
            note = await self._follow_up(task, candybar.id, follow_up)
        except Exception as exc:
            raise StewardError(
                f"evaluation stored ({verdict.outcome}, {evaluation.reportKey}) but the {follow_up} "
                f"follow-up failed: {exc}"
            ) from exc
        return {
            "outcome": verdict.outcome,
            "summary": f"{verdict.summary} Follow-up: {note}",
            "report_key": evaluation.reportKey,
        }

    async def _fix(self, task: Any, candybar: Any) -> dict[str, Any]:
        if getattr(candybar, "archived", False):
            raise StewardError("archived candybars are not fixed; unarchive it first")
        bundle = await self._bundle(candybar)
        template = self._template("fix")
        answer: FixAnswer = await self._converse(task, bundle, self._render(template, task, bundle), validate_fix)
        commits = f" Commits: {', '.join(answer.commits[:5])}." if answer.commits else ""
        if not answer.repaired:
            return {"outcome": "not_fixed", "summary": f"{answer.summary}{commits}"}
        invocation_id, row = await self._prove(task, candybar)
        if row is None:
            verdict = "did not finish in time (or was skipped as an overlap)"
            outcome = "not_fixed"
        else:
            outcome = "fixed" if row.result == "success" else "not_fixed"
            verdict = f"{row.result} (exit {row.exit_code})"
        return {
            "outcome": outcome,
            "summary": f"{answer.summary}{commits} Proving run {invocation_id}: {verdict}.",
            "invocation_id": invocation_id,
        }

    async def _refactor(self, task: Any, candybar: Any) -> dict[str, Any]:
        bundle = await self._bundle(candybar)
        template = self._template("refactor")
        answer: RefactorAnswer = await self._converse(
            task, bundle, self._render(template, task, bundle), validate_refactor
        )
        commits = f" Commits: {', '.join(answer.commits[:5])}." if answer.commits else ""
        applied = "" if answer.applied else " (not applied)"
        return {"outcome": "done", "summary": f"{answer.summary}{applied}{commits}"}

    # ------------------------------------------------------------------
    # Follow-ups and the fix proof.

    async def _follow_up(self, parent: Any, candybar_id: UUID, follow_up: str) -> str:
        if follow_up == "none":
            return "none."
        if follow_up == "archive":
            return await self._archive(candybar_id)
        return await self._queue(parent, candybar_id, follow_up)

    async def _queue(self, parent: Any, candybar_id: UUID, action: str) -> str:
        """Queue a follow-up task: command and `queued` fact first, then the row (the API's flow)."""
        async with self._session() as session:
            row = await lock_candybar(session, candybar_id)
            if row is None:
                return f"{action} skipped: the candybar is gone."
            if action == "fix" and getattr(row, "archived", False):
                return "fix skipped: the candybar is archived."
            active = await active_steward_task(session, candybar_id, action)
            if active is not None:
                return f"{action} already {active.status} as task {active.id}."
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
        return f"queued {action} task {draft.id}."

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
