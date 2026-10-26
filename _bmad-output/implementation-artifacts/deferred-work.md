# Deferred Work

- source_spec: `_bmad-output/implementation-artifacts/spec-1-2-bloodbank-cron-event-vocabulary-envelopes-and-governance-pr.md`
  summary: No automated check keeps candybar's `schemas/cron/` drafts byte-identical to the canonical bloodbank `schemas/bloodbank/cron/` set.
  evidence: Real — the drafts are verbatim copies today and `schemas/README.md` instructs manual sync, but candybar CI has no bloodbank checkout so drift would be silent. Settles when a cross-repo sync/check script exists (candidate home: bloodbank repo or a 33god-platform conformance sweep); `TestDraftSchemas` in candybar already pins the drafts against the in-repo vocabulary, which bounds the blast radius to doc-drift, not contract-drift.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-7-basic-prometheus-metrics-and-metrics-endpoint.md`
  summary: Story 1.9 must add a candybar scrape job and `--enable-feature=exemplar-storage` to the live Prometheus (3.5.0), or the `invocation_id` exemplars are scraped but never stored.
  evidence: Real. `docker inspect prometheus` shows no exemplar-storage flag and no candybar target. The 1.7 live smoke used a throwaway prom/prometheus:v3.5.0 with the flag, and `query_exemplars` returned the invocation ids. The frozen 1.7 intent excludes deploy changes.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-7-basic-prometheus-metrics-and-metrics-endpoint.md`
  summary: On SIGTERM the executor role's `nc.drain()` hits the 30s nats-py DrainTimeoutError (pull subscription), so a graceful daemon stop takes about 30s and logs an ERROR.
  evidence: Real and pre-existing (Story 1.6 role.py). Seen twice in the 1.7 smoke: the api role drained at once, and the executor logged "draining connection timed out" 30s later. Under `docker stop`'s default 10s grace (Story 1.9), the container will be SIGKILLed. In-flight children are already terminated before the drain, so no invocation is lost. Fix candidates: `await sub.unsubscribe()` before `drain()`, a shorter `drain_timeout`, or `stop_grace_period: 40s` in 1.9.
