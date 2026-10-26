---
title: 'Story 5.5: Noise collection endpoints'
type: 'feature'
created: '2026-10-26'
status: 'done'
baseline_commit: '02b1559e16246ff137a744cc94d656e580d2b985'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-5-context.md'
  - '{project-root}/_bmad-output/specs/spec-cron-framework/holocene-view-contract.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The Holocene view's noise toggle needs the contract's noise-collection writes: `PUT /candybars/{id}/noise`, `DELETE /candybars/{id}/noise` and `PUT /noise/batch` with `{ ids: [] }`. Today `noise` can only change through a full `PATCH`.

**Approach:** Three api routes flip the stored `noise` flag. They use the PATCH flow: lock, diff, publish one `bloodbank.cron.candybar.updated` (`changes: {noise}`) per changed candybar, then write in the same transaction. `/schedule` and the stream already read `noise` from the registry and the `updated` facts, so they reflect the change with no new code.

## Boundaries & Constraints

**Always:**
- `PUT` adds one candybar to the collection (`noise: true`) and `DELETE` removes it (`noise: false`). Both answer 200 with the single-definition shape. If the flag already has that value, the call is a no-op: no fact and no write.
- `PUT /noise/batch` replaces the collection. Afterwards exactly the given ids are noise and every other candybar is not, so one call can both add and remove. Duplicate ids are applied once, and `[]` clears the collection. The answer is 200 `{ids}`: the resulting collection, oldest candybar first.
- A batch is atomic. It locks the rows FOR UPDATE, ordered by id: the given ids plus the current noise rows. Every fact is published before any row is written, and everything commits as one transaction. A failure at any point writes nothing.
- All errors are problem+json:
  - single routes: unknown id → 404, malformed id → 422, bus down → 503 with the row unchanged;
  - batch: unknown ids → 422 naming them, a bad body (missing, null, malformed, extra keys, more than 10,000 ids) → 422, bus down → 503;
  - database down → 500.
- No auto-classification: only these calls (and `PATCH`/register) set `noise`. A minutely candybar stays `noise: false` until a caller adds it.

**Never:** No new Bloodbank type or schema; the `updated` schema already lists `noise` as a change. No stream code changes, no migration, no CLI, no auth, no holocene changes. No `<10m` logic. No compensating facts after a partial batch publish failure: the 503 tells the caller to retry, and replace semantics make a retry converge.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Add one | `PUT` on a non-noise candybar | 200, `noise:true`, one fact `{noise:true}` | n/a |
| Remove one | `DELETE` on a noise candybar | 200, `noise:false`, one fact `{noise:false}` | n/a |
| Idempotent single | `PUT` on a noise candybar | 200, nothing published or written | n/a |
| Batch replace | noise {a,b}; ids [b,c,c] | facts a→false and c→true only; 200 `{ids:[b,c]}` | n/a |
| Batch clear | ids [] | every noise row → false | n/a |
| Batch unknown id | ids [a, unknown] | 422 naming unknown; nothing published or written | problem+json |
| Batch bus fails at fact k | 2 changes, 2nd publish fails | 503; no row written | logged |
| Batch write fails mid-way | 2nd row write raises (live) | 500; transaction rolled back, first row unchanged | problem+json |
| Schedule reflects | after `PUT` on a | `/schedule` rows for a read `noise:true`, others false | n/a |

</frozen-after-approval>

## Code Map

