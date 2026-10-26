---
title: 'Story 1.1: Project scaffold, pinned stack, and CI'
type: 'feature'
created: '2026-10-25'
status: 'done'
baseline_commit: '0a3c2ecaaa485460081c584f4ddc4e8dbb985c3e'
route: 'dispatch'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The candybar repo is greenfield — no Python package, no structural layout, no dependency pins, no lint/test CI — so no later story has a buildable, verified codebase to land on.

**Approach:** Scaffold the architecture spine's Structural Seed exactly, pin the locked stack in `pyproject.toml` (uv-managed, hatchling build), ship a minimal typer CLI so `candybar --help` runs from the installed package, wire ruff + pytest, and add a GitHub Actions workflow on `ubuntu-latest` (public repo) running lint + unit tests on push to main.

## Boundaries & Constraints

**Always:**
- Layout matches the Structural Seed verbatim: `src/candybar/{heartbeat,dispatcher,executor,registry,api,cli,events}`, `sdks/{python,node,rust}`, `deploy/`, `schemas/`.
- Exact pins from the spine stack table: fastapi 0.162.0, uvicorn 0.54.0, nats-py 2.16.0, croniter 6.2.4, psycopg 3.3.6, sqlalchemy 2.1.6, alembic 1.20.1, prometheus-client 0.26.0, pydantic 2.14.1, typer 0.27.3; `requires-python = ">=3.13"`.
- New CI workflow uses GitHub-hosted `ubuntu-latest` (repo is public; AD-15/NFR12) on push to main and PRs.
- `candybar` console script installs from the package and `--help` exits 0.
- uv is the package manager; `uv.lock` is committed; no LAN IPs, no secrets anywhere.

**Never:**
- No business logic in any role package (heartbeat/dispatcher/executor/registry/api/events are docstring-only skeletons; later stories fill them).
- No crontab, no private timers, no scheduler libraries.
- No SDK implementations — `sdks/{python,node,rust}` get README placeholders only.
- No self-hosted runner labels in the new CI workflow (public repo; the pre-existing self-hosted `code-review.yml` template workflow is untouched).
- No changes to the template-managed `code-review.yml` or managed mise.toml blocks.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| CLI help | `candybar --help` | Usage text listing the app, exit 0 | N/A |
| CLI no args | `candybar` (bare) | Help/usage shown, no stack trace | typer default (exit 0 with help callback) |
| Layout guard | any missing seed directory | layout test fails naming the missing path | N/A |
| Pin guard | a dependency version in pyproject drifts from the spine table | pins test fails naming the offender | N/A |

</frozen-after-approval>

## Code Map

