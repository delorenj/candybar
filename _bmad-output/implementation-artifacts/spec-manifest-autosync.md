# Manifest autosync implementation acceptance

Owning component: Candybar. Contract: [SPEC.md](../specs/spec-manifest-autosync/SPEC.md). No story breakdown in this headless handoff.

## Original rollout (superseded enrollment architecture)

The evidence below records the original rollout, not current activation. The
2026-11-08 context replacement below supersedes the enrollment requirements and
managed-service state; lifecycle/validation guarantees remain unchanged.

- [x] Dry-run installation prints enrolled source files and proposed create/update/no-op/refusal actions; configured roots/exclusions and ownership/ledger matches are validated before activation.
- [x] Host event-driven worker watches configured canonical roots; worktrees, vendored trees, and caches are ignored. No timer, periodic poller, or extra Candybar schedule is installed.
- [x] Saving `.candybars/*.candybar.toml` or `.candybar.toml` validates every affected project definition before API mutation; duplicate names/claims, malformed content, and foreign owners refuse changes and produce actionable diagnostics.
- [x] Saving legacy `candybar.toml` updates only an explicitly enrolled row; enrollment authorizes same-name untagged adoption. Match exact active ledger IDs where present; exclude planned, rolled-back, and decommissioned migrations.
- [x] Schedule, timezone, description, tags, noise and runner drift reconcile through API and are verified by readback; existing enabled/skip state is unchanged, archived rows stay archived, and new rows are paused by default.
- [x] Deleting/renaming a manifest never prunes, archives, or deletes its prior registry row automatically; an in-process removed source is reported for manual resolution.
- [x] Worker startup reconciles eligible sources; atomic replace and rapid edits settle on latest complete content with bounded retries. Incomplete files, API failures, and partial apply outcomes remain visibly unsynced; a later valid save or restart converges.
- [x] Manual `candybar register` continues to work; watcher never writes UI state, git commits, or pushes.
- [x] Tests cover both formats, ownership collisions, inactive ledger statuses, failure/readback mismatch, state preservation, removal, startup, rapid-save behavior, CLI and gated installation. Live paired canaries proved API save/readback and `/usr/bin/true` terminal success; both archived after proof.

## 2026-11-08 context-first replacement (current)

- [x] `candybar sync [saved-file|project]` and default `watch` resolve local context,
  not global config. Saved project files select validated siblings; legacy files
  stay isolated. Checkout entry discovers nested definitions with exclusions and
  preserves nested project identity. Explicit `--config` remains compatibility-only.
- [x] Canonical zshyzsh integrations installed once: async LazyVim save hook,
  coalesced follow-up saves, reload-safe autocmd; shell-entry native watcher with
  checkout lifetime and exit/excluded-context cleanup. No repo enrollment.
- [x] Reconciliation serialized by per-API process flock outside repos; latest
  bytes loaded after acquisition. Empty contexts and last-file removal avoid API
  access. Existing ownership/ledger/lifecycle guarantees retained.
- [x] Old centrally enrolled `candybar-watch.service` stopped/disabled, verified
  `inactive` and `not-found` (installer symlink removed). AD-22 watchdog untouched.
- [x] Full suite: **1990 passed, 74 skipped**; final focused suite after four added
  CLI regressions: **51 passed**. Ruff passes. Actual headless Neovim and interactive
  Zsh fake-CLI tests: **2 passed**; `zsh -n candybar.zsh` passes.
- [x] Updated `scripts/prove-autosync.py` uses a fresh Git checkout with no enrollment.
  Both formats registered paused, ran `/usr/bin/true` to terminal success, accepted
  atomic-save updates, refused invalid save, recovered, and did not prune removal.
  Then stopped the native watcher: real Neovim saves through the installed Lua hook
  updated both live API definitions with pause state preserved. Temporary watcher
  stopped and both unique proof registrations archived afterward.

New shells/editors load integrations automatically. Existing shell:
`source "$ZC/candybar.zsh"`; existing LazyVim:
`:lua require("config.candybar").setup()`. Initially manifest-free checkouts need
re-entry to start native watching after a file appears; editor saves sync immediately.
Native watching is shell-scoped, not always-on host coverage.

## Verification evidence (original rollout)

- Enrollment: `deploy/host/watch.delonet.toml`, 81 files (10 project, 71 legacy); initial dry-run and one-shot readback: 79 unchanged, two inactive migrations, no writes/errors.
- `scripts/prove-autosync.py`: native startup, atomic save for both formats, creation paused despite `enabled = true`, terminal invocation success, invalid-save refusal, recovery and no prune on removal. Temporary watcher stopped; proof registrations archived.
- Independent review caught partial apply before a later ownership conflict; global ownership/identity preflight and a regression test now prevent it. Same-name untagged adoption is intentional only within explicitly enrolled sources and documented.
- Initial full-suite run overlapped live ledger reconciliation: two scheduling-policy tests returned `unknown` due valid nonblocking ledger lock contention. Clean rerun without live reconciliation: 1975 passed, 74 skipped. Full suite after ownership preflight: 1977 passed, 74 skipped. Final post-deployment suite including source-removal regression: 1978 passed, 74 skipped; focused suite: 37 passed.
- Managed `candybar-watch.service`: enabled/running, zero restarts or logged errors; startup readback 79 unchanged and two inactive migrations. Installed CLI dependency refreshed.
- CI run `42441000881`: Docker image build succeeded; existing Node SDK image smoke failed with `exec: "node": executable file not found in $PATH`. Autosync did not modify the Dockerfile; this remains a separate CI issue.
- Doctor: repository policy pass. Machine audit retains two pre-existing findings: `docker-stack-ssbnk-cleanup.timer`, `tailscale-monitor.timer`. Neither changed; no new clock added.

