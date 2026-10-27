# Holocene Candybar View — DELEGATED contract

> **DELEGATED (owner: `holocene` repo).** The view itself — gmail-inbox-style live schedule UI — is implemented in the sibling `holocene` repo, not here. Per the Director Rule, this document specifies only the contract the candybar repo must expose for that view to exist. It is the handoff the holocene PM builds stories against.

## What the view needs (from the braindump)

- Two tabs: **Definitions** (all cron definitions for a day or any time window; create new crons) and **Schedule** (the main live view).
- Schedule rows: full-width rectangular rows, autoscrolling through the day.
  - "Current" row = next to fire, solid/full color.
  - Rows above current = previously invoked crons, grayed-out appearance.
  - Rows below = upcoming crons, each with a countdown to fire time.
- Noise toggle: hide crons that repeat more often than every 10 minutes; individual crons or batches can be added to a "noise" collection.
- Skip operations: skip a single cron occurrence; skip the rest of the day.

## Contract candybar must expose

Base: REST over HTTPS at `https://candybar.delo.sh` (live since 2026-10-26), plus one SSE channel for live updates. All timestamps ISO 8601 UTC. No in-app auth in v1: access is edge-only (Traefik/Cloudflare). Errors are `application/problem+json` (`{ type, title, status, detail }`). The shapes below are the contract.

### Definitions

- `GET /candybars` → list of registered Candybars: `{ id, name, description, tags[], schedule (standard cron string), timezone (IANA zone name the cron is read in), noise: bool, enabled: bool, createdAt }`.
- `POST /candybars` → register a new Candybar (definition create path used by the Definitions tab). The body may carry `timezone` (IANA zone name, default `"UTC"`, validated against the zoneinfo database); `PATCH /candybars/{id}` may change it.
- `timezone` (added 2026-10-26, Epic 7 / AD-16) is the zone the `schedule` cron string is evaluated in, as local wall time with Vixie-cron DST rules; `skip-rest-of-day`'s `date` is a date in that zone. It is additive and changes nothing else: every timestamp (`fireTime`, `createdAt`, stream events) stays ISO 8601 UTC, so the view still renders countdowns from `fireTime`.
- `enabled` (added 2026-10-26, Epic 7 / AD-21) is whether the candybar is dispatched. A disabled candybar stays listed (with `enabled: false`) but never fires, so `/schedule` has no `current` or `upcoming` row for it, and its only rows are past fires that actually ran; the stream reports a change of `enabled` as a `definition.changed`. Migration cutovers and rollbacks drive it through `POST /candybars/{id}/enabled` with `{ enabled: bool }` (outside this contract, like the executor-facing runner). Additive: the view may grey out or hide disabled definitions.
- `GET /candybars/{id}` → single definition plus registry metadata `{ totalInvoked, totalSuccessful }`.

### Schedule window

- `GET /schedule?from=<ts>&to=<ts>` → ordered rows for the window. Each row: `{ candybarId, name, schedule, fireTime, state: "past" | "current" | "upcoming", lastResult?: "success" | "fail", skipped: bool }`.
  - `state: "current"` marks the single next-to-fire row; the view renders it solid.
  - Countdowns are derived client-side from `fireTime`; the API must return exact upcoming fire times computed from each cron schedule, honoring active skips.
- `GET /schedule/next` → the current next-to-fire row (cheap poll fallback if streaming is unavailable).

### Live updates

- A streaming channel (SSE, `GET /schedule/stream`, AD-11) emitting an event whenever: an invocation starts, an invocation completes (with result), a skip is applied, a definition changes, or the "current" row advances. Each event carries enough of the row shape above for the view to patch in place without a refetch.

#### Wire format (added 2026-10-27 for the holocene handoff; implemented by Story 5.4)

