---
title: 'Story 5.5: Noise collection endpoints'
type: 'feature'
created: '2026-10-26'
status: 'in-progress'
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
- [ ] `src/candybar/registry/store.py` + `__init__.py` -- `lock_noise_batch` -- the batch's lock set: one ordered locker, no deadlock between batches.
- [ ] `src/candybar/api/app.py` -- `PUT`/`DELETE /candybars/{id}/noise` and `PUT /noise/batch` per the frozen rules.
- [ ] `tests/contract_shapes.py` -- `noise_routes()` and `noise_batch_keys()` parsed from the contract.
- [ ] `tests/test_api_noise.py` -- every matrix row except the live-only one. Also: a contract test (the app serves exactly the contract's noise routes, the batch model's fields equal the contract keys, responses match `SINGLE_KEYS`), publish-before-write order, no auto-noise, and `/schedule` reflection.
- [ ] `tests/test_noise_integration.py` + CI -- on real Postgres through the real routes: single add/remove, batch replace and clear, unknown-id rollback, a mid-batch write failure rolling back, `/schedule` rows reflecting `noise`, and `lock_noise_batch` making a second locker wait.
- [ ] `README.md` -- a "Noise collection (Story 5.5)" section.

**Acceptance Criteria:**
- Given registered candybars, when a consumer calls any of the three noise routes, then the registry `noise` flag changes, the next `/schedule` and `GET /candybars` responses show it, and one `updated` fact goes out per candybar that changed.
- Given a batch with any unknown id, or any failure before commit, when it is processed, then no candybar's `noise` changes and the response is problem+json.

## Implementation Notes

## Spec Change Log

## Review Triage Log

## Design Notes

Why replace semantics: the contract body is only `{ ids: [] }`, with no `noise` flag, and it is a `PUT`, which replaces a resource in HTTP. "Batch add/remove" therefore means sending the whole collection. The view already knows the current collection from `GET /candybars`. The lock set "ids ∪ current noise rows" is the set of rows that can change. A row outside it that turns noise during the batch is a later single call, which is a valid serial order.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all pass
- `uv run ruff check src tests` -- expected: clean
- `CANDYBAR_TEST_PG=1 DATABASE_URL=<throwaway> uv run pytest tests/test_noise_integration.py tests/test_registry_integration.py -q` -- expected: pass on throwaway postgres:17-alpine
