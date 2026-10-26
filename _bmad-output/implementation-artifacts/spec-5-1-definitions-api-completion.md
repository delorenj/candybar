---
title: 'Story 5.1: Definitions API completion'
type: 'feature'
created: '2026-10-26'
status: 'done'
route: 'dispatch'
baseline_commit: 'df3a77c168f69b99451526d81304c4aa60d456e8'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-5-context.md'
  - '{project-root}/_bmad-output/specs/spec-cron-framework/holocene-view-contract.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The Definitions tab of the delegated Holocene view needs to list, inspect, create and edit candybars over REST, but the API only has `POST /candybars` (Story 1.4), whose response is not the contract shape (it leaks `entrypoint`, lacks the counters). There is no list, no single read, and no update path, so `bloodbank.cron.candybar.updated` is never emitted.

**Approach:** Add `GET /candybars` and `GET /candybars/{id}` returning the `holocene-view-contract.md` shapes exactly, align `POST /candybars`'s 201 body to the single-definition shape, and add `PATCH /candybars/{id}` that publishes `candybar.updated` (changed fields only) and then applies the row change in the same request flow. A contract test parses the field lists out of the contract file itself and asserts the live responses field-for-field.

## Boundaries & Constraints

**Always:**
- List item shape is exactly `{id, name, description, tags, schedule, noise, createdAt}`; single shape is exactly that plus `{totalInvoked, totalSuccessful}`. No extra keys (field-for-field). `POST` 201 and `PATCH` 200 answer the single shape.
- `GET /candybars` returns a bare JSON array (the contract says "list"), ordered by `createdAt` then `id`.
- `createdAt` is ISO 8601 in UTC (`utc_iso`), whatever the DB session zone.
- `PATCH` body: any non-empty subset of `{name, description, tags, schedule, entrypoint, noise}`; unknown keys, read-only keys (`id`, `createdAt`, counters) and explicit `null` are 422. The merged definition is re-validated with the `CandybarManifest` rules; a new `entrypoint` must be an existing file (as on register).
- `PATCH` locks the row (`SELECT ... FOR UPDATE`), diffs against it, and when something changed publishes ONE `candybar.updated` with `changes` = exactly the changed fields → new values and `name` = the post-update name, THEN applies the change (bumping `updated_at`) in the same session. Nothing changed → 200, no event, no write.
- Errors are RFC 7807: unknown id 404, malformed id / bad body 422, name taken 409 (pre-check and unique-race), bus down 503 with no row change, DB down 500.
- All DB access goes through `registry/store.py` (sole-writer guard).

**Never:**
- No DELETE (no candybar delete path exists; out of scope). No noise collection endpoints (5.5), no schedule/skip endpoints (5.2–5.6), no CLI changes.
- No migration, no new event types or schema revisions (`updated.data.changes` is already an open map).
- No pagination or filtering on the list (homelab scale; the contract asks for none).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| List | 2 registered | 200 array of 2 list-shape items, created order | n/a |
| List empty | none | 200 `[]` | n/a |
| Get one | known id, counters 5/4 | 200 single shape, `totalInvoked=5`, `totalSuccessful=4` | n/a |
| Get unknown / malformed | random UUID / `nope` | — | 404 / 422 problem+json |
| Create | valid body | 201 single shape, counters 0, no `entrypoint`; one `created` fact | existing 1.4 errors |
| Blank tag | `tags: [""]` on POST or PATCH | — | 422 (was an unhandled 500) |
| Patch schedule | `{schedule: "0 * * * *"}` | 200 updated single shape; one `updated` fact `changes={"schedule": "0 * * * *"}` | n/a |
| Patch no-op | same values as stored | 200 unchanged; no event; no write | n/a |
| Patch empty / null / unknown key | `{}`, `{description: null}`, `{id: ...}` | — | 422, no event |
| Patch invalid merge | bad cron, bad slug, missing entrypoint file | — | 422, no event |
| Patch rename to taken name | other candybar owns it | — | 409, no event (race after publish → 409, event may be out, as on register) |
| Patch unknown id | random UUID | — | 404, no event |
| Patch bus down | sink raises | — | 503, row unchanged |

**Decisions (unattended run, spine/contract silent):** update verb is `PATCH /candybars/{id}` (partial update; the contract names no update route but the AC demands `updated` events); responses are exact contract shapes, so `entrypoint` (an executor-internal host path) is no longer echoed by any read; `PATCH` may change `noise` and `entrypoint` too (they are definition fields; 5.5 adds the collection endpoints on the same store path); the list is a bare array; renames are allowed (metrics label on `candybar_id`, not name).

</frozen-after-approval>

## Code Map

