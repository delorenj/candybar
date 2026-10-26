---
title: 'Story 7.5: Migration ledger, rollback and the migrate CLI'
type: 'feature'
created: '2026-10-26'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: 'fd196ef549f4b2f8459ed82d9e3a2e251beae714'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-7-context.md'
  - '{project-root}/_bmad-output/planning-artifacts/cron-inventory.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Waves 7.7-7.17 must move 70 live jobs without any job firing on two schedulers or on none, every cutover must be reversible even with the daemon down, and nothing records which scheduler owns a job or how to put it back.

**Approach (AD-21):** a committed `deploy/migration/ledger.yaml` (one entry per inventory id) driven by `candybar migrate {plan,cutover,verify,rollback,status}`, a REST client of the api role. A new `candybars.enabled` flag lets cutover register the candybar disabled, retire the native scheduler, then enable it; rollback restores the native scheduler first and disables the candybar best effort.

## Boundaries & Constraints

**Always:** cutover order is register disabled -> retire native -> enable, refused near a fire; rollback restores native first and never disables the candybar when the restore failed (a job is never left with no scheduler); the CLI never writes the registry directly (AD-10) and never runs a job; every ledger write is atomic; commands are the exact shell strings committed in the ledger.

**Never:** migrating any real job (waves do); hand-rolled registration SQL; a candybar watchdog (7.6); AD-20 overlap/backlog coalescing.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| cutover | planned entry, native active, no fire near | registered `enabled=false`, disable cmds run, retired check passes, enabled; status `cutover` | none |
| fire near | native or candybar fire inside the guard | refused (exit 1) naming the fire; `--wait` sleeps until the window opens | none |
| disable fails | a disable cmd exits non-zero / native still active | rollback cmds restore native, candybar stays disabled, status unchanged | exit 1 |
| enable fails | API error after retirement | native restored, candybar left disabled | exit 1 |
| rollback, API down | entry `cutover`/`verified` | native restored and checked active; `disable_pending` recorded; status `rolled-back` | re-run finishes the disable |
| rollback, restore fails | rollback cmd fails | candybar NOT disabled, status unchanged | exit 1 |
| verify | first scheduled/misfire run after cutover | success -> `verified` + invocation id; fail -> verified only when the entry records a native pre-existing failure | none yet -> exit 3 pending |
| disabled candybar | `enabled=false` | dispatcher never dispatches it; no current/upcoming `/schedule` rows; executor skips a queued scheduled/misfire command (`skipped`, reason `disabled`) | manual run still allowed |

</frozen-after-approval>

## Code Map

- `deploy/alembic/versions/0007_candybar_enabled.py` (new) + `registry/{models,manifest,store}.py` -- `enabled` bool NOT NULL default true; `list_schedules` returns enabled rows only; `candybar_enabled()` for the executor
- `events/{envelope,builders}.py`, `schemas/cron/candybar.created.json` (+ bloodbank canonical) -- optional `enabled` on `created`; `updated.changes` is already open
- `api/app.py` -- `enabled` on register; `POST /candybars/{id}/enabled {enabled}` (the noise flow: lock, publish `updated` first, write); `api/shapes.py` definition gains `enabled`; contract doc updated in the same commit
- `api/schedule.py`, `api/stream.py` -- a disabled candybar yields no row at/after `cut`, is never `current`, and keeps only past rows with a recorded result
- `executor/worker.py`, `executor/role.py` -- optional `enabled` port; scheduled/misfire command of a disabled candybar -> `skipped(reason=disabled)` + ack
- `src/candybar/migration/` (new) -- `ledger.py` (pydantic entries, locked atomic YAML I/O), `native.py` (per-kind probes + default commands: `systemd-timer`, `crontab`, `hermes`), `client.py` (urllib REST), `ops.py` (the five verbs, injected shell/client/clock); `cli/main.py` mounts `migrate`
- `deploy/migration/{ledger.yaml,README.md}` -- empty ledger + schema and procedure

## Tasks & Acceptance

**Execution:**
- [x] registry, migration 0007, events, schema mirrors -- the flag and its facts
- [x] api enable route, shapes, schedule/stream filtering, contract doc -- disabled semantics
- [x] executor disabled guard + role wiring -- queued commands cannot resurrect a rolled-back job
- [x] migration package + CLI + ledger + README; pyyaml 6.0.3 dependency
- [x] tests: enable route, schedule/stream, dispatcher filter, executor guard, ledger I/O, native probes (stub shell), every matrix row of `ops` with a fake API, a live-PG enable round trip

