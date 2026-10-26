# Deferred Work

- source_spec: `_bmad-output/implementation-artifacts/spec-1-2-bloodbank-cron-event-vocabulary-envelopes-and-governance-pr.md`
  summary: No automated check keeps candybar's `schemas/cron/` drafts byte-identical to the canonical bloodbank `schemas/bloodbank/cron/` set.
  evidence: Real — the drafts are verbatim copies today and `schemas/README.md` instructs manual sync, but candybar CI has no bloodbank checkout so drift would be silent. Settles when a cross-repo sync/check script exists (candidate home: bloodbank repo or a 33god-platform conformance sweep); `TestDraftSchemas` in candybar already pins the drafts against the in-repo vocabulary, which bounds the blast radius to doc-drift, not contract-drift.
