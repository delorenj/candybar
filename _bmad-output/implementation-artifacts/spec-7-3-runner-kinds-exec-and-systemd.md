---
title: 'Story 7.3: Runner kinds (exec and systemd)'
type: 'feature'
created: '2026-10-26'
status: 'in-review'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '7b4bcbd'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-7-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The executor can only exec a bare path, so a migrated systemd job would lose its `EnvironmentFile`, hardening, `TimeoutStartSec` and `OnFailure=` alerting, and an exec job cannot carry argv, workdir, env literals, a timeout or a failure hook.

**Approach (AD-18):** `candybars.runner` (jsonb) selects `exec` (`{argv[], workdir?, env{}?, timeout_s?, on_failure?}`) or `systemd` (`{unit}`; `systemctl --user start --wait`, metered from the unit after exit). The bare `entrypoint` stays the exec default. The AD-8 env reaches systemd jobs through a once-installed drop-in `50-candybar.conf` (`EnvironmentFile=-%t/candybar/inv/%n.env`) whose env file the runner rewrites per start. `on_failure` is an argv run once, best effort and unmetered, after a recorded `failed`, with `CANDYBAR_FAILURE_MESSAGE`; never on a systemd runner whose unit already has `OnFailure=`.

## Boundaries & Constraints

**Always:** `entrypoint` stays valid and unchanged for existing rows (NULL runner); `env` holds non-secret literals only; no `daemon-reload` per run; metering is best effort and reads 0, never failing a run; every runner failure goes through the one record path.

**Never:** the hermes runner (7.4), overlap/backlog skips (AD-20), the ledger or `enabled` flag (7.5), migrating any job; a host/container routing field; per-run `daemon-reload`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| exec runner | argv, workdir, env, timeout_s | runs argv from workdir (default home) with literals under the AD-8 vars; SIGTERM then SIGKILL after timeout | timeout recorded `failed` naming the timeout |
| systemd success | loaded, inactive unit | start --wait rc 0; MemoryPeak, CPUUsageNSec / main-process span metered | unreadable props meter 0, still success |
| systemd failure | unit exits 3 | `failed`, exit_code 3, journal tail for that InvocationID | no journal: client stderr |
| unit start fails | never ran (resources, not-found) | `failed` with systemctl's own message | none |
| drop-in round trip | first start of a unit | drop-in + holder reference written, one reload; second start rewrites only the env file | drift is restored |
| on_failure | recorded fresh failure | argv runs once with protocol env + failure message | skipped if unit has OnFailure=; hook errors swallowed |
| overlap | unit already active | refused, not joined | recorded failed (AD-20 skip is later) |

</frozen-after-approval>

## Code Map