- `src/candybar/api/app.py` -- `create_app`: the `update` (PATCH) route is the pattern to follow (lock_candybar → diff → `build_candybar_updated` → `sink.publish` with `BUS_ERRORS` → `update_candybar` → `candybar_detail_json`). Reuse `problem`, `BUS_ERRORS` and `candybar_detail_json`. Add the routes and a `NoiseBatchRequest` (extra=forbid, `ids: list[UUID]`, max 10,000). Update the module docstring.
- `src/candybar/registry/store.py` -- add `lock_noise_batch(session, ids)`: `SELECT ... WHERE noise OR id = ANY(:ids) ORDER BY id FOR UPDATE`, with one array parameter like `fire_results`. Reuse `lock_candybar` and `update_candybar` unchanged. Export from `registry/__init__.py`. DB access stays in the store (AD-10 guard).
- `src/candybar/api/stream.py::_on_definition` -- already maps `updated` with `changes.noise` to `definition.changed`, overlaid before commit. Do not change it.
- `src/candybar/api/schedule.py::WindowCandybar.of` -- rows take `noise` from the row. Do not change it.
- `tests/test_api_definitions.py` -- `Rig`, `Row`, `FakeSink`, `T0`: the in-memory rig to import. Rig fixtures monkeypatch `candybar.api.app` names.
- `tests/contract_shapes.py` -- add a parse of the "Noise collection" section: its `METHOD /path` routes and the batch body keys.
- `tests/test_registry_integration.py::migrated_db` -- the fixture to import for the live test (the pattern of `test_schedule_next_integration.py`).
- `.github/workflows/ci.yml` -- the live-Postgres step (`CANDYBAR_TEST_PG=1`) gets the new live file.

## Tasks & Acceptance

**Execution:**
- [x] `src/candybar/registry/store.py` + `__init__.py` -- `lock_noise_batch` -- the batch's lock set: one ordered locker, no deadlock between batches.
- [x] `src/candybar/api/app.py` -- `PUT`/`DELETE /candybars/{id}/noise` and `PUT /noise/batch` per the frozen rules.
- [x] `tests/contract_shapes.py` -- `noise_routes()` and `noise_batch_keys()` parsed from the contract.
- [x] `tests/test_api_noise.py` -- every matrix row except the live-only one. Also: a contract test (the app serves exactly the contract's noise routes, the batch model's fields equal the contract keys, responses match `SINGLE_KEYS`), publish-before-write order, no auto-noise, and `/schedule` reflection.
- [x] `tests/test_noise_integration.py` + CI -- on real Postgres through the real routes: single add/remove, batch replace and clear, unknown-id rollback, a mid-batch write failure rolling back, `/schedule` rows reflecting `noise`, and `lock_noise_batch` making a second locker wait.
- [x] `README.md` -- a "Noise collection (Story 5.5)" section.

**Acceptance Criteria:**
- Given registered candybars, when a consumer calls any of the three noise routes, then the registry `noise` flag changes, the next `/schedule` and `GET /candybars` responses show it, and one `updated` fact goes out per candybar that changed.
- Given a batch with any unknown id, or any failure before commit, when it is processed, then no candybar's `noise` changes and the response is problem+json.

## Implementation Notes

- This was an unattended run under the workflow harness's no-questions directive.
  - Checkpoint 1 was auto-approved ("Approve and continue") with no Open Questions. The one choice the contract leaves open is the batch's meaning, and the frozen block decides it as replace semantics (Design Notes).
  - The spec is about 1,900 tokens, over the 1,600 target. It has one goal, so it was kept whole.
  - This session has no subagent tool, so implementation and review ran inline, the same deviation recorded for 1.1 to 5.4.
- g33 preflight passed. The ecosystem route returned event naming (Bloodbank) and UI (Holocene). No Bloodbank type or schema was added: the `updated` schema already lists `noise` as a change. Holocene is not touched.
- `epic-5-context.md` was valid, so it was loaded in place of the raw planning docs. Continuity came from the done 5.4 spec.
- What was built:
  - `registry/store.py`: `lock_noise_batch`. It runs one `SELECT ... WHERE noise OR id = ANY(:ids::uuid[]) ORDER BY id FOR UPDATE`, with the ids as one array parameter, as in `fire_results`. It is exported from `registry/__init__.py`.
  - `api/app.py`: `set_noise`, the PATCH flow for the one field, behind `PUT` and `DELETE /candybars/{id}/noise`. Also `PUT /noise/batch` with `NoiseBatchRequest` (extra=forbid, `ids: list[UUID]`, at most `NOISE_BATCH_MAX` = 10,000).
  - The batch sends every fact first, in id order, and then every write. Writing nothing until all facts are out makes it atomic even without the transaction, and the transaction covers a failing write.
  - `/schedule`, `GET /candybars` and the stream needed no change.
