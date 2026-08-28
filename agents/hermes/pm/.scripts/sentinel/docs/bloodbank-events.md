# Sentinel workflow events

> **Retired 2026-08-28.** The two `repo.issue.*` review families this engine used
> to mint — the adversarial-review decision and the regression rollback — are
> gone. A full-history query of the Candystore projection returned zero rows for
> both, nothing subscribed to them, and their shape was invalid twice over: the
> repo slug sat inside a type token, and `issue` is not in the Bloodbank §7
> entity allowlist, so there was no correct name to migrate them to. The verdict
> now lives in the review report, the issue evidence file, and the ticket
> comment. Mint a family again only when something will actually read it.

Status: Sentinel engine protocol (provider-agnostic)

## Purpose

Local workflow events are the machine-readable timeline of the sentinel loop.
The board remains the human command center; issue evidence files remain the
close gate; this JSONL spool lets Hermes, dashboards, and future agents observe
what happened.

## Emitter

```bash
.scripts/sentinel/bin/emit-event.py <event_type> --field key=value [...]
```

Appends to `_bmad-output/implementation-artifacts/bloodbank-events.jsonl`
(git-ignored dev spool) using the Hermes CloudEvents envelope shape. Event types
use the project repo lane `bloodbank.v1.repo.<repo>.<entity>.<action>`, where
`<repo>` comes from `role.yaml`.

## Event types

| Event type | When | Required data |
| --- | --- | --- |
| `…repo.<repo>.issue.evidence.created` | Evidence file created | `issue`, `evidence_file` |
| `…repo.<repo>.issue.gate.passed` | Close gate passes | `issue`, `evidence_file` |
| `…repo.<repo>.issue.gate.failed` | Close gate fails | `issue`, `evidence_file` |
| `…repo.<repo>.issue.truthcheck.flagged` | Status/evidence mismatch found | `issue`, `reason` |

## Rules

- Emit events for consequential transitions; do not invent types casually.
- Event emission never replaces the board update or issue evidence.
- If emission fails, continue and report the trail is incomplete.
- Autonomous acceptance is legitimate only when `bin/issue-autonomous-review.sh` exits 0 with
  `close_gate=pass` on its own output. That script will not report an
  accepted decision while the close gate fails or drift is `significant`.
  It publishes nothing — see the retirement note above.

## Canonical BloodBank

These project-local repo-lane events are BloodBank-*style*. Promote a type to a
canonical NATS subject only after adding its JSON Schema to the BloodBank schema
tree and passing validation. The local emitter does not require NATS so the loop
stays reliable offline.
