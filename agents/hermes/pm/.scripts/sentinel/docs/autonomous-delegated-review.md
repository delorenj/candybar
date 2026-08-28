# Autonomous adversarial review (act, do not wait)

Status: sentinel engine protocol (provider-agnostic)

## Purpose

When a ticket reaches the review lane, the sentinel runs an
**independent adversarial review** of the work against the operator's locked
intent and acts on the verdict autonomously. This is the **normal per-pass
path** for review-lane tickets — not a deliberately narrow escape hatch, and not
the only sanctioned path. The reviewer is an **adversarial microscope**: it
actively tries to break the work, surface unmet acceptance criteria, hidden
regressions, and drift. On a clean adversarial verdict the loop treats the
ticket as done and moves on — through the ticket-provider adapter
(`tp transition <id> <state>`) — and emits a BloodBank decision event carrying
the full report. This works identically on Linear, Plane, or Trello.

The operator's verification is **deferred** to end-of-product QA over the review
lane, backed by a queryable decision trail. A downstream regression rollback is
the safety valve. The loop does NOT wait on a human to approve or close
reviewed work.

## Anti-stall rule

The continuous ticket sentinel MUST act on the adversarial verdict autonomously.
It MUST NOT idle, stop, or end a pass solely because reviewed work awaits human
approval. "Looks good, waiting on the operator" is **never** a resting state and
never a terminal pass state. For any review-lane ticket there are exactly three
legitimate outcomes:

- the independent adversarial review has run and the ticket is **accepted**
  (treat as done, move on); or
- the review **held** it on a real finding (back to / stays active); or
- the ticket has a genuine **out-of-scope blocker** (recorded and waited on).

There is no fourth "waiting for the operator's sign-off" state.

## Out-of-scope blockers (not cleared by review)

The review does not clear an out-of-scope blocker. Record the blocker and wait,
exactly as before:

- Blocked on external credentials, third-party access, or paid actions.
- Blocked on an undecided product decision.
- Acceptance criteria not actually satisfied by repository evidence.
- Depends on another open, unblocked issue.

## Preconditions

1. **Review-lane ticket** — issue is `in_review`, complete, with no out-of-scope
   blocker outstanding.
2. **Evidence exists** — complete evidence file under
   `_bmad-output/implementation-artifacts/issue-evidence/<ISSUE>.md`.
3. **Independent reviewer available** — the reviewer agent id MUST differ from
   the implementer recorded in the evidence (`Worker:` / `Implemented by:`) and
   from the delegating PM. An implementer NEVER clears their own work.

There is no mandatory grace window. `reconcile.grace_hours` (role.yaml;
override env `RECONCILE_GRACE_HOURS`) defaults to `0` and is an
optional operator knob only — set `>0` to reintroduce a wait. The reviewer does
not wait for the operator's first right of refusal; it reviews and acts.

## Locked intent baseline

Drift is measured against fixed intent, assembled from: the issue's acceptance
criteria; the active milestone (`tp active_milestone`) and the project's horizon
model; the product north star; and any locked planning/decision artifacts. The
reviewer does not re-litigate intent.

## Drift rubric

- **significant (HOLD)** — an AC unmet; user-facing capability added/removed
  beyond the ACs or milestone; contradicts a locked decision/north star; pulls
  Later work into Now or touches another milestone; contradicts locked
  architecture; introduces a new external dependency/credential/paid action.
- **minor (accept allowed)** — internal refactors, extra tests, naming, cosmetic
  deviations within locked intent, docs.
- **none** — matches locked intent and ACs.

Only `none`/`minor` with no unresolved critical/high findings may be accepted.

## Decision

Run from the role's bin (couples gate + drift + event + acceptance):

```bash
.scripts/sentinel/bin/issue-autonomous-review.sh <ISSUE> <REPORT>
```

- **accepted** — independent adversarial review cleared it (drift `none|minor`,
  no unresolved critical/high findings, independence satisfied, close gate
  passes); the loop treats the ticket as done and moves on; event emitted with
  `decision=accepted`. The ticket comment states the autonomous acceptance and
  points to the review report — it does NOT request approval.
- **held** — any adversarial gate fails; the ticket goes back to / stays active;
  event `decision=held` with the reason. When in doubt, hold.

The close gate (evidence completeness) remains a HARD, AUTOMATED lock: the
script will not emit an `accepted` decision while the close gate fails, drift is
`significant`, an unresolved critical/high finding stands, or independence is
not satisfied.

## Treat review as done

An adversarially-**accepted** `in_review` ticket equals `completed` for
dependents and flow. By default the ticket STAYS in the review lane — it is the
operator's deferred-QA queue — and is NOT auto-transitioned to `completed`.
`--close` is an OPTIONAL flag (operator QA sweep) and is omitted by the normal
loop:

```bash
.scripts/sentinel/bin/issue-autonomous-review.sh <ISSUE> <REPORT> --close
```

## Decision event

```text
bloodbank.v1.repo.<repo>.issue.autonomous_review.decided
```

Required data: `issue`, `decision` (`accepted | held`), `drift`, `close_gate`,
`reviewer_agent`, `evidence_file`, `report_file`. See `bloodbank-events.md`.

## Downstream regression rollback

Deferring operator QA means a later dependent can prove a review-accepted
feature is ACTUALLY BROKEN. When that happens, move the accepted ticket back to
active (`started` if a worker takes it now, else `unstarted`) as a PREREQUISITE
of the dependent, via the adapter (`tp transition <id> <state>`); comment naming
the dependent + symptom; and emit:

```text
bloodbank.v1.repo.<repo>.issue.review_rollback.recorded
```

Required data: `issue`, `surfaced_by`, `reason`. The dependent is blocked on the
prerequisite until it is fixed. This is expected and healthy — it is the trade
for deferring operator QA, not a failure.

## Review report shape

Write `<ISSUE>.review.md`; the script validates it:

```markdown
# Autonomous Review Report: <ISSUE>
## Issue
- Linear/Plane/Trello issue: <ISSUE>
- Review lane reason:
## Reviewer
- Reviewer agent: <independent-agent-id>
- Independent of implementer: yes
## Locked Intent Baseline
- Acceptance criteria source:
- Milestone / horizon:
## Drift Assessment
- Drift assessment: none        # none | minor | significant
## Adversarial Findings
- Critical/high findings: none
## Decision
- Decision: accept              # accept | hold (legacy `close` tolerated)
```

## Operator override

`reconcile.auto_review: false` (role.yaml) or `RECONCILE_AUTO_REVIEW=off`
disables autonomous acceptance. Autonomous acceptances are fully traceable via
the decision events in
`_bmad-output/implementation-artifacts/bloodbank-events.jsonl`.
