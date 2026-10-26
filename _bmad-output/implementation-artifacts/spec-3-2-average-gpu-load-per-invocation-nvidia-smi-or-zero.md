---
title: 'Story 3.2: Average GPU load per invocation (nvidia-smi or zero)'
type: 'feature'
created: '2026-10-26'
status: 'done'
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
- [x] `src/candybar/executor/gpu.py` -- the sampler, parser, and membership described above.
- [x] `src/candybar/executor/runner.py` -- wire the sampler and add the RunOutcome field.
- [x] bloodbank schemas, then the drafts; `envelope.py` -- the gpu_load field.
- [x] `models.py`, `store.py`, migration 0004 -- the column round trip.
- [x] `worker.py`, `metrics.py` -- thread the value and observe the histogram.
- [x] `deploy/compose.yml`, README -- GPU visibility and the query docs.
- [x] tests: every matrix row, the overhead budget, a real-pmon env-gated test (`CANDYBAR_TEST_GPU=1`), and the promtool fixture.

**Acceptance Criteria:**
- Given this host's RTX 3090, when an invocation runs a CUDA/GPU workload, then its gpu_load is > 0 on the row, the event, and the histogram, and the added sampling overhead stays under 50 ms.
- Given a GPU-less environment (CI), when any invocation runs, then gpu_load is 0 and no error is logged at warning or above.
- Given the live container redeployed with GPU visibility, when a GPU candybar is invoked, then its row shows gpu_load > 0.

## Implementation Notes

- Planning gates (unattended run, recorded): the spec is about 1.5k tokens, under 1600. There were no Open Questions; every choice was settleable from code, spine, or host evidence, and each is recorded in Intent/Design Notes. Checkpoint 1 was auto-approved ("Approve and continue") under the workflow harness's no-questions directive. Subagents are unavailable in this session, so implementation and review ran inline, the same deviation recorded for 1.1–3.1.
- g33 preflight PASS. The route matched Bloodbank event naming, and no new event types were added; the change is additive within revision 1.
- Host evidence that settled the design:
  - pmon streams line-buffered through a pipe, with the first sample at about 40 ms.
  - The header row repeats mid-sample, so blocks are split by the `-o T` time token plus (gpu, pid) repeats, never by header lines.
  - Cost is about 3% of one core at a 1 s interval.
  - Inside a plain container pmon lists no processes, but with `--pid host` it lists host PIDs that resolve in `/proc`. That is why compose uses `pid: host`.
- Canonical schemas went first: bloodbank `39fd1a2` adds optional `gpu_load` to completed/failed `data.metrics`. `mise run validate:schemas` passed 186/186, and the drafts were copied byte-identical.
- Implementation commit `90c0adb` (21 files):
  - The fake pmon in `tests/test_gpu.py` is a Python script rather than the shell script the Code Map names, because it has to loop and read announced PIDs.
  - `tests/conftest.py` pins `CANDYBAR_NVIDIA_SMI=""` (setdefault), so the unit suite is deterministic on GPU hosts.
  - `scripts/gpu-burn-candybar.py` is a dependency-free CUDA driver-API load job: a PTX busy-loop through ctypes into libcuda.
  - One adjacent test expectation changed: the exact `data.metrics` dict in `tests/test_metering.py` gained `gpu_load: 0.0`.
- Verification:
  - Unit tests: 438 passed (the suite includes concurrent stories). Ruff is clean.
  - Real-GPU tests (`CANDYBAR_TEST_GPU=1`) 3/3 on the RTX 3090:
    - The CUDA burner was attributed above 0.3.
    - A CPU job read exactly 0 while desktop GPU processes ran at up to 24% SM.
    - Median overhead stayed inside 50 ms.
  - promtool SUCCESS. Registry integration 8/8 on a throwaway postgres:17-alpine, with migration 0004 up, down to base, and up again. Executor, manual-invoke and fast-stop integration 4/4 on throwaway NATS 2.10.29 + PG.
- Throwaway container proof: the image ran privileged with `--gpus all --pid host` against throwaway NATS/PG. `candybar register` + `run` of the burner gave:
  - row gpu_load 0.745;
  - the completed event `data.metrics.gpu_load` 0.745;
  - an OpenMetrics `candybar_invocation_gpu_load_ratio` bucket carrying the `invocation_id` exemplar;
  - `docker stop` in 636 ms.
