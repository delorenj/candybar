---
id: SPEC-manifest-autosync
companions: []
sources: []
---

# Candybar Manifest Autosync

## Why

Saving a Candybar manifest currently leaves the live registry stale until someone runs `candybar register` (and legacy `candybar.toml` registration is create-only). Operators need a file save to update the scheduler without taking ownership of foreign jobs or changing whether a job runs.

## Capabilities

- **CAP-1**
  - **intent:** A project owner can save `.candybars/*.candybar.toml` or `.candybar.toml` and have valid definitions reflected in the live registry.
  - **success:** A changed schedule or runner is visible through the API after a save, without manual registration.
- **CAP-2**
  - **intent:** An owner of a legacy `candybar.toml` can save its definition and have that job updated without changing its ownership or migration state.
  - **success:** An explicitly owned legacy job updates on save; a foreign, ambiguously owned, or inactive-ledger job does not.
- **CAP-3**
  - **intent:** An operator can rely on autosync after worker startup, rapid saves, and recoverable failures.
  - **success:** Startup reconciliation and the latest completed save converge to API readback; invalid or failed syncs remain visibly unsynced until corrected.

## Constraints

- Run a host-native filesystem-event worker over configured canonical roots; exclude worktrees, vendored directories and caches. No polling loop or second clock.
- Validate the whole affected source set and refuse duplicate source claims or foreign ownership before registry writes. Use the existing manifest semantics and API as the only registry writer; verify writes by API readback. A mid-apply failure must remain visibly unsynced, not claim atomic success.
- Preserve existing `enabled` and skip state; create new rows paused by default, never revive archived rows, and never delete or prune a row because its file was removed.
- For legacy manifests, explicit file/root enrollment authorizes same-name adoption of untagged registrations; foreign project ownership is always refused. Match active ledger entries by exact registration ID where present; exclude planned, rolled-back and decommissioned entries. Autosync must not drive migration cutover, rollback or decommission.
- On startup reconcile and retry transient/incomplete saves only within a bound against the latest content. Log failures and outstanding drift visibly. Installation requires dry-run inventory and proposed-change proof before live production mutations.

## Non-goals

- Auto-commit or push manifests; UI-to-file writeback; automatic job enablement; replacing the manual registration workflow.

## Success signal

A valid edit to either manifest format updates its owned live definition and matches API readback without an explicit register call. Invalid, duplicate, foreign, inactive, or removed sources make no unauthorized registry change, and any unsynced state is visible.