- `src/candybar/runners.py` -- pure `ExecRunner` / `SystemdRunner` models, `canonical_runner`, `runner_label`; the one validator the manifest, API and executor share
- `src/candybar/registry/{manifest,models,store}.py` + `deploy/alembic/versions/0006_candybar_runner.py` -- `entrypoint` xor `runner`; nullable jsonb (`none_as_null`); the NOT NULL `entrypoint` column holds the runner's label; `get_run_target`; PATCH switches shapes
- `src/candybar/api/app.py` -- runner on register/PATCH (filesystem check only for a plain entrypoint, per the 7.2 note); `GET /candybars/{id}/runner` (outside the contract); the contract-verbatim single shape is unchanged
- `src/candybar/executor/runner.py` -- `run_invocation` takes `argv/workdir/env/timeout_s` beside the legacy `entrypoint`; timeout kill; `RunOutcome.detail`
- `src/candybar/executor/systemd_runner.py` -- the systemd runner (drop-in, holder, metering, journal tail)
- `src/candybar/executor/targets.py` -- `run_target` (the executor's default `run`) and `run_on_failure`
- `src/candybar/executor/worker.py` -- hands `entrypoint=` or `runner=` to run; fires the hook after a fresh recorded failure
- `deploy/host/` -- `candybar-unit-holder.service`, `candybar-smoke.service`, `install.sh`
- tests: `test_runners.py`, `test_exec_runner.py`, `test_systemd_runner.py` (stub + `CANDYBAR_TEST_SYSTEMD=1` live), `test_executor_runner_dispatch.py`, `test_runner_integration.py` (live PG), `test_registry_integration.py`

## Design Notes

- **Reality check that overturned the premise.** On systemd 257 a *successful* oneshot is garbage-collected the instant it stops: `show` returns `MemoryPeak=[not set]`, `CPUUsageNSec=[not set]`, an empty `InvocationID` and zero timestamps. A failed unit stays loaded. The AC's "unit properties persist for an exited oneshot" is false as written. A unit that an active unit references stays loaded, so the runner lists each started unit as `After=` of a long-running `candybar-unit-holder.service` (generated drop-in, one reload per new unit). `After=` adds the reference and no behaviour. Both facts are pinned: a live test proves the loss without the holder, and the metered end-to-end test proves the fix. Rejected: polling `show` while the unit runs (misses short jobs, final CPU unknown); `RemainAfterExit` (changes the unit's semantics: the next start becomes a no-op); a D-Bus `RefUnit` (needs a persistent bus connection and a new dependency); an `ExecStopPost` meter (instruments the unit).
- **`entrypoint` xor `runner`.** The NOT NULL `entrypoint` column holds the path, or for a runner row a short label (`systemd:<unit>`, argv[0]). The definition seen by PATCH is the runner for a runner row, so supplying one replaces the other and the `updated` fact reports the implicit switch (`runner: null`). `runner` is held canonical (defaults applied, unset optionals dropped) so a re-PATCH of the stored value is a no-op.
- **`exec` workdir** defaults to the user's home (what cron and `systemd --user` give jobs), unlike the legacy entrypoint, which keeps running from its own directory.
- **systemd result file.** `CANDYBAR_RESULT_FILE` points under `%t/candybar/inv/`; a unit that writes it is honoured, and a sandboxed unit that cannot just falls back to the exit code.
- **Journal tail** is fetched for failures only and is stdout+stderr merged (the journal does not distinguish).
- **Stop.** The executor's shutdown terminates the `systemctl` client, not the unit; the invocation is recorded `failed` (stopped) and no hook fires.
- **API surface.** `runner` is deliberately absent from the Holocene contract shapes, like `entrypoint`; `GET /candybars/{id}/runner` exposes it for verification and for 7.5's `candybar migrate`.

## Verification

- Unit suite with live Postgres/NATS and `CANDYBAR_TEST_SYSTEMD=1`: 991 passed, 16 skipped; ruff clean on `src` and the new tests (11 pre-existing `E741` in untouched fault-injection/misfire tests); deploy-hygiene gate OK.
- `CANDYBAR_TEST_SYSTEMD=1`: 6 live tests on the real systemd 257 user manager (the GC premise, end-to-end metering with env reaching the unit, failing unit with journal tail, unstartable unit, unknown and overlapping unit, `OnFailure=` skip).
- Live deploy (2026-10-26 ~15:16Z): container rebuilt per deploy/README.md (alembic 0005 -> 0006 ran, `/healthz` 200), `candybar-executor` restarted (schema guard open at head 0006), `candybar-unit-holder` installed and active. `deploy-smoke` fired on the new executor at 15:16, 15:17 and 15:18. Canaries `deploy-smoke-exec` and `deploy-smoke-systemd` (manifests in `deploy/smoke/`, noise, `*/15`) registered through the CLI: a manual run of each recorded `success` with memory 3.1 MB / 3.8 MB and CPU 0.22 / 0.33; the unit's journal shows the AD-8 env (invocation id, trigger) reached it; the generated drop-in and holder `After=` line are in place.

## Independent Review (inline layers, 2026-10-26)

No subagent tool was available; blind, edge-case and verification-gap layers ran inline against the staged diff.

- **Patched:** a hook that raised in the worker would have stranded the command until ack_wait (now caught and logged, with a test); the overlap refusal now names `SubState` so an `active (exited)` `RemainAfterExit` unit is diagnosable; `ProcessRegistry.terminate` takes a signal for the timeout's SIGKILL escalation; a killed job whose grandchild holds the pipes no longer hangs `communicate()`.
- **Rejected:** validating `on_failure` against the unit's `OnFailure=` at registration (the container cannot see the unit; enforced at run time and in `candybar migrate`, 7.5); exposing `runner` on the Holocene shapes (contract-verbatim; `/runner` instead).
- **Deferred:** AD-20 (overlap `skipped`, executor-backlog coalescing) has no owning story in 7.3-7.6; see `deferred-work.md`.

## Spec Change Log

- 2026-10-26: deployed and verified live; implemented; the "properties persist" AC premise was falsified on systemd 257 and replaced by the holder (Design Notes).