- Live redeploy (`docker compose up -d --build`):
  - Migrations 0002->0003 (story 2.2's, first live apply) and 0003->0004 applied at boot.
  - The `gpu-burn` candybar (`~/candybar/jobs/gpu-burn`, yearly schedule, manual proof job) was registered and invoked: row gpu_load 0.505, runtime 3340 ms.
  - Live Prometheus scraped `candybar_invocation_gpu_load_ratio_sum` 0.505, with the fail series at 0.
  - The `deploy-smoke` and `hello-node` rows after the redeploy read 0.0, with zero warnings in the logs.
  - The gpu-burn burner reads lower live than in the container (0.505 vs 0.745) because CUDA context setup and PTX JIT are idle GPU time inside the wall-time average, the same semantics as cpu_load.
- Concurrency: stories 2.2 and others share the working tree. Only this story's files were committed (pathspec commit). `tests/test_registry_integration.py` was clean at commit time, since 2.2 had already landed its edits.

## Spec Change Log

## Review Triage Log

Review layers ran inline (blind-hunter, edge-case-hunter, verification-gap), because this session cannot spawn subagents; this is the same deviation recorded for stories 1.1–3.1. The diff is commit `90c0adb`, 65.4 kB, so the blind-hunter floor is N = min(floor(sqrt(65.4)+1), 10) = 9.

| # | Layer | Finding | Verdict | Evidence / route |
|---|---|---|---|---|
| B1 | blind/claims | An unexpected pmon exit after samples logs WARNING on every restart, which breaks the frozen "at most one warning per process" rule | medium | Real: the `_pump` finally block logged unconditionally. **patch**: only the first exit warns, later ones log at debug. Test `test_pmon_dying_mid_run_warns_once_and_restarts` (3 restarts, 1 warning). |
| B2 | blind | When pmon dies mid-run, the open block's pending contribution for active sessions is dropped | low | Real but needs a crash mid-block, and costs at most one partial sample. The fix adds state branches. **reject**. |
| B3 | blind | A pmon start/stop per busy period means a burst of short jobs respawns nvidia-smi each time | low | By design (frozen: stop when the last session finishes). Spawn is about 40 ms of sys CPU and off the await path; the overhead test bounds it. **reject**. |
| B4 | blind | nvidia-smi pmon monitors at most 4 devices, so GPUs 5+ would never be attributed | low | nvidia-smi limitation; the host has one GPU, and multi-GPU attribution beyond v1 is spine-deferred. **reject**. |
| B5 | blind | Without `pid: host`, a GPU-visible container silently meters 0 | low | Observed: pmon lists no processes there, which looks the same as an idle GPU, so it cannot be detected cheaply. Documented in the compose comment. **reject**. |
| B6 | blind | The first sample blocks of a run (CUDA context setup, JIT) read 0 and pull the average down | false | Not a defect: gpu_load is the average over the invocation's wall time, the same semantics as cpu_load (spine). Idle setup time is real idle GPU time. |
| B7 | blind | `test_default_sampler_is_off_in_the_unit_suite` fails if a developer exports `CANDYBAR_NVIDIA_SMI` | low | It is the suite contract that conftest establishes; it only breaks under a deliberate override. **reject**. |
| B8 | blind | Overhead tests compare wall medians on a shared 32-thread box and could flake | false | Three back-to-back runs of `tests/test_gpu.py` under concurrent agent load passed in 6.87–6.90 s. The budget (50 ms) is far above the measured delta, and medians of 7 runs absorb outliers. |
| B9 | blind | `GpuSampler(binary: str | None | object)` typing is loose (sentinel) | low | Cosmetic; mypy is not in the toolchain. **reject**. |
| E1 | edge | `create_subprocess_exec` raising a non-OSError (e.g. RuntimeError on loop teardown) leaves `_run` set, so pmon never restarts | false | `_finish` calls `_stop_run` when the last session ends, which clears `_run` unconditionally. The next track starts fresh. |
| E2 | edge | Non-numeric sm token (`N/A`) | false | `float()` raises ValueError, which returns early: the line counts toward the block and contributes 0. |
| C1 | claims | "startup and teardown stay off the critical path": spawn still runs on the same event loop (a fork blocks briefly) | false | The claim is about the invocation's await path, and the spawn is a separate task. Measured overhead is within budget, both fake (`test_sampling_overhead_within_budget_fake_pmon`) and real (`test_real_pmon_overhead_within_budget`). |
| V1 | verification-gap | The compose GPU wiring (`gpus: all`, `pid: host`) has no automated check; dropping `pid: host` would make prod GPU load silently 0 | gap | No YAML parser is in the dependencies, and a source-text assertion does not count as verification. Proven live instead (row 0.505 after redeploy). **defer** (deferred-work.md). |
| V2 | verification-gap | Real-pmon attribution never runs in CI (env-gated, no GPU runner); a driver output-format change would ship green | gap | Parser tests use the real captured header and line shapes, and the live deploy is the backstop. A GPU CI runner would mean self-hosted runners on a public repo. **defer** (deferred-work.md). |

### Independent review (fresh context, 2026-10-26)

A second reviewer, not the implementer, reviewed commits `90c0adb` + `b22582e` (diff 1633 lines) against the current tree. Layers ran inline because no subagent capability was available.
- AC 1 holds. Re-run on the RTX 3090 with `CANDYBAR_TEST_GPU=1 CANDYBAR_NVIDIA_SMI=nvidia-smi`: the real-pmon tests passed 3/3 (CUDA burner attributed, foreign GPU processes not attributed, overhead inside 50 ms).
- AC 2 holds. GPU-less runs are silent because the unit suite pins `CANDYBAR_NVIDIA_SMI=""`, and the CI image has no nvidia-smi.
- AC 3 holds. The live `gpu-burn` row reads gpu_load 0.505 after the redeploy. Live `hello-node`/`deploy-smoke` rows read 0.0, and the container logs show no GPU warnings.

| # | Layer | Finding | Verdict | Evidence / route |
|---|---|---|---|---|
| R1 | edge | If two pmon samples share one wall-clock second with disjoint PID sets, they merge into one block, which undercounts samples | low | Needs both cadence drift and a PID-set change inside one second. Effect is at most one sample per occurrence. **reject**. |
| R2 | edge | A session tracked while a block is open misses that block's earlier lines but still counts it as a full sample | low | Costs at most one partial sample at the start of the job. **reject**. |
| R3 | edge | A NaN SM token would propagate as NaN gpu_load (the `max`/`min` clamp passes NaN through) | low | pmon prints integer or `-` SM values only. **reject**. |
| R4 | verification | Compose keeps `cgroup: private`, `pid: host`, `gpus: all`, and the live container inspect shows `private host true` | n/a | Verified live. Automating the compose check stays deferred (V1). |

Verdict: **done**. All ACs hold, and no defect needs a patch.

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
