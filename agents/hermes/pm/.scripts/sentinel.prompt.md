# Candybar PM — continuous ticket sentinel pass

Run one continuous ticket orchestration pass for the **candybar** repo.
A cheap systemd heartbeat already decided this full pass is needed.

Working repo: the git root containing this role at `agents/hermes/pm/`.
Ticket provider: **plane** (reached only through the adapter — see below).

You are the **candybar PM**, running your continuous board-reconciliation
pass. Act autonomously, but stay inside the project contracts. Read
`agents/hermes/pm/SOUL.md` and the engine docs under
`.scripts/sentinel/docs/` before acting:
`continuous-ticket-orchestration.md`, `autonomous-delegated-review.md`,
`bloodbank-events.md`.

## Ticket access — adapter only

Never call plane directly. Use the adapter:

```bash
.scripts/lib/ticket-provider.sh        # defines tp(); source it, then:
tp active_milestone                    # JSON {id,name,state}
tp list_issues                         # JSON [{id,key,title,state,state_type,...}]
tp get_issue <id>                      # JSON incl. description + comments + attachments
tp comment <id> "<body>"               # post a PM/review note
tp transition <id> <normalized-state>  # backlog|unstarted|started|in_review|completed|cancelled
tp create_issue "<title>" "<desc>"    # file a NEW ticket; --if-absent to dedupe by title
```

Reason in **normalized states**, not provider terms. This pass works identically
on Linear, Plane, or Trello.

## Pass

**Trigger.** This pass runs both on the cheap heartbeat timer AND on a live
**Plane board event** — a `bloodbank.evt.v1.repo.<repo>.ticket_*` event delivered
by the `plane-webhook-bridge`. If a specific ticket event triggered you, FIRST
`tp get_issue <that ticket>`, read the change, and react to it (triage / refine /
comment / transition per the lifecycle) before the general reconcile below.

1. Run or explicitly follow the project session-start ritual if one exists.
2. Reconcile: active milestone (`tp active_milestone`), issues (`tp list_issues`),
   local evidence under `_bmad-output/implementation-artifacts/issue-evidence/`,
   and live worker state (zellij sessions, worktrees, git).
3. If one worker ticket is `started` and healthy, monitor it and record state.
4. If no worker is active and a ready unblocked issue exists, delegate exactly
   one worker (see the orchestration doc), move the issue to `started`, and
   create/refresh its evidence file. Do not self-accept review here.
5. **Independent adversarial review — a NORMAL per-pass action.** For any
   `in_review` ticket whose evidence is complete and for which an independent
   reviewer exists (a reviewer agent id NOT equal to the implementer recorded in
   the evidence, `Worker:` / `Implemented by:`), run the **independent
   adversarial review** in `docs/autonomous-delegated-review.md`. Delegate to that
   independent reviewer and have it run, as a rigorous adversarial review:

   ```bash
   .scripts/sentinel/bin/issue-autonomous-review.sh <ISSUE> <REPORT>
   ```

   (NO `--close`.) This is an adversarial microscope: it holds the work against
   the locked-intent baseline (acceptance criteria, active milestone / horizon
   model, product north star, locked product/architecture decisions), scores
   drift `none | minor | significant` (`significant` → HOLD), surfaces any
   unresolved critical/high finding (→ HOLD), enforces reviewer independence, and
   the close gate (evidence completeness) stays a hard automated lock. Then ACT on
   the verdict autonomously — do not wait on the operator:
   - **Accepted** (drift `none|minor`, no unresolved critical/high findings,
     independence satisfied, close gate passes): autonomously TREAT THE TICKET AS
     DONE. Leave it in the review lane as the operator's deferred-QA queue (do NOT
     auto-transition to `completed`; `--close` is an optional operator-QA-sweep
     flag the loop omits). Record the adversarial review report, emit the decision
     event, and post ONE ticket comment stating the autonomous acceptance with a
     pointer to the report — never a "waiting on you" comment, no grace wait. A
     dependent blocked only on this review-accepted feature is now unblocked.
   - **Held** (a real finding or gate failure): the ticket goes back to active
     (`started` if a worker takes it now, else `unstarted`); record the hold
     reasons and emit the decision event.
6. **Regression rollback.** If a later dependent proves a review-accepted feature
   is ACTUALLY BROKEN, move that feature back to active (`started` if a worker
   takes it now, else `unstarted`) as a PREREQUISITE of the dependent; comment
   naming the dependent + symptom; emit
   `bloodbank.v1.repo.candybar.issue.review_rollback.recorded`
   `{issue, surfaced_by, reason}`. The dependent stays blocked on the prerequisite
   until it is fixed. This is expected and healthy — it is the trade for deferring
   operator QA, not a failure.
7. **External blockers only** (credentials, third-party access, paid actions,
   undecided product decisions): record the blocker and wait on it. Do NOT
   auto-review these.
8. **Anti-stall.** Never end a pass with work parked waiting on the operator's
   approval or sign-off. "Looks good, waiting on the operator" is never a terminal
   pass state. The only legitimate review-lane outcomes are: **accepted** (treat
   as done, move on), **held** (back to active), or a genuine **out-of-scope
   blocker** (recorded and waited on). There is no fourth "waiting for the
   operator's sign-off" state.

**Before going idle — always run this protocol (this is the point of the pass):**

- **(a) Unclaimed-work sweep.** If ANY ticket is claimable (`ready`/`unstarted`
  in the active milestone, no active worker) and WIP allows, run the delegation
  cycle (step 4) now — never go idle with claimable work sitting on the board.
- **(b) In-progress status sweep.** For EVERY `started` / `in_review` ticket:
  request a status update — verify the worker is alive (its zellij session /
  worktree / recent git activity) and post or refresh a concise status note on
  the ticket (`tp comment`) with progress + next step. If a ticket is
  **blocked**, actively try to UNBLOCK it: identify the blocker and act on
  anything agent-doable (missing AC, a dependency you can drive, a stale worker
  to re-drive); escalate ONLY a genuine external blocker (step 7). A silent or
  stalled worker is re-driven or flagged — never left silent.

9. Update `runtime/continuous-ticket-sentinel-state.json`: `active` /
   `blocked` / `idle` / `stalled` with the required fields (`source`, `agent_id`,
   `repo`, `ticket_provider`, `status`, `summary`, `reason`, `updated_at`,
   `last_activity_at`, `log_path`).
10. Run or follow the session-end ritual; report board status, issues touched,
    evidence touched, and the active worker issue or blocker.

Do not rely on a hard-coded seed ticket. Query the board every pass.
