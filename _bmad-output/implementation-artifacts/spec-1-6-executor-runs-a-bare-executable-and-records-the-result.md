---
title: 'Story 1.6: Executor runs a bare executable and records the result'
type: 'feature'
created: '2026-10-26'
status: 'done'
baseline_commit: '77df05f90b28b53de98fc2161d3c587c33ee9174'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Invoke commands sit on the command durable with no consumer: nothing spawns the registered entrypoint, records the outcome, or emits lifecycle facts.

**Approach:** Add an executor role that consumes `bloodbank.cmd.cron.candybar.invoke` from its single durable pull consumer, spawns the entrypoint as a cgroup v2 scoped subprocess speaking the AD-8 env/result-file protocol, commits the terminal `invocations` row and `candybars` counters in one transaction, and emits `invoked`/`completed`/`failed` facts — idempotently keyed so redelivery never re-executes.

## Boundaries & Constraints

**Always:** One durable pull consumer filtered to `bloodbank.cmd.cron.candybar.invoke` on the `BLOODBANK_COMMANDS` stream (durable `candybar-executor-invoke`, explicit ack, deliver NEW); reuse `connect_nats`/`NatsEventSink` from `bus.py`. Child env carries `CANDYBAR_INVOCATION_ID`, `CANDYBAR_ID`, `CANDYBAR_TRIGGER`, `CANDYBAR_TICK_ID` (empty string when trigger=manual), `CANDYBAR_RESULT_FILE` (AD-8). Emit `invoked` at spawn before the child runs; emit `completed` or `failed` after exit. The terminal `invocations` insert and the `total_invoked`/`total_successful` update commit in ONE transaction via `registry/store.py` only. A redelivered command whose `idempotency_key` is already recorded performs no second execution; it re-publishes the deterministic terminal event from the row and acks. Stderr in failure data is the last 500 chars, `redacted: true` (existing `build_candybar_failed`). Lifecycle facts use `correlationid` = the command's `correlationid`, `causationid` = the command's `command_id`; terminal event ids are deterministic (UUIDv5 of `<result>:<invocation_id>`) so bus dedupe makes republication safe. Bare executables record unmeasured metrics as zero (`token_spend=0`, `cost_usd=None`).

**Never:** No clock/sleep primitives (AD-1 guard applies: only heartbeat/ may own time), no per-candybar bus subscribers, no SQL outside `registry/`, no Prometheus series (Story 1.7), no memory/CPU/GPU metering columns (Epic 3), no new migration (the 0001 schema already carries every field), no CLI changes (Story 1.8). Do not stage concurrent provisioning files under `_bmad/`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Bare success | Entrypoint exits 0, no result file | `completed`, exit_code 0, metrics zero, counters +1/+1 | Ack after commit+publish |
| Bare failure | Entrypoint exits 3 | `failed`, exit_code 3, stderr tail <=500 redacted, counters +1/+0 | Ack after commit+publish |
| SDK success | Exit 0 + result file `{status:"success", metrics:{token_spend:5}}` | `completed`, metrics from file | Ack |
| SDK fail | Exit 0 + result file `{status:"fail", message:"..."}` | `failed` with message | Ack |
| Invalid result | Exit 0 + unparseable/wrong-shape result file | `failed`, message names invalid result file | Ack |
| Spawn failure | Entrypoint missing/not executable | `failed`, exit_code null, spawn error as message | Ack |
| Redelivery | Command whose idempotency_key is recorded | No second spawn; terminal event republished from row; ack | N/A |
| Unknown candybar | Command references id absent from registry | `failed` event (exit null, message) then term; no row (FK) | Term, not retry loop |
| Poison | Malformed/non-invoke payload | Discard without execution | Term with log |
| Bus/DB outage | Publish or commit fails mid-flow | Nak; redelivery converges via recorded key | No silent loss |
| Shutdown mid-run | stop set while child running | Child SIGTERMed, recorded `failed` ("executor stopped") | Then drain |

</frozen-after-approval>

## Code Map

