# Continuous ticket orchestration (sentinel engine)

Status: sentinel engine protocol (provider-agnostic)

## Invariant

If a ready ticket exists, exactly one implementation worker must be actively
moving it, or the sentinel records why none can. Prefer one live thread over
a quiet backlog. WIP limit: one active worker ticket.

The sentinel owns the watch loop. Workers (codex, opencode, copilot, …) own
implementation. The sentinel clears review-lane work via the autonomous
adversarial review (act, do not wait) protocol
(`autonomous-delegated-review.md`), and still does not write application code or
approve merges.

## Ticket access

All board access goes through the adapter (`tp`, from
`.scripts/lib/ticket-provider.sh`) and reasons in normalized states:
`backlog | unstarted | started | in_review | completed`. Never call the provider
directly — the engine is identical across Linear, Plane, and Trello.

## Work-state feed

Every heartbeat/pass keeps `runtime/continuous-ticket-sentinel-state.json`
current and machine-readable: `source`, `agent_id`, `repo`, `ticket_provider`,
`status` (`idle|checking|active|blocked|stalled|error`), `active_issue`,
`summary`, `reason`, `session`, `worktree`, `updated_at`, `last_activity_at`,
`log_path`.

## Source order (each pass)

1. Active milestone (`tp active_milestone`) and issues (`tp list_issues`).
2. Local evidence under `_bmad-output/implementation-artifacts/issue-evidence/`.
3. Live worker state: zellij sessions, worktrees, branches, recent git.

When sources disagree, record a truth-check note and keep the issue open.

## Ticket selection (when no worker active)

1. A blocked/review ticket needing only agent-doable evidence repair.
2. An unblocked issue in the current milestone.
3. A small, high-priority backlog issue when the milestone has no ready ticket.

Move the chosen issue to `started` (`tp transition <id> started`) and create/
refresh its evidence file before spawning exactly one worker.

## Stop conditions

Stop without spawning only when: the board/evidence cannot be inspected; every
candidate is blocked by external evidence/credentials/product decisions (a
ticket blocked **only** on human review is NOT a stop condition — run it through
the independent adversarial review and act on the verdict immediately, no
waiting; and a dependent blocked **only** on a review-accepted feature is NOT
blocked); a worker is already active and healthy; or the next action needs
destructive git ops / production credentials / a paid action.

The loop never ends a pass with work parked waiting on the operator.

## Review and closure

1. Run ticket verification.
2. Run the close gate: `.scripts/sentinel/bin/issue-close-gate.sh <ISSUE>`.
3. Run the independent adversarial review — an adversarial microscope, with the
   reviewer agent NOT the implementer (`autonomous-delegated-review.md`). On a
   clean adversarial verdict the loop autonomously treats the ticket as done
   (leaving it in the review lane as the operator's deferred-QA queue) and moves
   on with no grace wait. A real finding holds it back to active.
4. Gate fail → leave open, record missing evidence.
5. Downstream regression rollback: if a later dependent proves a
   review-accepted feature is actually broken, move it back to active as a
   prerequisite of the dependent and record the rollback.

Board status is not proof. Repository evidence and the close gate are proof.
