# Epic 5 Context: Schedule Read/Control API (CAP-8)

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Give a schedule consumer — first of all the delegated Holocene schedule view — everything it needs from candybar through one HTTP API: definitions CRUD, a schedule window of past/current/upcoming rows with exact skip-aware fire times, a cheap next-fire poll, a live SSE stream, the noise collection, and skip operations. The API implements `_bmad-output/specs/spec-cron-framework/holocene-view-contract.md` verbatim; that file is the only handoff to the holocene repo, so conformance (not invention) is the job.

## Stories

- Story 5.1: Definitions API completion
- Story 5.2: Schedule window endpoint with exact skip-aware fire times
- Story 5.3: Next-fire poll fallback
- Story 5.4: Live updates over SSE
- Story 5.5: Noise collection endpoints
- Story 5.6: Skip operations API

## Requirements & Constraints

- Contract shapes are binding field-for-field: definitions `{ id, name, description, tags[], schedule, noise, createdAt }`, single definition adds `{ totalInvoked, totalSuccessful }`; schedule rows `{ candybarId, name, schedule, fireTime, state: "past"|"current"|"upcoming", lastResult?, skipped, noise }`. Every story carries a contract-conformance test against the contract file.
- Exactly one schedule row is `current` (the next to fire); upcoming fire times must be exact and honor active skips — skipped fires appear with `skipped: true`, never silently dropped. Past rows carry `lastResult` from the invocations table.
- `GET /schedule/next` answers from computed state with a bounded query under a stated latency budget.
- SSE stream emits on invocation start, invocation completion (with result), skip applied, definition change, and current-row advance; payloads carry enough row shape to patch in place; `Last-Event-ID` reconnection resumes without duplicate application.
- Noise: candybar stores only the explicit collection (no `<10m` auto-classification); batch calls are atomic per request.
- Skips: `{ fireTime }` / `{ date }` bodies write skip rows the dispatcher honors; a nonexistent fireTime or id is a 404.
- All timestamps ISO 8601 UTC; all errors RFC 7807 problem+json. Schedules are standard 5-field cron, evaluated in UTC only.
- Edge-only auth in v1 (Cloudflare Access / LAN); no in-app authz. Secrets only via env / `op://` refs; hostnames on `*.delo.sh`.

## Technical Decisions

- FastAPI REST + SSE in the `api` role of the single daemon, served at `https://candybar.delo.sh`; live updates are SSE at `GET /schedule/stream` with `/schedule/next` as the poll fallback.
- Upcoming fire times use the same croniter UTC evaluation the dispatcher uses, with registry skips applied — share the dispatcher's schedule helpers rather than reimplementing them.
- The daemon is the sole registry writer; DB access goes only through the registry store module (a guard test forbids DB imports elsewhere).
- Every lifecycle state change emits its `bloodbank.cron.candybar.*` fact as one logical unit with the registry mutation, event first then the row in the same flow (`created` on register, `updated` on any definition change, `skipped` for skips). The `updated` payload's `changes` maps each changed field to its new value.
- Candybar names are unique lowercase slugs; registry ids are UUIDv7.

## Cross-Story Dependencies

- Epic 2 owns skip mechanics (occurrence and rest-of-day skip rows, dispatcher honor, skip-aware fire helpers); 5.2 and 5.6 build on them.
- 5.2's row computation is reused by 5.3 (next row) and 5.4 (stream payloads); 5.4's stream is the channel 5.5/5.6 changes appear on.
- When Epic 5 is done, the holocene handoff is filed with the holocene PM referencing the contract and `https://candybar.delo.sh` (Epic 6 is contract-only).