- Decisions the frozen block leaves to the code:
  - An unknown id in the batch body is a 422 titled "Unknown candybar ids", not a 404. The route exists; the body names things that do not.
  - The 422 detail lists the unknown ids, truncated to 500 characters.
  - The batch answers with the resulting collection, ordered by `(created_at, id)`, the same order as `GET /candybars`.
  - The single routes take no body.
  - The Tasks line names `noise_batch_keys()`; it shipped as `noise_batch_fields()`, which also returns each field's kind (`ids` → list).
- Tests:
  - `tests/test_api_noise.py` has 33 tests:
    - every non-live matrix row;
    - the contract parse: the app serves exactly the contract's three noise routes, the batch body fields match the contract, and responses match `SINGLE_KEYS`;
    - bad bodies, plus the 10,000-id limit and one over it;
    - publish-before-write order for the single and batch routes, and 500s when the database is down;
    - `/schedule` and `GET /candybars` reflection, and no auto-noise for a minutely job;
    - a route-to-stream test. The real routes' facts are handed to a live `ScheduleStream` hub during the request, before the row write, and `definition.changed` still reports the new flag.
  - `tests/test_noise_integration.py` (live Postgres) has 2 tests. The first runs single add/remove, batch replace and clear, and the unknown-id refusal on the real routes, plus a second write failing mid-batch, which rolls the first back. The second checks the exact rows `lock_noise_batch` holds, and that a single-row locker waits for it. It is in the CI live-Postgres step.
- Mutation checks, each of which fails its tests:
  - no `FOR UPDATE` (live: the single-row lock did not wait);
  - a lock set without the current noise rows (live: the batch replace left `a` noise);
  - no definition overlay in the stream (unit: the route-to-stream test).
- Verification:
  - 697 unit tests pass and ruff is clean.
  - On a throwaway postgres:17-alpine container, every file in the CI live-Postgres step gave 17 passed and 2 skipped. The 2 skips are tests that need the bus.

## Spec Change Log

## Review Triage Log

The three layers ran inline because this session has no subagent tool: blind-hunter, edge-case-hunter (with the deletion and claims checks) and verification-gap. This is the same deviation as 1.1 to 5.4. The blind-hunter floor was N = min(floor(sqrt(44.1) + 1), 10) = 7.

