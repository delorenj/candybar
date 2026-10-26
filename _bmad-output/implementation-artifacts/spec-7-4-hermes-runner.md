---
title: 'Story 7.4: Hermes runner'
type: 'feature'
created: '2026-10-26'
status: 'in-review'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '8f78327'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-7-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Hermes agent-mode jobs fire from the gateway ticker, so migrating one would leave it on two schedulers, and `hermes cron run` neither waits for the attempt nor reports its usage. Re-implementing Hermes delivery, sessions, `context_from` and monitor gates in candybar is out of the question.

**Approach (AD-19):** Two halves. In delorenj/hermes-agent: a job flag `scheduler: external` that the ticker never fires on its own and that a trigger keeps external, plus `hermes cron run <job> --wait`, which blocks until the durable attempt is terminal, exits 0/1 and prints its token usage. In candybar: a `hermes` runner `{profile, job_id}` that runs exactly that command on the host executor and writes the attempt's input + output tokens into the AD-8 result.

## Boundaries & Constraints

**Always:** a double trigger of one job never yields two concurrent attempts (joined or refused); the exit code stays primary and the parsed usage only refines it; usage reads 0 when unavailable; the no-agent Hermes path stays an `exec` runner with an `on_failure` hook.

**Never:** a candybar-side reimplementation of Hermes delivery or scheduling; restarting the gateway here (one batched `hermes-gateway-restart` in wave 7.17); migrating any job.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| success | attempt `completed` | exit 0; `cron-run-result` line with tokens; invocation `success`, `token_spend` = input + output | usage missing -> 0 |
| failure | attempt `failed`/`unknown` | exit 1; line carries Hermes' error; invocation `failed` with that message and the tokens it spent | none |
| crash, no line | CLI dies | exit code decides: `failed`, `exit code N`, stderr tail kept | none |
| timeout | attempt outlives `timeout_s` | Hermes stops waiting (exit 1, "timed out"); executor kills the CLI `timeout_s + 60s` after start | recorded `failed` naming the timeout |
| double trigger | job already running | the second `--wait` joins the in-flight attempt; one execution row | owner died -> exit 1, not a hang |
| external job, natural time | ticker finds it due | not fired; after a trigger the next natural instant is refused too | none |
| paused job | trigger | refused by Hermes (resume first) -> `failed` | none |

</frozen-after-approval>

## Code Map