**Acceptance Criteria:**
- Given a stub ledger entry and a stub native scheduler, when `cutover` then `rollback` run (the API reachable, then unreachable), then the shell calls and API calls happen in the documented order and the ledger statuses match the matrix.
- Given the live deployment, when redeployed, then migration 0007 runs, every existing candybar reads `enabled: true`, and `deploy-smoke` still fires each minute.

## Implementation Notes

- **Decisions recorded (no human to ask):** `POST /candybars/{id}/enabled` takes the wanted state `{enabled}`, not a blind toggle, so a retried request or a rollback re-run converges. `PATCH` cannot touch `enabled` (one route owns it). `created` gains an optional `enabled` (bloodbank canonical `a358079`, mirrored byte-identical in `schemas/`). A manual `candybar run` of a disabled candybar still runs (explicit operator intent); only queued scheduled/misfire commands are skipped by the executor (`skipped`, reason `disabled`, deterministic id per idempotency key, acked, fact only, like dispatcher skips).
- **Disabled in `/schedule`:** no row at/after `cut`, never `current`; a past fire is a row only when an invocation was recorded for it (`visible_rows`, applied after `attach_results` in the route and the stream's windowed rows; `_fact_row` returns no row for a disabled candybar's unrun or future fire). A windowless stream client refetches on an `enabled` change (stream docstring + contract doc).
- **Ledger I/O:** pyyaml 6.0.3 (new pinned dep, recorded in the spine Stack table). YAML comments do not survive a rewrite, so the schema lives in `deploy/migration/README.md` and the file carries a fixed header. Writes are temp file + fsync + `os.replace`, round-trip validated first; the lock is a non-blocking `flock` on the ledger's directory (the file inode changes on every write).
- **Never zero schedulers:** cutover persists `candybar_id` before touching the native scheduler; any failure (including an unexpected exception) after the native scheduler was touched runs the rollback commands and leaves the candybar disabled; a failed restore prints MANUAL ACTION. Rollback restores and probes the native scheduler active before it disables anything, and never disables when the restore failed.
- **Guard extras beyond the spec's Design Notes:** a 15 s settle after a candybar fire (a crontab run has no "in progress" signal, and the tick is still being dispatched); `--wait` polls every 5 s while holding the ledger lock.
- **Cutover blockers:** missing `source_of_truth.commit`; missing disable/rollback commands; a systemd runner `on_failure` hook on a unit that already has `OnFailure=` (the 7.3 deferral, AD-18). `verify` also fails when the native scheduler is active again (a re-install resurrected it). Hermes entries are documented as not cuttable before the 7.17 gateway restart (7.4's inert pin).
- Files: alembic `0007_candybar_enabled.py`; `registry/{models,manifest,store,__init__}.py`; `events/{envelope,builders}.py`; `api/{app,shapes,schedule,stream}.py`; `executor/{worker,role}.py`; `migration/{__init__,ledger,native,client,ops}.py`; `cli/{main,migrate}.py`; `deploy/migration/{ledger.yaml,README.md}`; README, deploy/README, holocene contract, schemas, spine Stack row, CI (adds `test_enabled_integration.py` to the PG step). Tests: `test_api_enabled.py`, `test_executor_disabled.py`, `test_migrate_ledger_native.py`, `test_migrate_ops.py`, `test_enabled_integration.py`, plus enabled fields in existing rigs; safe ruff fixes in five pre-existing test files so `ruff check .` (CI) is clean.

## Review Triage Log

Inline layers (no subagent tool in this session): blind, edge-case and verification-gap over the staged diff.

| # | Finding | Verdict | Evidence / route |
|---|---------|---------|------------------|
| 1 | An unexpected exception (not `ApiError`) after the disable commands ran would leave the native scheduler retired and the candybar disabled: zero schedulers | medium | Real: only `ApiError` was caught around steps 2-3. Patched: a broad catch routes to the restore; test `test_a_surprise_after_retirement_still_restores_native` |
| 2 | Crontab natives have no "run in progress" signal; a cutover in the first second of a fire minute could straddle the cron daemon's run or the tick's dispatch | low | Real but narrow. Patched (trivial guard): 15 s settle after a candybar fire; test `test_cutover_refused_in_the_first_seconds_of_a_fire_minute` |
| 3 | The executor role could drop the `enabled=` wiring with no test failing | low (gap) | `test_executor_role.py` asserted only `by_key`/`record`. Patched: asserts `enabled` is wired |
| 4 | `tests/test_enabled_integration.py` (the only live proof of `list_schedules` filtering and migration 0007) is not in CI's PG step | low (gap) | CI lists PG test files explicitly. Patched: added to the step |
| 5 | Hermes entries: the `scheduler: external` pin is inert until the 7.17 gateway restart, so `verify`/probe would read "retired" while the ticker still fires | low | Real; enforcing needs gateway-boot introspection. Documented in deploy/migration/README.md; 7.17 owns the ordering |
| 6 | Past `/schedule` rows between a disabled registration and the enable read as "fired, no result" once enabled | low | Real, at most one minute of rows at cutover; rejected (fix would need a per-candybar enable history) |
| 7 | A crontab line with trailing whitespace is "present" to the probe but not matched by `grep -x` | false | The disable then fails the retired probe, so cutover restores (append-unless-present is a no-op) and refuses: safe failure, never two schedulers |
| 8 | `plan --write` rewrites the ledger even when nothing changed | low | Rejected: rewrite is idempotent (same bytes plus a history row only when defaults were written) |
| 9 | The dispatcher's misfire coalescing could run a fire from a candybar's disabled period if a dispatcher gap closes after re-enable | maybe-false | Needs the dispatcher down while the API (same container) is up; not reachable in this deployment. Rejected |
| 10 | Five pre-existing ruff errors fail CI's `ruff check .` | low | Pre-existing (7.3/7.4 test files); fixed with safe autofixes since CI would fail on this push |

## Design Notes

- **Guard window** = min(2 min, period/4) before the next native fire AND the next candybar fire; period = the candybar cron's gap around now. A strict 2 minutes would make `*/2` and `* * * * *` jobs uncuttable. A calendar timer with jitter also refuses while a jittered native run of the candybar's previous fire may still be pending (`native.last < candybar.prev` and now < prev + jitter); a systemd timer whose service is active refuses.
- **Linked units.** On systemd 257 `disable` of a `systemctl link`-ed timer removes the link, so `enable <name>` fails afterwards (reality-checked). The default rollback enables by the unit file's resolved absolute path, which works for linked and plain units.
- **Commands** run as `bash -euo pipefail -c '<cmd>'`; `plan --write` fills kind defaults into the ledger so the committed commands are the exact ones.

## Verification

- `uv run pytest -q` and ruff clean; live-PG suite (`CANDYBAR_TEST_PG=1`) for the enable round trip.
- Live: container rebuilt + `candybar-executor` restarted per deploy/README.md; `/healthz` 200; alembic head 0007; next tick fires `deploy-smoke`; `candybar migrate status` reads the empty ledger.

**Results (2026-10-26):**
- Unit suite 1066 passed, 71 skipped; with throwaway postgres:17-alpine + nats:2.10.29 and `CANDYBAR_TEST_{PG,NATS,SYSTEMD}=1`: 1119 passed, 18 skipped. `ruff check .` clean. bloodbank `scripts/validate_schemas.sh`: 186 schemas valid.
- Live systemd (`CANDYBAR_TEST_SYSTEMD=1`): a `systemctl link`-ed throwaway timer round-trips through the default disable/rollback commands (the link vanishes on disable; enable-by-name fails; enable-by-path restores it active with a next elapse).
- Deploy (commit 7926b63): container rebuilt, alembic ran 0006 -> 0007, `/healthz` 200; `candybar-executor` restarted, schema guard open at 0007. `deploy-smoke` fired at 16:32:00Z (success, memory 256 KiB, cpu 0.07). All six registered candybars read `enabled: true`.
- Live disabled proof on `hello-node` (`*/5`, example job, no side effects): disabled at 16:32Z -> `/schedule` showed only its past rows that ran, `/schedule/next` moved on, and the 16:35 tick dispatched 1 candybar (deploy-smoke only); re-enabled at 16:35Z -> it fired at 16:40:00Z (success), no catch-up run. Restored to enabled.
- Live read-only `candybar migrate plan --write` on a throwaway ledger for `systemd:cache-prune-daily.timer`: probed the real timer (enabled/active, next 09:24:53Z with its jitter), resolved the linked unit file to `~/code/infra/systemd/user/cache-prune-daily.timer` for the rollback command, computed the candybar fires in New York time, and refused on the missing source-of-truth commit (exit 1). No job was migrated; the committed ledger is empty.
