---
title: 'Story 3.2: Average GPU load per invocation (nvidia-smi or zero)'
type: 'feature'
created: '2026-10-26'
status: 'in-progress'
route: 'dispatch'
baseline_commit: 'a1463e27f009bf45f53938c0b656620ac7ebba6d'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-3-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The executor meters memory and CPU per invocation (3.1) but not GPU. An operator cannot see which candybars use the host's NVIDIA GPU. The live container also cannot see the GPU or the host PIDs that `nvidia-smi pmon` reports.

**Approach:**
- One process-wide sampler runs `nvidia-smi pmon -d 1 -s u -o T` while at least one invocation is in flight. It attributes each sample's per-PID SM% to every invocation whose process tree owns that PID.
- The process tree is the child's session (`start_new_session` makes sid = child pid), plus the PIDs in its cgroup scope.
- Average GPU load (busy GPUs; 1.0 = one GPU saturated) is stored in a `gpu_load` invocations column (alembic 0004), carried in completed/failed `data.metrics`, and observed on a `{candybar_id, result}` histogram with `invocation_id` exemplars.
- Production: the compose service gets `gpus: all` and `pid: host` so pmon's PIDs resolve inside the container.

## Boundaries & Constraints

**Always:**
- Metering never raises or fails an invocation. Any of the following meters 0, with at most one warning per process: no `nvidia-smi`, a failing pmon (no driver, non-NVIDIA, unsupported device), or a child that never appears in a sample.
- The sampler is shared. Concurrent invocations share one pmon, which stops when the last session finishes. Its startup and teardown stay off the invocation's critical path.
- `CANDYBAR_NVIDIA_SMI` overrides the binary path; `""` disables GPU metering.
- Labels stay `{candybar_id, result}`; both result series materialize at zero.
- The schema change is additive and optional within revision 1, canonical in bloodbank first, then copied byte-identical.
- The child cannot self-report gpu_load (SelfReportedMetrics unchanged).
- Sampling overhead is at most 50 ms of added wall time per invocation (GPU_SAMPLING_BUDGET_MS), asserted in tests on a fake pmon and on the real one when a GPU is present.

**Never:** No NVML/pynvml dependency, no AMD/multi-tenant attribution (deferred by the spine), no token counter or API read surface (3.3), no executor-owned sleep or timer (AD-1 guard).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| GPU job | child's session PIDs show sm 40, 60 over 2 samples | gpu_load 0.5 on row, event, histogram | N/A |
| Multi-PID / multi-GPU | 2 member PIDs at sm 50 each in one sample | gpu_load 1.0 for that sample | N/A |
| Foreign PID | GPU PID in another session or scope | not attributed | N/A |
| `-` sm / empty sample | `0 - - ...` or sm `-` | contributes 0, still counts as a sample | N/A |
| No samples | job exits before the first sample | 0 | N/A |
| GPU-less host | `nvidia-smi` absent or `""` | 0, pmon never spawned | none raised |
| pmon fails | binary exits with no data lines | 0; sampler disabled for the process | warn once |
| Concurrency | 2 sessions overlap | one pmon process; each gets its own load | N/A |
| Redelivery | key recorded | republished event carries the stored gpu_load; no metric change | N/A |

</frozen-after-approval>

## Code Map

- `src/candybar/executor/gpu.py` (new) -- `GpuSampler` (track(pid, scope) -> `GpuSession`; `finish()` -> float), pmon line parser, process-default `GPU_SAMPLER`. Membership: `/proc/<pid>/stat` session field (after the last `)`, index 3) == child pid, or pid in `scope/cgroup.procs`; both cached per sample block. Block boundary: the time token changes or a (gpu, pid) repeats.
- `src/candybar/executor/runner.py` -- `run_invocation`: track after `_enter_scope`, finish after `communicate()`; `RunOutcome.gpu_load: float = 0.0`; inject `gpu: GpuSampler | None`.
- `src/candybar/executor/worker.py` -- thread `gpu_load` through `TerminalInvocation`, `terminal_event_for`, `record_from_row`, and `_observe`; same pattern as `cpu_load`.
- `src/candybar/executor/metrics.py` -- `candybar_invocation_gpu_load_ratio` histogram plus `GPU_LOAD_BUCKETS`, and add it to `_families`.
- `src/candybar/events/envelope.py` -- `InvocationMetrics.gpu_load` (ge 0, default 0).
- `src/candybar/registry/{models,store}.py` + `deploy/alembic/versions/0004_invocation_gpu_load.py` (down_revision `0003`) -- a Double NOT NULL column with default 0, and extend the existing check constraint pattern with a new `invocations_gpu_load_nonnegative`.
- `../bloodbank/schemas/bloodbank/cron/candybar.{completed,failed}.json` -> `schemas/cron/` -- add an optional `gpu_load` number with minimum 0.
- `deploy/compose.yml` -- `gpus: all`, `pid: host`, `NVIDIA_DRIVER_CAPABILITIES: compute,utility`. CI's `docker run` boot smoke is unaffected, and the hosted runner has no GPU.
- Tests: `tests/test_gpu.py` (new; the fake pmon is a shell script that prints canned blocks with a PID substituted from env), `tests/test_registry_integration.py` (column set), `deploy/prometheus/candybar-queries.test.yml`, the promtool name guard in `tests/test_metrics.py`, and the README Metrics table.

## Tasks & Acceptance

**Execution:**
- [ ] `src/candybar/executor/gpu.py` -- the sampler, parser, and membership described above.
- [ ] `src/candybar/executor/runner.py` -- wire the sampler and add the RunOutcome field.
- [ ] bloodbank schemas, then the drafts; `envelope.py` -- the gpu_load field.
- [ ] `models.py`, `store.py`, migration 0004 -- the column round trip.
- [ ] `worker.py`, `metrics.py` -- thread the value and observe the histogram.
- [ ] `deploy/compose.yml`, README -- GPU visibility and the query docs.
- [ ] tests: every matrix row, the overhead budget, a real-pmon env-gated test (`CANDYBAR_TEST_GPU=1`), and the promtool fixture.

**Acceptance Criteria:**
- Given this host's RTX 3090, when an invocation runs a CUDA/GPU workload, then its gpu_load is > 0 on the row, the event, and the histogram, and the added sampling overhead stays under 50 ms.
- Given a GPU-less environment (CI), when any invocation runs, then gpu_load is 0 and no error is logged at warning or above.
- Given the live container redeployed with GPU visibility, when a GPU candybar is invoked, then its row shows gpu_load > 0.

## Design Notes

- The histogram is named `_ratio` for parity with cpu_load. It is the spine's "GPU load summary" and, unlike a Summary, can carry exemplars.
- Buckets run 0.01–8 busy GPUs.
- pmon's own 1 s interval is an external sampler cadence, not an executor-owned clock (AD-1).
- The open block at `finish()` counts as one sample when non-empty, so a 1–2 s job still gets its burst.

## Verification

**Commands:**
- `uv run ruff check . && uv run pytest -q` -- expected: all pass.
- `CANDYBAR_TEST_GPU=1 uv run pytest -q tests/test_gpu.py` -- expected: the real-pmon tests pass on big-chungus.
- bloodbank `mise run validate:schemas`; promtool fixture; throwaway PG registry integration -- expected: pass.
