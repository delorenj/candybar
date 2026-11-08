---
id: SPEC-manifest-autosync
companions: [integration-contract.md]
sources: []
---

# Candybar Manifest Autosync

## Why

Saving a Candybar manifest must update the live registry without a manual registration command or enrollment of every repository in global host configuration. Project/editor context supplies the source scope; files own definitions, not whether jobs run.

## Capabilities

- **CAP-1**
  - **intent:** A project owner can save `.candybars/*.candybar.toml` or `.candybar.toml` and have valid definitions reflected in the live registry.
  - **success:** A changed schedule or runner is visible through the API after a save, without manual registration or global project enrollment.
- **CAP-2**
  - **intent:** An owner of a legacy `candybar.toml` can save its definition and have that job updated without changing its ownership or migration state.
  - **success:** A context-selected legacy job updates on save; a foreign, ambiguously owned, or inactive-ledger job does not.
- **CAP-3**
  - **intent:** An operator can rely on autosync after project entry, worker startup, rapid saves, and recoverable failures.
  - **success:** Startup reconciliation and the latest completed save converge to API readback; invalid or failed syncs remain visibly unsynced until corrected.

## Constraints

- Global tooling installs integration once, never a repository/location inventory. Resolve sources from saved-file or bounded checkout context using the selection and lifetime rules in `integration-contract.md`.
- Native filesystem events and asynchronous editor saves only; no polling fallback, timer, second clock, or additional Candybar schedule. Exclude worktrees, symlinks, fixtures, dependencies, caches and generated trees.
- Validate the selected source set and preflight duplicate claims, identity and foreign ownership before writes. Reuse existing manifest semantics; the API is the only registry write boundary, with readback verification. Partial API failure remains visibly unsynced, not atomic success.
- Preserve existing enabled, archived and skip state; create new rows paused, never revive archived rows, and never delete or prune rows on file removal. Empty/fully removed source sets perform no API access.
- Local source context authorizes same-name untagged legacy adoption; foreign project ownership is refused. Migration sources require exact active ledger registration IDs and locking; planned, rolled-back and decommissioned entries remain excluded. No automatic migration transitions or ledger rewriting.
- Serialize overlapping reconciliations per API endpoint outside repositories, reading latest content after lock acquisition. Startup reconciles; bounded retries read latest content; failures stay visible. Provide dry-run preview before intentional production changes.

## Non-goals

- Auto-commit or Git push; UI-to-file writeback; automatic enablement; replacement of manual registration; always-on host-wide repository discovery.

## Success signal

A valid save of either format in a previously unenrolled checkout reaches API readback without manual registration. Editor saves and native checkout events share reconciliation; malformed, duplicate, foreign, inactive or removed sources make no unauthorized change. The old centrally enrolled watcher is inactive and no competing clock is introduced.
