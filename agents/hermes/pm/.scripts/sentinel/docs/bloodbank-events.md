# Sentinel workflow events

Status: Sentinel engine protocol (provider-agnostic)

## Purpose

The board remains the human command center and issue evidence files remain the
close gate. Neither depends on an event trail: `bin/issue-close-gate.sh` and
`bin/issue-autonomous-review.sh` report their verdicts on stdout/stderr and via
their exit codes, and publish nothing.

The `repo.issue.*` family the sentinel loop used to mint was retired on
2026-08-28. It was never published to NATS and never consumed by anything, and
its shape was invalid twice over: it embedded the repo slug inside the type
(`bloodbank.v1.repo.<repo>.issue.…`) and `issue` is not in the Bloodbank §7
entity allowlist. There is no correct version of it to migrate to, so it is
gone rather than renamed.

## Emitter

`bin/emit-event.py` stays as a dependency-free local emitter for a family a
future pass genuinely needs:

```bash
.scripts/sentinel/bin/emit-event.py <event_type> --field key=value [...]
```

It appends to `_bmad-output/implementation-artifacts/bloodbank-events.jsonl`
(git-ignored dev spool) using the Hermes CloudEvents envelope shape. Nothing in
the engine calls it today.

## Naming, before you add one

Event types are exactly four tokens — `bloodbank.<domain>.<entity>.<action>` —
with `<domain>` and `<entity>` drawn from the allowlists in
`~/code/33GOD/bloodbank/docs/event-naming.md` (§6/§7). Repo and agent identity
go in `data.repo` / `actor.agent_id`, never in a type or subject token. Schema
revision lives in `dataschema`/`schemaref`, never in the type.

Add a family when something will actually consume it — register the schema in
the BloodBank schema tree first, then emit. A type nobody reads is a type that
rots into the wrong shape.