- `pyproject.toml` — NEW: hatchling build, pinned deps, dev extras (pytest, ruff), console script `candybar = candybar.cli.main:app`.
- `src/candybar/__init__.py` — NEW: `__version__ = "0.1.0"`.
- `src/candybar/{heartbeat,dispatcher,executor,registry,api,events}/__init__.py` — NEW: docstring-only role packages.
- `src/candybar/cli/__init__.py`, `src/candybar/cli/main.py` — NEW: minimal typer app (version callback); the only module with real code.
- `tests/test_scaffold.py` — NEW: layout/pins/CLI smoke tests (the repo's first test file).
- `.github/workflows/ci.yml` — NEW: ruff check + pytest on `ubuntu-latest`, Python 3.13, astral-sh/setup-uv.
- `.github/workflows/code-review.yml` — EXISTS (template, self-hosted, PR-triggered): do not touch.
- `sdks/{python,node,rust}/README.md`, `deploy/README.md`, `schemas/README.md` — NEW: placeholder stubs marking later-story ownership.
- `mise.toml` — add `[tools] python = "3.14"` (host toolchain; ≥3.13 per spine) above the managed blocks; do not edit managed sections.
- `.gitignore` — append Python artifacts (`__pycache__/`, `.venv/`, `dist/`, `*.egg-info/`, `.ruff_cache/`, `.pytest_cache/`); keep existing contract lines.

## Tasks & Acceptance

**Execution:**
- [x] `pyproject.toml` -- create with pinned stack, dev deps, console script, hatchling src-layout config -- packaging foundation every story lands on.
- [x] `src/candybar/**` -- create the seven role packages + `cli/main.py` typer skeleton -- Structural Seed layout and working `--help`.
- [x] `sdks/python/README.md`, `sdks/node/README.md`, `sdks/rust/README.md`, `deploy/README.md`, `schemas/README.md` -- placeholder stubs -- seed directories exist without implying implementation.
- [x] `.gitignore` -- append Python build/cache artifacts -- runtime droppings never enter the tree.
- [x] `mise.toml` -- pin python tool -- reproducible local toolchain.
- [x] `tests/test_scaffold.py` -- layout guard, pin guard (parse pyproject, assert exact spine versions), CLI `--help` exit-0 test, import smoke -- codifies the ACs as tests.
- [x] `.github/workflows/ci.yml` -- lint + test on push to main / PRs, `ubuntu-latest`, setup-uv + Python 3.13 -- AC's CI requirement.
- [x] `uv.lock` -- `uv lock` output, committed -- reproducible installs.

**Acceptance Criteria:**
- Given the greenfield candybar repo, when the scaffold is committed, then the directory layout matches the Structural Seed exactly and `uv run pytest` proves it.
- Given the committed `pyproject.toml`, when parsed, then every spine-stack dependency is pinned to its exact locked version and `requires-python` is `>=3.13`.
- Given a push to main, when CI runs, then the `ci.yml` workflow executes ruff and pytest green on `ubuntu-latest` (validated by workflow-linting the YAML and running the same commands locally).
- Given `uv sync` has run, when `uv run candybar --help` executes, then it prints usage and exits 0.

## Implementation Notes

- Implemented directly (no subagent capability in the build session); spec was sole source of truth.
- All pinned versions resolved cleanly against PyPI (`uv lock`: 38 packages, CPython 3.14.7).
- Decision: ruff `extend-exclude = [".mise", ".agents", "_bmad", "_bmad-output"]` — those dirs are template-managed tooling; the first `ruff check .` flagged their scripts, which this story must not touch. Only repo-owned code (`src/`, `tests/`) is linted.
- Decision: bare `candybar` initially exited 2 (typer/click `no_args_is_help` default) while the matrix expects exit 0 help; fixed the code (`invoke_without_command=True` callback prints help and exits 0) rather than the expectation.
- Added root `README.md` beyond the task list (hatchling `readme` field + repo front door); additive only.
- Dev tooling (`pytest>=8.3`, `ruff>=0.8`) lives in uv `[dependency-groups].dev`; floors, not pins — the spine pins runtime deps only.
- Verified: `uv sync`, `uv run ruff check .` clean, `uv run pytest -q` 25/25 pass, `uv run candybar --help` / bare / `--version` all exit 0, `ci.yml` parses as YAML.
- Matrix audit: all 4 I/O rows covered by named tests that ran and passed (help, no-args, layout guard, pin guard).

## Spec Change Log

## Review Triage Log

Review layers ran inline in the build session (no subagent capability available — deviation from the context-free-subagent design, recorded). Patches applied and full verification re-run green.

- pyproject.toml `license = { text = "MIT" }` with no LICENSE file — low — verified: no LICENSE in repo; declaring MIT without the text is incomplete. Fix is a direct deletion, so kept → patched (field removed).
- ci.yml missing `permissions:` block — low — verified: sibling code-review.yml sets explicit permissions (adoption signal); default GITHUB_TOKEN is broader than needed → patched (`contents: read`).
- test_scaffold.py `dict(dep.split("=="))` crashes with bare ValueError on any non-`==` runtime dep — low — verified: failure would be an unhelpful ValueError, not a named assertion → patched (partition + explicit pin assertion).
- test_scaffold.py pin guard used full-dict equality — low — verified: any later non-spine dependency (e.g. httpx for the CLI in 1.8) would fail the scaffold test though the spec only requires asserting spine pins → patched (subset assertion over EXPECTED_PINS).
- Verification gap: installed console script not exercised in CI — medium — pre-verified gap finding: CliRunner imports `candybar.cli.main:app` in-process, so a broken `[project.scripts]` entry would ship green and break the story's own `candybar --help` AC → patched (CI step `uv run candybar --help`).
- `__version__` duplicated in `__init__.py` and pyproject.toml — low, rejected — harm is `--version` drifting at bumps; both fixes (importlib.metadata single-source, or adding pyproject to .mise/version-files.conf) add fragility/coupling beyond a direct correction at scaffold stage. Noted in Implementation Notes.
- Bare `candybar` prints plain Click help while `--help` prints rich help — low, rejected — cosmetic; nobody is harmed in everyday use.
- CI Python 3.13 vs mise local 3.14 skew — false — CI on 3.13 tests the supported floor deliberately; any 3.14-only syntax fails there and ruff targets py313. The skew is the guard, not the defect.
- ci.yml lacks `timeout-minutes`/`concurrency` — low, rejected — a ~1-minute lint+test job never meets the 6h default in practice.
- No `.python-version` file — false — mise.toml `[tools] python = "3.14"` pins this repo's toolchain; uv respects `requires-python` otherwise.
- Layout guard asserts presence, not absence of extra dirs — low, rejected — "matches the Structural Seed" means the seed exists; extras are not violations and an absence assertion would trip on every legitimate addition.

## Design Notes

Pinned-version guard test parses `pyproject.toml` via stdlib `tomllib` and compares against a single `EXPECTED_PINS` dict mirroring the spine stack table — the spine is the authority, the test is the tripwire. CLI uses a bare `typer.Typer()` with a version-printing callback only; subcommands (`register`, `run`) arrive with their owning stories (1.4, 1.8).

## Verification

**Commands:**
- `uv sync` -- expected: resolves and installs the pinned stack with no errors.
- `uv run candybar --help` -- expected: usage output, exit code 0.
- `uv run pytest -q` -- expected: all scaffold tests pass.
- `uv run ruff check .` -- expected: clean.
- `python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))"` -- expected: workflow YAML parses.
