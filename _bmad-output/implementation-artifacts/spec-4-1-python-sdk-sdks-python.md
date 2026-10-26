---
title: 'Story 4.1: Python SDK (sdks/python)'
type: 'feature'
created: '2026-10-26'
status: 'done'
baseline_commit: 'c56b127e7313095b2c1705da3d62a7fc6365fc7c'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-4-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** A python candybar author today must hand-roll the AD-8 process protocol (read five `CANDYBAR_*` env vars, write the result JSON, map exceptions, report token spend); every hand-rolled copy is a chance to drift from what the executor reads.

**Approach:** Ship `sdks/python` as the pip-installable, zero-dependency package `candybar-sdk` (import `candybar_sdk`). The author decorates a function with `@candybar`, calls `main.run()` under `if __name__ == "__main__"`, and registers the file with an ordinary `candybar.toml`. The SDK parses the env into a `Context`, invokes the function (sync or async), and atomically writes the AD-8 result JSON; token spend and cost accumulate on the context, so they are reported on failure too.

## Boundaries & Constraints

**Always:** Zero runtime dependencies; no `nats` import anywhere in the SDK. Result JSON is exactly `{"status": "success"|"fail", "message"?: str, "metrics": {"token_spend": int>=0, "cost_usd"?: float>=0}}` — `message` and `cost_usd` omitted when unset, no other keys inside `metrics` (the executor's `InvocationMetrics` forbids extras). Message capped at 500 chars. Exit code 0 on success, 1 on fail, 2 on protocol error. The result file is written atomically (temp file in the same dir + `os.replace`). Every SDK-written file must validate against the daemon's own `ResultFile` model.

**Never:** No daemon/executor/registry/API changes beyond packaging the SDK into the image; no SDK-side registration over the network; no bus or HTTP client; no strict trigger allow-list (an unknown trigger string is passed through — reader generosity); no pypi publishing (deferred).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Bare success | handler returns `None` | `{"status":"success","metrics":{"token_spend":0}}`, exit 0 | N/A |
| Success + message | handler returns `"done 12"` | `message:"done 12"`, exit 0 | N/A |
| Explicit result | returns `Result.fail("quota")` | `status:"fail"`, `message:"quota"`, exit 1 | N/A |
| Metrics | `ctx.add_tokens(100)`, `ctx.add_tokens(23)`, `ctx.add_cost(0.25)` | `token_spend:123`, `cost_usd:0.25` | N/A |
| Unhandled exception | handler raises `RuntimeError("boom")` | `status:"fail"`, `message:"RuntimeError: boom"`, accrued metrics kept, traceback on stderr, exit 1 | caught |
| Async handler | `async def` returning a message | awaited via `asyncio.run`, same mapping | N/A |
| `sys.exit` in handler | `SystemExit(0)` / `SystemExit(3)` | success exit 0 / fail `"exit 3"` exit 1 | caught |
| Bad return type | handler returns `42` | `status:"fail"`, message names the type, exit 1 | caught |
| Bad metric | `ctx.add_tokens(-1)` / `True` / `add_cost(nan)` | `ValueError`/`TypeError` → fail path | caught |
| Manual trigger | `CANDYBAR_TICK_ID=""` | `ctx.tick_id is None`, `ctx.trigger == "manual"` | N/A |
| Not under executor | `CANDYBAR_RESULT_FILE` unset | stderr explains missing vars, no file, exit 2 | N/A |
| Partial env | result file set, `CANDYBAR_ID` unset | fail result written naming the missing vars, exit 2 | N/A |
| Long message | 2000-char exception text | message truncated to 500 | N/A |

</frozen-after-approval>

## Code Map

- `src/candybar/executor/runner.py:46` `ResultFile` / `src/candybar/events/envelope.py:153` `InvocationMetrics` -- the reader contract the SDK's output must satisfy; read-only. Runner execs `entrypoint` with no args, cwd = its dir, env = daemon env + AD-8 vars (`child_env`, tick id empty for manual).
- `src/candybar/executor/worker.py:66` `resolve_outcome` -- nonzero exit → failed but result message/metrics honored; read-only.
- `src/candybar/registry/manifest.py` `load_manifest` -- the example's `candybar.toml` must load through it (relative entrypoint resolved).
- `tests/test_executor_integration.py` `_ensure_schema`, `_make_script`; `tests/test_manual_invoke_integration.py` RerouteSink/isolated-stream pattern; `src/candybar/dispatcher/worker.py` `Dispatcher`, `command_for` -- reuse for the scheduled e2e test.
- `pyproject.toml` -- add `candybar-sdk` to the dev group with a `[tool.uv.sources]` editable path source; daemon runtime deps unchanged.
- `Dockerfile` -- install `sdks/python` into `/app/.venv` so SDK candybars under the bind-mounted jobs dir import it (PATH already has the venv first).
- `.github/workflows/ci.yml` -- add the SDK e2e test to the executor integration step.

## Tasks & Acceptance

**Execution:**
- [x] `sdks/python/pyproject.toml` -- hatchling package `candybar-sdk` 0.1.0, `requires-python >=3.13`, `dependencies = []`.
- [x] `sdks/python/src/candybar_sdk/__init__.py`, `_protocol.py` -- `Context.from_env`, `Result`, `candybar` decorator returning a `Candybar` with `invoke(environ=None) -> int` and `run()`; `ProtocolError`; `py.typed`.
- [x] `sdks/python/examples/hello/{hello.py,candybar.toml}` -- executable example reporting tokens.
- [x] `sdks/python/README.md` -- author guide.
- [x] `tests/test_sdk_python.py` -- every matrix row via `invoke(environ=...)`; output validated by the daemon `ResultFile`; subprocess import test asserting no `nats` module loaded and zero declared deps plus an AST scan; example manifest loads.
- [x] `tests/test_sdk_python_integration.py` -- env-gated NATS+PG: register the example → `Dispatcher.dispatch` a matching tick → real `Executor` spawns the example → row success with `token_spend` recorded, counters and `candybar_invocations_total` +1, completed event carries the metrics.
- [x] `pyproject.toml`, `uv.lock`, `Dockerfile`, `.github/workflows/ci.yml`, `README.md` -- wiring.

**Acceptance Criteria:**
- Given the example registered via its `candybar.toml`, when a tick matching its schedule is dispatched, then the executor runs it through the SDK and the invocation row, counters, metrics and completed event record success with the SDK-reported token spend.
- Given the SDK package, when imported in a fresh interpreter, then no `nats` module is loaded and it declares no dependencies.

## Implementation Notes

Implemented inline: this session has no subagent-spawn tool (same as 1.1-1.9). g33 preflight PASS; ecosystem-route returned only the analysis route plus the Bloodbank naming contract. No new event types.

Decisions where the spine is silent:
- Import name is `candybar_sdk` (the daemon already owns `candybar`); dist name `candybar-sdk` per the epic.
- Metrics live on the `Context` (`add_tokens`/`add_cost`), not on the return value, so tokens spent before an exception are still reported.
- Exit codes 0/1/2 (success/fail/protocol error). The executor is exit-code primary but honors the result file's message and metrics on nonzero exit, so fail keeps its detail.
- Unknown `CANDYBAR_TRIGGER` values pass through; `CANDYBAR_TICK_ID` empty or absent means `None`.
- `token_spend` totals are capped at BIGINT max in the SDK, because a larger value would fail the executor's row write on every redelivery.
- The root project takes the SDK as an editable dev-group path dependency (`[tool.uv.sources]`). Daemon runtime deps are unchanged. The image installs `sdks/python` into `/app/.venv` with `--no-deps`, so jobs under the jobs mount can import it.
- No shared cross-SDK conformance fixture: every SDK-written file is validated by the daemon's own `ResultFile` reader. 4.2 and 4.3 can check against the same reader.

Verification:
- On a clean `git archive` of HEAD plus the review patch: 318 unit tests pass and ruff is clean. The shared working tree carried other agents' in-flight edits (Epic 3 metering, node/rust SDKs), so verification ran on the clean export.
- Live integration tests (SDK e2e, executor, manual invoke) passed on throwaway postgres:17-alpine and nats:2.10.29 containers.
- The image built; the example ran inside it under Python 3.13 and imported `candybar_sdk` from the venv.
- Real-daemon smoke, all four roles from the clean export:
  - `candybar register` of a `* * * * *` copy of the example.
  - The real heartbeat published tick:29883249, the dispatcher dispatched it, and the executor ran hello.py (success, 40ms).
  - Rows recorded scheduled and manual success with token_spend 142 and cost_usd 0.0007. Both completed events carried `{token_spend: 142, cost_usd: 0.0007}`, and `/metrics` counted them on the success series.
- The first smoke attempt launched `.venv/bin/candybar daemon` directly, without the venv on PATH. `#!/usr/bin/env python3` then found the host python, so the job failed with `ModuleNotFoundError` in its stderr tail. That proved the failure path end to end and became the README note.
- Everything was torn down afterwards.

Process note: a broad `pkill -f "candybar daemon"` was attempted to stop the smoke daemon. It only reached this session's daemon; the live container (uid 10001) was untouched. Later stops targeted PIDs.

## Spec Change Log

## Review Triage Log

The three layers (blind-hunter floor N = min(floor(sqrt(44.4)+1),10) = 7, edge-case-hunter, verification-gap) ran inline because no subagent-spawn capability exists in this session, the same deviation as 1.1-1.9.

| # | Layer | Finding | Verdict | Route / evidence |
|---|-------|---------|---------|------------------|
| 1 | blind | A lone surrogate in a message (e.g. an OSError naming a non-UTF-8 filename) makes `.encode("utf-8")` raise, so the run exits 2 with no result file and message and metrics are lost | medium | patch: `_clean_message` replaces surrogates with U+FFFD; `test_unstorable_characters_are_replaced` |
| 2 | blind | NUL in a fail message reaches the executor's Postgres text column, the row write fails, and the invoke is nak'd and re-executed | medium | patch (SDK side): NUL becomes U+FFFD in the same scrub; executor-side twin deferred (#11) |
| 3 | blind | The example's `/usr/bin/env python3` depends on the daemon's PATH having the SDK; `.venv/bin/candybar daemon` fails | low | patch (docs): reproduced in smoke; README Install section explains it and the fix |
| 4 | verification-gap | The no-NATS probe's sanity check only asserted `find_spec("nats")`, not that the detector fires | low | patch: counter-probe imports nats first and asserts the detector sees it |
| 5 | blind | `invoke()` called from inside a running event loop makes `asyncio.run` raise | low | reject: the process entry never runs inside a loop, and the failure is loud (fail result naming RuntimeError) |
| 6 | blind | The sum of finite costs can overflow to inf, so `allow_nan=False` raises and exits 2 | low | reject: needs more than 1e308 USD; the failure is loud |
| 7 | blind | `dataclasses.replace(ctx)` shares the meter | low | reject: not a documented API and nothing calls it |
| 8 | blind | `sys.exit(True)` reports "exit True" | low | reject: cosmetic, still a correct fail |
| 9 | edge | `mkstemp` makes the result file 0600 | false | the child inherits the daemon's uid; the executor read it in the container and host smokes |
| 10 | edge | A missing result-file directory leaves no file | false | handled: exit 2 plus a stderr message, pinned by `test_unwritable_result_file_is_protocol_error` |
| 11 | edge | A bare executable's stderr NUL hits the same Postgres write-fail and re-execute loop | medium | defer: pre-existing in 1.6; deferred-work.md entry |
| 12 | verification-gap | The CI e2e drives `Dispatcher.dispatch` with a built tick, not a real heartbeat | low | reject: the real-daemon smoke closed it (tick:29883249), and the heartbeat is CI-proven since 1.3 |
| 13 | edge (claims) | Spec claims about exit codes, the ResultFile contract and image shipping | false | every claim verified by tests and the smoke; nothing falsified |

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all pass
- `uv run ruff check .` -- expected: clean
- `CANDYBAR_TEST_NATS=1 CANDYBAR_TEST_PG=1 uv run pytest tests/test_sdk_python_integration.py` (throwaway nats + postgres) -- expected: pass
- `docker build -t candybar:sdk .` then run the example inside the image -- expected: imports `candybar_sdk`
