---
title: 'Stories 8.1 and 8.2: Project candybar files, project-aware register/ls, candybar create TUI'
type: 'feature'
created: '2026-11-04'
status: 'review'
route: 'dispatch'
review_loop_iteration: 1
baseline_commit: 'a8532fb'
context:
  - '{project-root}/_bmad-output/planning-artifacts/epics.md'
  - '{project-root}/README.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** A candybar is registered from a lone `candybar.toml` with no link to the project it serves. Nothing says which repo owns a registered candybar, so the registry accumulates orphans, and writing a valid manifest needs the README open.

**Approach:** A repo owns its candybars mise-style: `.candybars/<name>.candybar.toml` (one each) and/or `.candybar.toml` (`[candybars.<name>]` tables). `register` and `ls` act on the current project by default, linked to registry rows through a `project:<repo>` tag. `candybar create` is a TUI (plus a flag mode) that writes a file validated through the same loader.

## Boundaries & Constraints

**Always:** the registry stays the sole inventory and the daemon its only writer (the CLI goes through the API); a legacy `candybar.toml` registers exactly as before (create-only, no tag, paths against its directory); all project files validate before any write; `enabled` is creation-time state only; textual is imported only when the TUI runs.

**Never:** a schema or API change (tags carry ownership); auto-unarchiving; pruning without `--prune`; a second place that decides what a valid candybar is.

</frozen-after-approval>

## Code Map

- `src/candybar/project.py` -- NEW: discovery (git toplevel, worktree keeps main repo name), both file formats, project path rules, tag, drift, TOML render, validated `plan_write`
- `src/candybar/cli/main.py`, `src/candybar/cli/control.py` -- `register` project sync (`--update`, `--prune`, `--dry-run`, `-n`), `ls`/`list` project view, `--global`
- `src/candybar/cli/create.py`, `src/candybar/tui/{spec,schedule,create_app}.py` -- NEW: `candybar create` flag mode and Textual wizard
- `src/candybar/steward/context.py` -- manifests found by `[candybars.<name>]` too
- `tests/test_project.py`, `tests/test_cli_project.py`, `tests/test_cli_create.py`, `tests/test_tui_create.py`, `tests/test_tui_schedule.py` -- NEW

## Implementation Notes

- A project `entrypoint` (and the wizard's "script" kind) registers as the equivalent exec runner: the API runs in a container and 422s a host entrypoint path it cannot see.
- Project paths are made absolute without following symlinks (a venv interpreter stays the venv's).
- `describe_schedule` only says "every N" when N divides the unit, cross-checked against `fire_times` gaps; anything it cannot state truthfully stays the raw cron.

## Review Triage Log

- Round 1 (two adversarial reviewers exercising the real code): 6 core/CLI and 11 TUI findings, all fixed with regression tests except the process note below.
- Process note: one reviewer run reached the live API and created then archived candybar `a` (id 01a1a6fb-99a2-736a-aa11-a8f112cfca36); it is archived and inert, but holds the name `a`. The API has no hard delete yet (CAND-51).
