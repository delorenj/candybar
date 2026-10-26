---
title: 'Story 4.1: Python SDK (sdks/python)'
type: 'feature'
created: '2026-10-26'
status: 'in-progress'
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
- [ ] `sdks/python/pyproject.toml` -- hatchling package `candybar-sdk` 0.1.0, `requires-python >=3.13`, `dependencies = []`.
- [ ] `sdks/python/src/candybar_sdk/__init__.py`, `_protocol.py` -- `Context.from_env`, `Result`, `candybar` decorator returning a `Candybar` with `invoke(environ=None) -> int` and `run()`; `ProtocolError`; `py.typed`.
- [ ] `sdks/python/examples/hello/{hello.py,candybar.toml}` -- executable example reporting tokens.
- [ ] `sdks/python/README.md` -- author guide.
- [ ] `tests/test_sdk_python.py` -- every matrix row via `invoke(environ=...)`; output validated by the daemon `ResultFile`; subprocess import test asserting no `nats` module loaded and zero declared deps plus an AST scan; example manifest loads.
- [ ] `tests/test_sdk_python_integration.py` -- env-gated NATS+PG: register the example → `Dispatcher.dispatch` a matching tick → real `Executor` spawns the example → row success with `token_spend` recorded, counters and `candybar_invocations_total` +1, completed event carries the metrics.
- [ ] `pyproject.toml`, `uv.lock`, `Dockerfile`, `.github/workflows/ci.yml`, `README.md` -- wiring.

**Acceptance Criteria:**
- Given the example registered via its `candybar.toml`, when a tick matching its schedule is dispatched, then the executor runs it through the SDK and the invocation row, counters, metrics and completed event record success with the SDK-reported token spend.
- Given the SDK package, when imported in a fresh interpreter, then no `nats` module is loaded and it declares no dependencies.

## Implementation Notes

## Spec Change Log

## Review Triage Log

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all pass
- `uv run ruff check .` -- expected: clean
- `CANDYBAR_TEST_NATS=1 CANDYBAR_TEST_PG=1 uv run pytest tests/test_sdk_python_integration.py` (throwaway nats + postgres) -- expected: pass
- `docker build -t candybar:sdk .` then run the example inside the image -- expected: imports `candybar_sdk`
