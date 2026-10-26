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

Base: REST over HTTPS at a `*.delo.sh` hostname, plus one streaming channel for live updates. All timestamps ISO 8601 UTC. Auth/transport details are architecture-phase decisions; the shapes below are the contract.

### Definitions

- `GET /candybars` → list of registered Candybars: `{ id, name, description, tags[], schedule (standard cron string), timezone (IANA zone name the cron is read in), noise: bool, createdAt }`.
- `POST /candybars` → register a new Candybar (definition create path used by the Definitions tab). The body may carry `timezone` (IANA zone name, default `"UTC"`, validated against the zoneinfo database); `PATCH /candybars/{id}` may change it.
- `timezone` (added 2026-10-26, Epic 7 / AD-16) is the zone the `schedule` cron string is evaluated in, as local wall time with Vixie-cron DST rules; `skip-rest-of-day`'s `date` is a date in that zone. It is additive and changes nothing else: every timestamp (`fireTime`, `createdAt`, stream events) stays ISO 8601 UTC, so the view still renders countdowns from `fireTime`.
- `GET /candybars/{id}` → single definition plus registry metadata `{ totalInvoked, totalSuccessful }`.

### Schedule window

- `GET /schedule?from=<ts>&to=<ts>` → ordered rows for the window. Each row: `{ candybarId, name, schedule, fireTime, state: "past" | "current" | "upcoming", lastResult?: "success" | "fail", skipped: bool }`.
  - `state: "current"` marks the single next-to-fire row; the view renders it solid.
  - Countdowns are derived client-side from `fireTime`; the API must return exact upcoming fire times computed from each cron schedule, honoring active skips.
- `GET /schedule/next` → the current next-to-fire row (cheap poll fallback if streaming is unavailable).

### Live updates

- A streaming channel (websocket or SSE — architecture phase picks one) emitting an event whenever: an invocation starts, an invocation completes (with result), a skip is applied, a definition changes, or the "current" row advances. Each event carries enough of the row shape above for the view to patch in place without a refetch.

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