- `src/candybar/api/app.py` -- `create_app`; reuse `problem`, `utc_iso`, `BUS_ERRORS`, `RegisterRequest.to_manifest`, the entrypoint `Path.is_file` check, and the register flow's publish-then-write shape. `candybar_row_json` currently includes `entrypoint`; reshape it to the list shape and add a single-shape helper. `/candybars/{id}/invoke` and the invocation routes stay untouched.
- `src/candybar/registry/store.py` -- add `list_candybars`, `lock_candybar` (FOR UPDATE), `update_candybar(session, row, changes)` mapping `candybars_name_key` to `DuplicateNameError` (copy `insert_candybar`'s pattern). Export via `registry/__init__.py`.
- `src/candybar/registry/manifest.py` -- `CandybarManifest`: add a non-blank-tags validator (the `created`/event payload requires `NonBlank` tags; today a blank tag is an unhandled 500).
- `src/candybar/events/builders.py` -- `build_candybar_updated(candybar_id, name, changes)` exists; use as-is.
- `tests/test_api_register.py` -- fake-store rig pattern (monkeypatch app-module functions); its happy-path test asserts `entrypoint` in the body — update to the new single shape; `FakeRow` needs counters.
- `tests/test_registry_integration.py` -- `migrated_db` fixture + `test_invocation_reads_round_trip_through_the_api` show the live TestClient-over-NullPool pattern; CI step "Integration tests (live Postgres)" already runs this file.

## Tasks & Acceptance

**Execution:**
- [x] `src/candybar/registry/manifest.py` -- non-blank tags validator -- closes the 500 for blank tags on POST and PATCH.
- [x] `src/candybar/registry/store.py`, `src/candybar/registry/__init__.py` -- `list_candybars`, `lock_candybar`, `update_candybar` -- the read and update primitives.
- [x] `src/candybar/api/app.py` -- list/single shape helpers, `GET /candybars`, `GET /candybars/{id}`, `PATCH /candybars/{id}`, POST answers single shape; docstring -- the story surface.
- [x] `tests/test_api_definitions.py` -- every matrix row with the fake-store rig, plus the contract test that parses `holocene-view-contract.md` for the two field lists and asserts list/single/POST/PATCH responses key-for-key and type-for-type -- AC conformance.
- [x] `tests/test_api_register.py` -- adjust the happy path to the single shape.
- [x] `tests/test_registry_integration.py` -- live round trip: register via the real API, list, get (with counters moved by a recorded invocation), patch (row + `updated` event captured by a fake sink), rename-conflict 409, and the contract key check on real rows.
- [x] `README.md` -- definitions API section.

**Acceptance Criteria:**
- Given registered candybars, when the consumer calls `GET /candybars`, `GET /candybars/{id}` and `POST /candybars`, then the bodies match the contract field lists parsed from `holocene-view-contract.md` exactly, with ISO 8601 UTC `createdAt`.
- Given a definition change via `PATCH`, then exactly one `bloodbank.cron.candybar.updated` envelope that round-trips through `parse_envelope` is published before the row change commits, and no event is published when the request is rejected or changes nothing.
- Given any error, the response is `application/problem+json`.

## Implementation Notes

- Run under the workflow harness's no-questions directive: Checkpoint 1 was auto-approved ("Approve and continue"). The spec ran to about 1200 words, which is over the 1600-token target, and was kept whole because it has one goal. Subagents are unavailable in this session, so implementation and all three review layers ran inline, the same deviation recorded for 1.1–2.4.
- g33 preflight PASS. The route was execution/story breakdown, both owned elsewhere (Momo, Pilot), and no board writes were made. No event types or schema revisions were added: `updated.data.changes` is already an open map.
- Epic 5 had no cached context, so `epic-5-context.md` was compiled inline. No earlier Epic 5 spec exists, so there was no continuity to load.
- Implementation commit `0813916`:
  - Store: `list_candybars`, `lock_candybar` (FOR UPDATE + populate_existing), `update_candybar`, and `UPDATABLE_FIELDS`.
  - API: `GET /candybars`, `GET /candybars/{id}` and `PATCH /candybars/{id}`; POST now answers the single shape.
  - `CandybarManifest` gained a non-blank-tags validator.
  - Tests: `tests/contract_shapes.py` parses the two field lists out of the contract file. It is shared by `tests/test_api_definitions.py` (37 unit tests) and the live round trip in `tests/test_registry_integration.py`.
  - README section.
- Behaviour change for earlier stories: `POST /candybars` no longer echoes `entrypoint`, and now returns `totalInvoked`/`totalSuccessful`. The only consumer is the CLI's `register`, which reads only `name` and `id`. 1.4's happy-path test was updated to match.
- Review patch commit `b006fe3` fixes a real bug that only the live test could show. On a rename clash, `update_candybar` built its error with `changes.get("name", row.name)`, which evaluates `row.name` eagerly. The failed flush had already expired the row, so that load raised `PendingRollbackError`, and a PATCH rename race answered 500 instead of 409. It now never touches the row after a failed flush.

## Spec Change Log

## Review Triage Log

Layers ran inline: blind-hunter (diff 54.97 kB, floor N = min(floor(sqrt(54.97)+1), 10) = 8), edge-case-hunter (with the claims check), and verification-gap.

| # | Layer | Finding | Verdict | Evidence / route |
|---|-------|---------|---------|------------------|
| 1 | verification-gap | Nothing proves `lock_candybar` serializes concurrent PATCHes. Dropping `with_for_update` fails no test, so two PATCHes could diff against a stale row and both publish. | medium | Pre-verified gap; patched with a live test where a second locker must still be waiting after 0.5s and then reads the committed schedule (`b006fe3`). |
| 2 | verification-gap | `update_candybar`'s mapping of the real unique violation to `DuplicateNameError` is never exercised on Postgres; the unit rig fakes the raise. | high | Patched with a live test. It exposed #3. |
| 3 | other (found by #2) | On a rename clash, `update_candybar` evaluates `row.name` on the expired row, raising `PendingRollbackError` and returning 500 instead of 409. | high | Reproduced live (`PendingRollbackError` at store.py:389). Patched: the error no longer touches the row. The live test now passes and failed before the fix. |
| 4 | verification-gap | `UPDATABLE_FIELDS`/`update_candybar` refusing registry-owned fields (`id`, counters, `created_at`) has no test. | low | Patched with a unit test (no DB needed; it raises before touching the session). |
| 5 | blind-hunter | The PATCH holds the candybar row lock across the bus publish (up to JetStream's 5s timeout), so a concurrent executor terminal write for that candybar waits. | low | Real but bounded. The spine requires the event first, then the row, in one flow. The wait only delays a counter write and does not lose one: there is no cycle, because the executor takes the same row lock first and only then the FK key-share lock. Rejected: rare, and a fix would add complexity. |
| 6 | blind-hunter | `updated.changes` carries `entrypoint` (a host path) onto the bus, while the API no longer echoes it. | false | No harm: the bus is the internal audit trail, the spine does not forbid it, and without it an entrypoint-only PATCH would emit no `updated` fact, breaking the AC. Recorded as a decision. |
| 7 | blind-hunter | PATCH accepts a relative `entrypoint`, checked with `is_file()` against the daemon's cwd. | false | The API and executor roles run in one process with one cwd (AD-2), so the check and the spawn resolve the same path. Register behaves the same since 1.4. |
| 8 | blind-hunter | `GET /candybars` is unbounded (no pagination). | false | The intent excludes pagination (the contract asks for none; homelab scale). |
| 9 | blind-hunter | `noise: "yes"` is coerced to `true` (pydantic lax bool). | low | Same as `POST /candybars` since 1.4. Rejected: rare, and strict mode would diverge from register. |
| 10 | blind-hunter | `contract_shapes` is imported as a top-level module, which relies on pytest's default `prepend` import mode. | low | That mode is what CI and the local runs use. Rejected: no one runs this suite in importlib mode. |
| 11 | edge-case-hunter | If the row write fails after a successful publish (a non-name IntegrityError, or a commit failure), the `updated` fact is out but the row is unchanged, and the response is a 500. | false | This is the spine's accepted event-first rule, identical to register's documented behaviour. Candystore is the durable audit. Not a new path. |
| 12 | edge-case-hunter | A PATCH that renames to its own current name could 409 against itself via `name_taken`. | false | The change detection drops fields equal to the stored row before `name_taken` runs, so the self-name is never checked (`test_patch_with_stored_values_is_a_no_op`). |
| 13 | edge-case-hunter | Explicit `null` on a PATCH field could clear a NOT NULL column. | false | `UpdateRequest` fields are non-Optional with unvalidated defaults, so an explicit `null` fails validation with a 422 (`test_patch_bad_body_is_422_no_event`, `description`/`noise` null). |
| 14 | edge-case-hunter (claims) | Claims checked: the contract file is parsed rather than hardcoded; changed fields only; publish before write; no event when rejected or unchanged; POST returns the single shape; list order is `createdAt, id`; no CLI change; blank tag gives 422. | — | None falsified. |

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all pass
- `uv run ruff check src tests` -- expected: clean
- `CANDYBAR_TEST_PG=1 DATABASE_URL=<throwaway pg> uv run pytest tests/test_registry_integration.py -q` -- expected: all pass (throwaway postgres:17-alpine, never the live DB)
