---
title: 'Story 1.8: Manual invoke via candybar run <id>'
type: 'feature'
created: '2026-10-26'
status: 'in-progress'
baseline_commit: 'a68179c7d4ff3f8b174f8e741ee3c807798a5fbb'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** A registered candybar only runs when its cron schedule matches a tick; an operator has no way to run it now, out of schedule.

**Approach:** `candybar run <id>` calls `POST /candybars/{id}/invoke`; the api role publishes one `bloodbank.cmd.cron.candybar.invoke` command with `trigger=manual` and `idempotency_key = candybar:<id>:manual:<command_id>` (AD-12), and the existing executor consumes it down the same execution/registry/metrics path as a scheduled fire. The CLI prints the accepted invocation and exits 0, or prints the API's problem+json and exits nonzero.

## Boundaries & Constraints

**Always:** The command is built only by `build_candybar_invoke(trigger="manual")` (no tick) and published through the same `NatsEventSink` the dispatcher uses (`Nats-Msg-Id` = event id = command_id). Each request mints a fresh `command_id` and `invocation_id`, so two runs are two invocations. The candybar must exist in the registry before anything is published. Every API failure, including unknown routes and malformed ids, answers RFC 7807 problem+json. The API returns `202 Accepted` once the command is on the bus; execution is asynchronous.

**Never:** No executor, runner, dispatcher, or registry-schema changes (manual already flows through them, and 1.6 pins it); no direct subprocess spawn from the API; no new event types or subjects; no lookup by name (the id is the registry UUID, per SPEC CAP-7); no waiting or polling for the result in the CLI (Epic 5's read surface).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Happy path | `POST /candybars/{known-id}/invoke` | 202 `{candybarId, name, invocationId, commandId, idempotencyKey, trigger:"manual"}`; one invoke command on the bus with matching ids, `tick_id` null | N/A |
| Repeat run | Same id invoked twice | Two distinct command ids, invocation ids, and idempotency keys | N/A |
| Unknown id | Well-formed UUID with no row | 404 problem+json, nothing published | CLI prints the body to stderr, exits 1 |
| Malformed id | `not-a-uuid` | 422 problem+json, nothing published | CLI prints the body to stderr, exits 1 |
| Bus down | Publish raises NATS/OS/timeout error | 503 problem+json | CLI prints the body, exits 1 |
| DB down | Lookup raises | 500 problem+json, nothing published | Existing unhandled handler |
| Unknown route | e.g. `GET /nope` | 404 problem+json (not starlette's `{"detail"}`) | HTTPException handler |
| API unreachable | Connection refused | CLI prints "unreachable", exits 1 | N/A |

</frozen-after-approval>

## Code Map

- `src/candybar/registry/store.py` -- add `get_candybar(session, candybar_id) -> Candybar | None` (session.get); export from `registry/__init__.py`. Do not touch `record_terminal_invocation`.
- `src/candybar/api/app.py` -- new route `POST /candybars/{candybar_id}/invoke` (`candybar_id: UUID` so FastAPI rejects malformed ids through the existing `RequestValidationError` handler). Reuse `problem()`, the publish-error tuple from `register`, and `session_factory`. Add a `StarletteHTTPException` handler that renders problem+json. Update the module docstring.
- `src/candybar/api/role.py`, `src/candybar/api/__init__.py` -- docstrings list the new endpoint.
- `src/candybar/cli/main.py` -- `run` command next to `register`; reuse `API_URL_ENV`/`DEFAULT_API_URL` and register's urllib error handling; `urllib.parse.quote(id, safe="")` the path segment. Update the module docstring.
- `src/candybar/executor/*`, `src/candybar/dispatcher/*` -- read-only. The executor already passes `trigger` into `TerminalInvocation`/runner env (`runner.py:113`, `worker.py:244`); `tests/test_executor.py::test_manual_trigger_passes_empty_tick` pins the unit path.
- `tests/test_api_register.py` -- reuse its `FakeSink`/monkeypatch pattern for the new API tests; `tests/test_cli_register.py` -- reuse `_FakeResponse` pattern.
- `tests/test_executor_integration.py` -- reuse `_ensure_schema`, `_make_script`, and its isolated-stream wiring for the live end-to-end test.
- `.github/workflows/ci.yml` -- executor integration step (line ~111) runs live NATS + PG tests; add the new file there.

## Tasks & Acceptance

**Execution:**
- [ ] `src/candybar/registry/store.py` + `__init__.py` -- `get_candybar` -- the API needs name + existence before publishing.
- [ ] `src/candybar/api/app.py` -- invoke route + problem+json HTTPException handler -- the AC's API half.
- [ ] `src/candybar/cli/main.py` -- `candybar run <id>` -- the AC's CLI half.
- [ ] `tests/test_api_invoke.py` -- every API matrix row, plus a check that the manual command shares subject/type/delivery with the dispatcher's `command_for`.
- [ ] `tests/test_cli_run.py` -- CLI happy path, 404/422 problem+json printed + exit 1, unreachable exit 1, id quoting.
- [ ] `tests/test_manual_invoke_integration.py` -- env-gated (NATS+PG): real API app (httpx2 ASGITransport) → isolated command stream → real `Executor` → script that writes its `CANDYBAR_TRIGGER`; asserts row `trigger=manual`, key match, counters, metrics, invoked event `trigger=manual`.
- [ ] `.github/workflows/ci.yml`, `README.md`, api docstrings -- wire the integration test; document `candybar run`.

**Acceptance Criteria:**
- Given a registered candybar id, when `candybar run <id>` runs against a live api+executor daemon, then the invocation row records `trigger=manual` with the API-returned idempotency key, the counters and `candybar_invocations_total` move by one, and the child saw `CANDYBAR_TRIGGER=manual`.
- Given an unknown id, when `candybar run <id>` runs, then it exits nonzero and prints the problem+json body.

## Design Notes

`event_id = command_id` mirrors the dispatcher, where both are the same uuid5, so `Nats-Msg-Id` dedupes a re-sent command. Response keys are camelCase, like `createdAt` in the register response (holocene view contract). The API checks the row exists before publishing, and the executor still handles a candybar deleted in between (`_fail_unknown_candybar`).

## Verification

**Commands:**
- `uv run pytest -q` -- all green.
- `uv run ruff check .` -- clean.
- `CANDYBAR_TEST_NATS=1 CANDYBAR_TEST_PG=1 uv run pytest tests/test_manual_invoke_integration.py tests/test_executor_integration.py -q` against throwaway nats:2.10.29 + postgres:17-alpine -- green.

**Manual checks (if no CLI):**
- Throwaway smoke: `candybar daemon --roles api,executor` with BLOODBANK_COMMANDS/EVENTS streams on a throwaway NATS; `candybar register` then `candybar run <id>` → row `trigger=manual`, `/metrics` shows the count; `candybar run <random-uuid>` → exit 1 with a 404 problem+json.
