# Deferred Work

Resolved by Story 1.9 (2026-10-26): the 1.7 Prometheus scrape/exemplar item
(monitoring@80f6d2f — candybar job added, `--enable-feature=exemplar-storage`
on the live container, `query_exemplars` returns invocation ids) and the 1.7
30s-drain item (executor/dispatcher unsubscribe before drain; `docker stop`
now completes in ~1s, pinned by tests/test_fast_stop_integration.py).

- source_spec: `_bmad-output/implementation-artifacts/spec-1-2-bloodbank-cron-event-vocabulary-envelopes-and-governance-pr.md`
  summary: No automated check keeps candybar's `schemas/cron/` drafts byte-identical to the canonical bloodbank `schemas/bloodbank/cron/` set.
  evidence: Real — the drafts are verbatim copies today and `schemas/README.md` instructs manual sync, but candybar CI has no bloodbank checkout so drift would be silent. Settles when a cross-repo sync/check script exists (candidate home: bloodbank repo or a 33god-platform conformance sweep); `TestDraftSchemas` in candybar already pins the drafts against the in-repo vocabulary, which bounds the blast radius to doc-drift, not contract-drift.

- source_spec: `_bmad-output/implementation-artifacts/spec-4-1-python-sdk-sdks-python.md`
  summary: The executor stores a child's stderr tail and failure message in Postgres text columns without stripping NUL, so a job that writes a NUL byte to stderr makes the terminal write fail and the invoke command is nak'd and re-executed on every redelivery.
  evidence: Pre-existing in Story 1.6's executor (runner.py decodes stderr with errors="replace", which keeps U+0000; worker.py naks on record failure; Postgres text rejects 0x00). Story 4.1 strips NUL and lone surrogates from SDK messages only — bare executables still reach the path. Settles with a NUL/surrogate scrub in the executor before `record_terminal_invocation` plus a live-PG test writing `\0` to stderr.

- source_spec: `_bmad-output/implementation-artifacts/spec-2-1-effectively-once-dispatch-proven-under-fault-injection.md`
  summary: A hard kill (SIGKILL/OOM/power loss) of the executor while a child runs leaves no invocations row, so JetStream redelivers the command after ack_wait (1h) and the job runs a second time.
  evidence: Real. The terminal row is written only after the child exits (1.6 row-first design), and graceful SIGTERM is covered because the child is recorded `failed` and acked. Closing the hard-kill case needs a claim row with result NULL committed before spawn, plus a policy for orphaned claims (mark failed on restart vs re-run). That changes 1.6's counter accounting, and the 2.1 AC fault set (tick redelivery, command redelivery, dispatcher restart) does not name it.

- source_spec: `_bmad-output/implementation-artifacts/spec-4-2-node-sdk-sdks-node.md`
  summary: The Python, Node, and Rust SDKs have no shared conformance fixture. Edge behavior has already diverged where the executor treats both forms alike: failure message `Type: msg` (Python) vs `msg` (Node), exit 2 (Python) vs 1 (Node) on an unwritable result file, and a fail doc vs no file on a protocol error when `CANDYBAR_RESULT_FILE` is set.
  evidence: Real but harmless today. `resolve_outcome` maps each pair to the same verdict, and no consumer parses messages. Drift is the risk the epic names. This settles with one language-neutral case table (env in → expected result JSON + exit code) under `sdks/conformance/`, run by all three SDK test suites, plus a single decision on the message format.

- source_spec: `_bmad-output/implementation-artifacts/spec-3-1-memory-peak-and-average-cpu-load-per-invocation.md`
  summary: The executor moves a child into its cgroup scope only after spawn. Memory charged before that move (interpreter or runtime startup, about 10 MB for Python) is never counted, and a sub-millisecond job exits before it joins, so it meters 0. Live `deploy-smoke` rows read 0 for this reason.
  evidence: Real and observed. The live `deploy-smoke` write to cgroup.procs failed with ESRCH. The 64 MiB canary reads 64.2 MiB even though the interpreter baseline is about 10 MB, because cgroup v2 never migrates existing charges. Story 3.1's frozen intent rules out a pre-exec join. Closing the gap needs the child to join before exec, either through a `preexec_fn` that writes `0` to an fd held open on the scope's cgroup.procs (fork in a multi-threaded process) or through clone3 CLONE_INTO_CGROUP via a small native helper. Either choice needs a human decision on the tradeoff.