- hermes-agent `cron/jobs.py` -- `scheduler` normalizer (`external` | unset), `is_external_scheduler`, the guard at the top of `_evaluate_due_job` (due only when `manual_run_at == next_run_at`), the same refusal in `claim_job_for_fire` for non-manual claims (external providers)
- hermes-agent `hermes_cli/cron_wait.py` -- `run_and_wait`: follows the execution ledger to terminal, joins an attempt in flight, emits `cron-run-result: <json>`; `read_usage` joins `usage_audit.jsonl` on the new `execution_id`
- hermes-agent `cron/scheduler.py` -- `_FireAudit` records `execution_id`; `hermes_cli/cron.py` + `subcommands/cron.py` -- `cron run --wait [--timeout]`, `--scheduler` on create/edit (CLI-only, not the model's); `tools/cronjob_tools.py` plumbing; `cron/job_definition.py` field; tests `tests/cron/test_scheduler_external.py`, `tests/hermes_cli/test_cron_run_wait.py`
- `src/candybar/runners.py` -- `HermesRunner` `{profile, job_id, timeout_s=3600, on_failure?}`; plain-name validation (no leading dash)
- `src/candybar/executor/hermes_runner.py` -- argv, kill deadline (`timeout_s + 60s`), `parse_result_line`
- `src/candybar/executor/runner.py` -- `run_invocation(stdout_result=)`: a result derived from stdout when no result file exists
- `src/candybar/executor/targets.py` -- dispatch to the exec spawn path with the hermes argv
- `deploy/smoke/hermes/` -- the canary: no-agent Hermes job script, manifest, README
- tests: `tests/test_hermes_runner.py` (stub `hermes`; `CANDYBAR_TEST_HERMES=1` live suite against a throwaway `HERMES_HOME`)

## Design Notes

- **Inline run, not just a ticker trigger.** `hermes cron run` already executes inline in the CLI process (or hands a primary-routed job to the ticker, or backgrounds it). `--wait` is defined over the attempt, not the mechanism: it follows the ledger row whichever path produced it. For the common inline path the CLI process IS the attempt, so the exec spawn path meters the agent's real memory and CPU.
- **The external guard is one early return.** Every other pass in the due scan (re-anchor, fast-forward, stale-error re-arm, catch-up, restore-unclaimed-slot) is a way to turn a stale natural instant into a fire; none of them runs for an external job. `mark_job_run` still advances `next_run_at` to the next natural instant, which keeps the "enabled record always has a next_run_at" invariants true and is simply never acted on. The trigger therefore cannot re-enable the natural schedule (the AD-19 `trigger_job` hazard).
- **The pin is inert until the gateway restarts.** The ticker is a long-lived process holding the old code. The guard takes effect at the 7.17 batched restart, so no Hermes job may be cut over before then (README says so). The canary sidesteps it with a yearly natural schedule.
- **Join, never duplicate.** The claim CAS in `claim_job_for_fire` already refuses a second concurrent attempt ("already being fired"); the second `--wait` then follows the live row. Pinned by a live test: two overlapping runner invocations, one execution row.
- **Usage.** `usage_audit.jsonl` had no link to the attempt; it now carries `execution_id`. `total_tokens` is not trusted: token spend is input + output (AD-8). A run that never reached the model (no-agent, early gate) has no line: 0.
- **`--profile default` and scripts.** With `-p default` the real hermes resolves the scripts directory under the real home regardless of `HERMES_HOME`, and a project checkout can put a profile-pinned PM launcher first on PATH. The live tests therefore call `~/.local/bin/hermes` and create jobs without `-p`.
- Rejected: polling `hermes cron runs` with a far-future schedule (the spine's fallback) — the flag was cheap to land and avoids a second source of truth; a candybar-side Hermes API client.

## Verification

- hermes-agent (20a9721964, delorenj/main): new tests 18 passed; the cron suite (1608 tests) passes in an isolated checkout copy except 6 that fail identically on the pristine tree (missing async plugin in my throwaway pytest, and the repo-runtime path guard); real CLI against a throwaway home: success exits 0 with a result line, a failing script exits 1 with its error, a repeated run makes a new attempt.
- candybar (42f83b6): unit suite 978 passed, 68 skipped; `CANDYBAR_TEST_HERMES=1` adds 2 live tests (real CLI exit mapping; double trigger joined, exactly one execution row); ruff clean on the changed files.
- Live (2026-10-26 ~15:50Z): container rebuilt and `candybar-executor` restarted per deploy/README.md, no migration; `/healthz` 200; `deploy-smoke` fired at 15:49, 15:50, 15:51. Canary `deploy-smoke-hermes` (Hermes job `e5b801fca664`, no-agent, `scheduler: external`, yearly natural schedule) registered through the CLI; a manual run recorded `success`, memory 107.5 MB, cpu_load 1.00, one `completed` attempt in the Hermes ledger. First scheduled fire is 16:00Z.

## Independent Review (inline layers, 2026-10-26)

No subagent tool was available; blind, edge-case and verification-gap layers ran inline against the diffs.

- **Patched:** `Infinity`/`NaN` token counts (valid JSON floats) would have raised in the parser (now read 0); a trigger that never produces an attempt row now fails after a bounded grace rather than hanging; a dead attempt owner fails the wait instead of polling forever; `claim_job_for_fire` also refuses non-manual claims so an external provider callback cannot fire an external job.
- **Rejected:** validating the Hermes job's `scheduler` pin from candybar (the container cannot see the Hermes store; story 7.5's `candybar migrate` checks it at cutover); exposing `scheduler` to the model's cronjob tool schema (a model must not pick who schedules its jobs).
- **Deferred:** none new.

## Spec Change Log

- 2026-10-26: implemented in both repos, deployed and verified live (container + executor; the hermes-agent change is on disk for the CLI and reaches the gateway ticker at the 7.17 restart).