| # | Layer | Finding | Verdict | Evidence / route |
|---|-------|---------|---------|------------------|
| 1 | verification-gap | `lock_noise_batch`'s id order, the batch-vs-batch deadlock guard, was only verified by chance. uuid7 ids minted in the same millisecond insert in random id order, so dropping `ORDER BY` failed the live test in 1 of 6 runs. | low | Patched: the live test inserts rows in descending id order, so only the `ORDER BY` can return id order. The mutation now fails 5 of 5 runs. |
| 2 | verification-gap | The batch answer's `(created_at, id)` order was only verified by chance. The rig's uuid4 ids made sorting by id pass about half the time. | low | Patched: the unit test gives ids that run opposite to registration order. Sorting by id now fails 3 of 3 runs. |
| 3 | blind-hunter | The batch holds its row locks across up to N sequential bus publishes, so a slow bus delays executor counter writes on those candybars. | low | Rejected. This is an operator-rate call, and a dead bus fails the first publish inside the publish timeout. The fix (concurrent publishes or a later lock) changes the partial-failure semantics. |
| 4 | blind-hunter | A partially published batch leaves facts on the bus for flags that never changed, so the stream shows them until a retry or refetch. | low | Rejected: the frozen Never rule. The 503 says retry, replace semantics make a retry converge, and 5.1's rename race carries the same event-first caveat. |
| 5 | blind-hunter | The unknown ids appear only in the free-text `detail`, truncated at 500 characters, with no machine-readable list. | low | Rejected: the fix adds public problem surface. Unknown ids need a stale client, since no delete path exists in `src/`. |
| 6 | blind-hunter | An unknown candybar is a 404 on the single routes and a 422 in the batch body. | low | Rejected: a recorded decision. The batch route exists; its body names missing things. |
| 7 | blind-hunter | The noise routes skip the merged-manifest validation PATCH applies, so `PATCH {noise}` can 422 on a row whose other stored fields no longer validate while `PUT .../noise` succeeds. | low | Rejected: toggling noise should not depend on unrelated fields. PATCH's whole-definition check is 5.1's frozen rule. |
| 8 | blind-hunter | The live round-trip test disposes its engine only on the success path. | low | Rejected: NullPool holds no connections, and this is test-only. |
| 9 | blind-hunter | Each fact in a batch makes the stream hub do a registry read (`_refresh_current`). | low | Rejected: operator-rate events, the precedent of 5.4 #9. |
| 10 | blind-hunter | `NOISE_BATCH_MAX` caps the whole collection under replace semantics. | low | Rejected: the registry holds tens of candybars, and the cap is documented in the README. |
| 11 | blind-hunter | There is no CLI for the noise collection. | low | Rejected: the contract is the API, as in 5.1. |
| 12 | edge-case-hunter | A request cancelled between batch publishes (role stop) leaves earlier facts out while `session_scope` rolls the rows back. | low | Rejected: the same caveat as #4, and `session_scope` rolls back on BaseException, so no row is half-applied. |
| 13 | edge-case-hunter | The 422 detail joins up to 10,000 unknown ids before slicing to 500 characters. | low | Rejected: bounded by the cap, about 360 KB once per refused request. |
| 14 | edge-case-hunter (claims) | The Tasks line names `noise_batch_keys()`; the code ships `noise_batch_fields()`. | low | Rejected: naming only, recorded in Implementation Notes. |
| 15 | edge-case-hunter (claims) | Other claims checked: no stream or schedule code change (the diff touches neither file); only register, PATCH and the noise routes write `noise` (grep of `src/`); facts before writes; one transaction; dedupe; `[]` clears; the lock set; every error status; no new event type. | — | None falsified except #14. Deletion check: only an import reflow and a CI comment line were replaced; nothing qualifies. |

Real-daemon smoke on throwaway nats:2.10.29 and postgres:17-alpine, with `candybar daemon --roles api` and `curl -N` on `/schedule/stream`:
- Two real registrations, then PUT, a no-op PUT, a batch, an unknown-id batch, and DELETE.
- Each changed flag streamed `definition.changed` with `changes: {noise}`. The no-op and the refused batch streamed nothing.
- `GET /candybars` read back the final flags.
- SIGTERM stopped the role in 308 ms.
- Everything was torn down. The live candybar container was not touched.

## Design Notes

Why replace semantics: the contract body is only `{ ids: [] }`, with no `noise` flag, and it is a `PUT`, which replaces a resource in HTTP. "Batch add/remove" therefore means sending the whole collection. The view already knows the current collection from `GET /candybars`. The lock set "ids ∪ current noise rows" is the set of rows that can change. A row outside it that turns noise during the batch is a later single call, which is a valid serial order.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all pass
- `uv run ruff check src tests` -- expected: clean
- `CANDYBAR_TEST_PG=1 DATABASE_URL=<throwaway> uv run pytest tests/test_noise_integration.py tests/test_registry_integration.py -q` -- expected: pass on throwaway postgres:17-alpine