- `src/candybar/executor/runner.py` — subprocess spawn + cgroup v2 scope + env/result-file protocol; returns a `RunOutcome`. NEW.
- `src/candybar/executor/worker.py` — `Executor.handle_message`: parse/dedupe/spawn/record/emit/ack lifecycle. NEW.
- `src/candybar/executor/role.py` — `run_executor(stop)`: NATS + engine wiring, workqueue durable pull loop, in-flight task set with stop handling. NEW.
- `src/candybar/executor/__init__.py` — package exports (currently a docstring stub).
- `src/candybar/registry/store.py` — add `get_entrypoint`, `invocation_by_key`, `record_terminal_invocation` (row + counter update, caller's transaction; unique-key race returns False).
- `src/candybar/registry/__init__.py` — export the new store functions.
- `src/candybar/events/builders.py` — reuse `build_candybar_{invoked,completed,failed}` verbatim; they already take correlation/causation and truncate stderr.
- `src/candybar/daemon.py` — register `executor` role in `ROLES`.
- `tests/test_executor.py` — runner + worker unit matrix; `tests/test_executor_role.py` — role wiring (dispatcher-role pattern).
- `tests/test_executor_integration.py` — env-gated live NATS + live Postgres end-to-end: real script fixture, real durable, real rows/counters.
- `tests/test_heartbeat.py` — extend `available_roles` assertion with `executor` (it pins the sorted role list).
- `.github/workflows/ci.yml` — run executor integration in the live-NATS and live-Postgres steps.

## Tasks & Acceptance

**Execution:**
- [x] `registry/store.py` + `registry/__init__.py` — entrypoint read, key lookup, one-transaction terminal record.
- [x] `executor/runner.py` — cgroup-scoped spawn, AD-8 env, result-file parse, stderr tail, wall-ms.
- [x] `executor/worker.py` — command handling: idempotency, event emission, ack/nak/term policy.
- [x] `executor/role.py` + `executor/__init__.py` + `daemon.py` — durable consumer wiring and role registration.
- [x] `tests/test_executor.py`, `tests/test_executor_role.py`, `tests/test_heartbeat.py` — full matrix incl. redelivery, poison, unknown candybar, cgroup degradation.
- [x] `tests/test_executor_integration.py` + `ci.yml` — live bus + live registry proof.

**Acceptance Criteria:**
- Given an invoke command on the workqueue durable, when the executor consumes it, then it spawns the entrypoint under its own cgroup v2 scope with the five AD-8 env vars.
- Given a finished child, when the exit resolves, then `invoked` was emitted at spawn and `completed` (exit 0 or result `{status:"success"}`) or `failed` (nonzero exit / missing-or-invalid result contract, stderr tail max 500 redacted) is emitted.
- Given a terminal outcome, when it is recorded, then the `invocations` row insert and counter update commit in the same transaction.
- Given a redelivered command with an already-recorded idempotency_key, when consumed, then no second execution occurs.
- Given a bare executable, when it runs, then unmeasured metrics record zero.

## Implementation Notes

Implemented inline (no subagent-spawn tool in this session, same as 1.1–1.5). All matrix rows have passing tests; 210 unit tests + role wiring green, ruff clean.

Surprises and resolutions:

- **Live BLOODBANK_COMMANDS is WorkQueue retention**, which rejects `DeliverPolicy.NEW` on consumers (the spec's frozen "deliver NEW" could not hold on the real bus). The executor consumer uses `DeliverPolicy.ALL` — safe because acked commands leave a workqueue stream, so a rejoining durable only ever sees unacked work. Pinned in `test_executor_role.py`. Discovered by the first live smoke: the daemon crashed on consumer creation; the unacked command was then delivered to the SECOND daemon run, which also proved crash-rejoin redelivery end-to-end.
- **SIGTERM to the direct child leaves grandchildren holding the pipes** (`sh` waiting on `sleep` kept stderr open and hung `communicate()`). Children now spawn with `start_new_session=True` and shutdown kills the whole process group (`os.killpg`), falling back to `proc.terminate()`.
- **Plain-fs cgroup test double leaves a real `cgroup.procs` file** that blocks scope rmdir; `_leave_scope` retries after unlinking it (on real cgroupfs the file is virtual and the first rmdir succeeds).
- The executor integration test self-ensures the schema (alembic upgrade head) because the registry suite's fixture downgrades the shared throwaway DB to base after itself.

Production smoke (live bus + live registry): registered a temporary `smoke-exec` candybar, ran `candybar daemon --roles executor`, published a real `bloodbank.cmd.cron.candybar.invoke` — the script ran with the AD-8 env, `invoked`+`completed` emitted, terminal row committed with counters `2/2` (two commands: the pre-crash one plus the re-published one — dedupe-by-key visible as two distinct keys), cgroup degradation logged as a warning. Daemon stopped and smoke rows deleted afterwards.

## Spec Change Log

- 2026-10-26 (3cf089f, deferred hard-kill fix): the row-first design gains a pre-spawn **claim**. `claim_invocation` commits the `invocations` row with `result` NULL, keyed by the idempotency key, before the child spawns, and the terminal write finalizes it with an UPDATE guarded by `result IS NULL`. Counter accounting changes: `total_invoked` counts at claim, once per key, and `total_successful` counts at finalize. A redelivered command whose key is claimed but unfinished (executor SIGKILL/OOM, or a failed terminal write) is recorded `fail` ("abandoned: ...") and is not re-run, unless the candybar carries the `retry-on-abandon` tag (bounded to 3 deliveries). Child text (stderr tail, messages) is scrubbed of NUL/lone surrogates before the write. Running invocations now read back with `result: null`.

## Review Triage Log

Review run inline because no subagent-spawn tool was available to this session; blind, edge-case, and verification-gap checks applied to the owned diff (/tmp/story-1-6-diff.txt, 92kB). Finding floor N=min(floor(sqrt(92)+1),10)=10. Findings and routes:

- Uncaught exception from the dedupe read (`by_key`) crashed the task, leaving the command un-acked and stalled until the 3600s ack_wait — medium, patched: guarded with nak(delay=1) + test.
- Uncaught exception from `run_invocation` (e.g. mkdtemp OSError on an unwritable work root) — same stall — medium, patched with the same guard + test.
- Uncaught exception from `record` (e.g. KeyError when the candybar is deleted between lookup and terminal write) — same stall — medium, patched + test.
- Uncaught entrypoint-lookup failure crashed the task — medium, patched + test. (These four share one root cause: handle_message had no operational-error floor around its awaited stages.)
- Runner end-to-end env test observed only 3 of 5 AD-8 vars from the spawned child; CANDYBAR_ID/CANDYBAR_RESULT_FILE were covered only by a pure-function test — medium verification gap, patched: the capture now asserts all five plus per-invocation work-dir cleanup.
- `_ensure_schema` in the executor integration raised KeyError when CANDYBAR_TEST_PG=1 without DATABASE_URL — low, patched with the registry suite's localhost default + normalize_dsn.
- killpg pid-reuse race in terminate_all — false/low, rejected: returncode is checked first and the failure mode is a caught OSError; the fix adds complexity without a reachable harm in everyday use.
- stderr tail slicing could split a multibyte character — false: stderr is decoded with errors="replace" BEFORE the 500-char slice, so slicing operates on str, never bytes.
- Redelivery republish uses the redelivered command's name, which could drift if a candybar is renamed — low, rejected: rename surface lands with Epic 5; the row's candybar_id (the join key) is authoritative and carried correctly.
- cgroup scope write racing a fast-exiting child (ESRCH) degrades to unscoped with a warning — false as a defect: that is the designed degrade path, pinned by test_cgroup_degrades_when_root_unwritable.
- One deviation discovered during implementation rather than review: the frozen block's "deliver NEW" could not hold — the live BLOODBANK_COMMANDS stream has WorkQueue retention which rejects DeliverPolicy.NEW; the consumer uses DeliverPolicy.ALL (safe: acked commands leave a workqueue stream), pinned in test_executor_role.py. Recorded in Implementation Notes.

Zero deferred.

## Design Notes

Result precedence (epics AC ambiguity resolved): nonzero exit always `failed`; exit 0 + valid result file honors its `status`; exit 0 + no result file is the AD-8 bare-executable fallback = `completed`; exit 0 + invalid result file = `failed` (the process attempted the protocol and broke it). "Missing result file → failed" in the AC reads as the invalid-contract case, not the bare case — otherwise every SDK-less script would fail, defeating the story.

Terminal write ordering: commit the row FIRST (idempotency anchor), then publish the terminal event, then ack. The spine's generic "event first" rule (registration flow, 1.4) cannot hold here: a crash between event and row would make redelivery re-execute, violating the effectively-once AC. Row-first + deterministic event ids + republish-from-row on redelivery satisfies both the AC and the bus's durable-audit posture.

cgroup v2 mechanism (spine names the scope, not the syscall): direct cgroupfs — mkdir `<CANDYBAR_CGROUP_ROOT>/<invocation_id>` (default `/sys/fs/cgroup/candybar`), write the child pid to `cgroup.procs`, rmdir after exit. If the fs is not writable (dev box, CI, container without delegation) or the env var is empty, log a warning and run unscoped — execution never fails on missing cgroup privilege. Epic 3 reads `memory.peak`/`cpu.stat` from the same scope path.

Executions run as concurrent asyncio tasks (a 10-minute job must not head-of-line block others); on stop, in-flight children get SIGTERM and record `failed`.

## Verification

**Commands:**
- `uv run pytest -q` — unit suite green.
- `uv run ruff check .` — lint green.
- `CANDYBAR_TEST_NATS=1 uv run pytest tests/test_executor_integration.py -q` — live JetStream consume/dedupe green.
- `CANDYBAR_TEST_NATS=1 CANDYBAR_TEST_PG=1 uv run pytest tests/test_executor_integration.py -q` — full path: command in, real script executed, row + counters + terminal event verified.

## Independent Review (2026-10-26)

Fresh-context reviewer (not the implementer), bmad-code-review over the story's commits and the CURRENT tree (later epics reshaped shared modules). No subagent capability in this session, so the blind / edge-case / verification-gap / acceptance layers ran inline. Evidence classes: implemented + tested = unit suite 749 passed (`uv run pytest -q`), ruff clean; deployed = container rebuilt and redeployed per deploy/README.md after the patches, `/healthz` 200, next tick `tick:29883724` fired `deploy-smoke` (success, 19ms).

### Review Findings

- [x] [Review][Patch] A command picked up as stop began spawned a child nothing would terminate [src/candybar/executor/worker.py:_handle_claimed, runner.py:ProcessRegistry] — medium. The role's `finally` runs `terminate_all` (no await inside) BEFORE tasks created by the last `fetch` — or tasks still in their dedupe read/invoked publish — ever spawn, so their children were never signalled, ran to completion during shutdown past docker's 10s grace, and a SIGKILL left no row (re-execution after the 1h ack_wait). Reachable on every redeploy that lands on a fire minute. Fixed in 50be4fe: a command handled after stop began is nak'd back unexecuted (no invoked fact, no row), and a child whose spawn raced `terminate_all` is SIGTERMed on arrival (`ProcessRegistry.add`) and recorded `failed` ("executor stopped") per the matrix. Both new tests fail without the fix (the runner one times out with the 30s child still running); `test_shutdown_marks_running_child_failed` now stops mid-run, which is what it names.
- Rejected: `msg.in_progress()` raising inside the wait loop would orphan the child — low: it only raises on a closed/draining connection, which the role reaches only after every task is gathered. Unbounded executor concurrency (consumer default max_ack_pending 1000) — false as a defect: no spine/spec cap exists. The lost-race winner read was unguarded — real, fixed concurrently by the Epic 2 reviewer (f6a7a4a).
- Out of scope by instruction: stderr NUL scrubbing, pre-spawn claim rows, pre-exec cgroup join (owned by a dedicated agent, deferred-work.md).