- **Endpoint:** `GET /schedule/stream`, optionally `?from=<ts>&to=<ts>` (both or neither, same limits as `/schedule`). Use a browser `EventSource`. The server sends `retry: 3000` first and a `: keepalive` comment when idle.
- **Frame:** `id: <boot>-<seq>` / `event: <name>` / `data: <one-line JSON>`. Every payload has `type` (= the event name), `seq`, `key` (the patch key) and `at` (UTC). Event names are API-level only, not Bloodbank types.
- **Events:**

| `event` | Trigger | Payload (besides `type`, `seq`, `key`, `at`) |
|---|---|---|
| `ready` | every fresh connection | `cut`, `current` (key `ready`; not a patch) |
| `invocation.started` | an invocation starts | `candybarId`, `invocationId`, `trigger` (`scheduled`/`manual`/`misfire`), `row` |
| `invocation.completed` | an invocation completes | as above, plus `result` (`success`/`fail`); `row.lastResult` set |
| `skip.applied` | a skip is applied | `candybarId`, `reason`, `row`. `reason` is `occurrence` or `rest_of_day` for operator skips (the only ones whose `row.skipped` is true), or a system skip: `misfire_coalesced`, `overlap`, `executor_backlog_coalesced`, `disabled` |
| `definition.changed` | a definition is created or updated, incl. `enabled` | `candybarId`, `change` (`created`/`updated`), `definition` (the `GET /candybars` shape), `changes` (updates only: the changed visible fields); `rows` when the stream has a window |
| `current.advanced` | a tick or definition change moves the current row | `cut`, `current` (a `/schedule` row, or `null` when nothing is upcoming) |
| `reset` | the server cannot guarantee continuity | `reason` (`unresumable`, `update_failed`, `render_failed`), `cut`, `current` |

- **Patch key:** a row's identity is `<candybarId>@<fireTime>`. Events with no schedule row carry `row: null` and a non-row key: `invocation:<invocationId>` (a manual run), `skip:<factId>`, `definition:<candybarId>`; the notices use `ready`, `current` and `reset`.
- **Patch rules:** on a row event, replace the row at its key. On `current.advanced`, rows with `fireTime < cut` become `past`, the row matching `current` becomes `current`, and the rest become `upcoming`. On `definition.changed`, set `name`, `schedule` and `noise` on that candybar's rows (or replace them with `rows` when the stream has a window); a change of `enabled` adds or removes upcoming rows, so a stream without a window refetches `/schedule` on it.
- **Idempotent:** every payload is absolute state, so applying one twice changes nothing.
- **Resume:** reconnect with `Last-Event-ID` (EventSource does this itself). If the id is still in the server's 1024-event buffer, exactly the missed events replay; otherwise the client gets `reset` and must refetch `/schedule`. `seq` counts within one server boot (the `<boot>` prefix changes on restart), so after a `reset` the client forgets the seqs it applied.
- **Window:** with `from`/`to`, row events outside the window arrive as id-only frames (no `event`, no `data`); they move the resume cursor and dispatch nothing.
- **Fallback:** if streaming is unavailable, poll `GET /schedule/next`.

### Noise collection

- `PUT /candybars/{id}/noise` / `DELETE /candybars/{id}/noise` → add/remove one cron from the noise collection.
- `PUT /noise/batch` with `{ ids: [] }` → batch add/remove.
- `GET /schedule` rows include `noise: bool`; the `<10m` auto-classification is computed by the view from the cron string — candybar only stores the explicit collection.

### Skip operations

- `POST /candybars/{id}/skip` with `{ fireTime: <ts> }` → skip that single occurrence.
- `POST /candybars/{id}/skip-rest-of-day` with `{ date: <YYYY-MM-DD> }` → suppress all remaining fires that day.
- Both are reflected in subsequent `/schedule` responses (`skipped: true`, upcoming fire times recomputed) and in the stream.

## Out of scope for candybar

Everything visual or behavioral in the view: row styling, autoscroll, tab layout, the toggle interaction, and any grouping/filtering beyond the queries above.
