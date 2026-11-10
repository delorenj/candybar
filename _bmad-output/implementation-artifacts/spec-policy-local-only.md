---
title: 'Keep scheduling policy drift in local agent attention'
type: 'bugfix'
created: '2026-11-10'
status: 'in-review'
route: 'oneshot'
review_loop_iteration: 0
---
<frozen-after-approval reason="delegated urgent containment, source-only">
## Intent
Stop recurring raw policy transitions reaching Telegram durably. The canonical manifest must use the existing local-only transition wrapper; register and autosync must retain that route. Preserve audits, pending outbox bytes and true violations. Active faults go to existing local agent attention with stable keys; recovery stays quiet. Diagnose flapping unknown read-only. Audit other operational senders read-only, preserving human Jim and critical escalations. No live activation, restart, resume or main landing; independent parent review and real invocation precede resume.
</frozen-after-approval>
## Implementation Notes
- Reuse existing transition_exec local-only mode, register loader and autosync reconciliation; no scheduler or transport added.
- Offline tests execute real detector/wrapper subprocesses against temporary inventories and failing sender tripwires.
- Shared ~/.agents gate is a second source-only worktree; unrelated dirty main work is untouched.
- Read-only lock probe reproduced BlockingIOError acquiring canonical migration directory shared lock. Retain unknown rather than weaken rollback evidence.
- Flap owner observed via /proc/locks inode match: duplicate `candybar watch /home/delorenj/code/33GOD --if-present` processes PID 3538128 then 3288117, parent 4334, installed uv-tool runtime. Exact newapi ledger is rolled-back, disable_pending=false; exact registration disabled with matching systemd runner. Only diagnostic reason is narrowed; lock semantics and checks unchanged.
- Real API later readback found paused scheduling-policy-drift but the original routed runner restored by autosync. No live state changed here; canonical landing and PATCH/readback must precede proof/resume.
- 116 focused Candybar tests passed, full ruff passed. Agents gate suite: 47 passed, 5 subtests. Raw outputs are colocated. Parent independent review remains pending; no reviewer tool is exposed in this subagent runtime, so the full bounded review prompt is saved separately.
