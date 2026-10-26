# Deferred Work

Resolved by Story 1.9 (2026-10-26): the 1.7 Prometheus scrape/exemplar item
(monitoring@80f6d2f — candybar job added, `--enable-feature=exemplar-storage`
on the live container, `query_exemplars` returns invocation ids) and the 1.7
30s-drain item (executor/dispatcher unsubscribe before drain; `docker stop`
now completes in ~1s, pinned by tests/test_fast_stop_integration.py).

- source_spec: `_bmad-output/implementation-artifacts/spec-1-2-bloodbank-cron-event-vocabulary-envelopes-and-governance-pr.md`
  summary: No automated check keeps candybar's `schemas/cron/` drafts byte-identical to the canonical bloodbank `schemas/bloodbank/cron/` set.
  evidence: Real — the drafts are verbatim copies today and `schemas/README.md` instructs manual sync, but candybar CI has no bloodbank checkout so drift would be silent. Settles when a cross-repo sync/check script exists (candidate home: bloodbank repo or a 33god-platform conformance sweep); `TestDraftSchemas` in candybar already pins the drafts against the in-repo vocabulary, which bounds the blast radius to doc-drift, not contract-drift.
