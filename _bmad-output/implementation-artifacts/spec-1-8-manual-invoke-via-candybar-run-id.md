---
title: 'Story 1.8: Manual invoke via candybar run <id>'
type: 'feature'
created: '2026-10-26'
status: 'in-review'
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
- [x] `src/candybar/registry/store.py` + `__init__.py` -- `get_candybar` -- the API needs name + existence before publishing.
- [x] `src/candybar/api/app.py` -- invoke route + problem+json HTTPException handler -- the AC's API half.
- [x] `src/candybar/cli/main.py` -- `candybar run <id>` -- the AC's CLI half.
- [x] `tests/test_api_invoke.py` -- every API matrix row, plus a check that the manual command shares subject/type/delivery with the dispatcher's `command_for`.
- [x] `tests/test_cli_run.py` -- CLI happy path, 404/422 problem+json printed + exit 1, unreachable exit 1, id quoting.
- [x] `tests/test_manual_invoke_integration.py` -- env-gated (NATS+PG): real API app (httpx2 ASGITransport) → isolated command stream → real `Executor` → script that writes its `CANDYBAR_TRIGGER`; asserts row `trigger=manual`, key match, counters, metrics, invoked event `trigger=manual`.
- [x] `.github/workflows/ci.yml`, `README.md`, api docstrings -- wire the integration test; document `candybar run`.

**Acceptance Criteria:**
- Given a registered candybar id, when `candybar run <id>` runs against a live api+executor daemon, then the invocation row records `trigger=manual` with the API-returned idempotency key, the counters and `candybar_invocations_total` move by one, and the child saw `CANDYBAR_TRIGGER=manual`.
- Given an unknown id, when `candybar run <id>` runs, then it exits nonzero and prints the problem+json body.

## Implementation Notes

I implemented this inline because this session has no subagent-spawn tool (same as 1.1–1.7). The g33 preflight passed (krebs-enrollment WARN: legacy adapter, as before), and ecosystem-route pointed only at the Bloodbank naming contract. No new event types were added.

The executor needed no change, because Story 1.6 already passes `trigger` through to the runner env, the terminal row, and the lifecycle facts. This story adds the front door: `get_candybar`, `POST /candybars/{id}/invoke`, `candybar run`, and a problem+json handler for starlette `HTTPException` (unknown routes and 405s used to answer `{"detail"}`).

Verification:
- 258 unit tests pass and ruff is clean.
- 9 live integration tests pass (manual invoke, executor, registry, dispatcher) on throwaway postgres:17-alpine and nats:2.10.29 containers, never the live database.
- Throwaway smoke through the real console script, `daemon --roles api,executor` with BLOODBANK_COMMANDS (workqueue) and BLOODBANK_EVENTS streams: `candybar register`, then `candybar run <id>` twice. Each exited 0, and the child logged `trigger=manual tick=[]`. Two rows were recorded with `trigger=manual` and distinct `candybar:<id>:manual:<command_id>` keys. Counters went to 2/2 and `/metrics` showed `{result="success"} 2.0` and `{result="fail"} 0.0`. The stream held created plus 2×(invoked, completed), all `trigger=manual`, and the command stream was drained. An unknown UUID returned the 404 problem+json with exit 1, and `not-a-uuid` returned the 422 problem+json with exit 1. Torn down afterwards.

Surprise (prior-story break, fixed): `.github/workflows/ci.yml` has been unparseable YAML since Story 1.6. The step `name: Integration tests (executor: live NATS + Postgres)` contains an unquoted `: `, so GitHub rejected the workflow and every push from `99d03de` on reported failure (`gh run list`). The step name is now quoted, and `yaml.safe_load` parses the file. This also means the 1.6/1.7 integration suites never actually ran in CI.

## Spec Change Log

## Review Triage Log

I ran the review inline because no subagent-spawn tool was available (same deviation as 1.1–1.7). It applied the blind, edge-case (path tracing plus claims), and verification-gap checks to `/tmp/story-1-8-diff.u2vVKC.txt` (41.7 kB). Finding floor N = min(floor(sqrt(41.7) + 1), 10) = 7.

- Blind: the invoke 503 detail said "could not publish the invoke command", but a JetStream publish timeout is ambiguous because the server may have stored it. A user who trusts the message and retries gets a second run. Verdict: low. Patched as a direct wording correction: "the bus did not confirm the invoke command; it may still run".
- Verification gap (contract drift): the CLI tests use a hand-made 202 body, so a key rename on either side of the CLI↔API contract would pass both suites. Verdict: medium. Patched with `test_run_against_the_real_app_contract`, which sends the CLI's real request through `TestClient` to the real `create_app` and asserts that the printed ids are the published command's (202), and that a 404 body passes through with exit 1.
- Blind: `candybar run` crashes with a traceback when a 2xx body is not JSON or lacks keys (wrong `CANDYBAR_API_URL`). Verdict: low. Rejected: unlikely in everyday use, the fix adds new except branches, and `register` shares the same pattern.
- Blind: `exc.read().decode()` raises on a non-UTF-8 error body. Verdict: low. Rejected for the same reasons: the API only emits UTF-8 JSON.
- Blind: `_http_problem` stringifies a non-string `HTTPException.detail` into the title. Verdict: false. No code in the app raises `HTTPException` with a structured detail. Routing (404/405) passes the HTTP phrase.
- Blind: the lookup-then-publish window (a candybar deleted between the lookup and the publish) runs an unknown id. Verdict: false. The executor's `_fail_unknown_candybar` handles it (1.6), and the Design Notes record it.
- Blind: the CLI does not validate UUIDs locally. Verdict: false. That is deliberate: the API is the single validator, and the 422 problem+json is printed with exit 1 (tested and smoked).
- Edge: an empty id (`candybar run ""`) builds `/candybars//invoke`. Verdict: false. The route does not match, so the new handler answers a 404 problem+json and the CLI exits 1.
- Edge: an http→https redirect turns the POST into a GET. Verdict: low. Rejected: this is a deploy URL misconfiguration, it fails loudly (405 problem+json, exit 1), and `register` shares it. The `candybar.delo.sh` URL belongs to Story 1.9.
- Blind: the integration test imports private helpers from `test_executor_integration`. Verdict: low. Rejected: a rename breaks the import at collection time, which is loud, and it follows the `test_metrics.py` precedent.
- Claims check: every Intent and Tasks claim was checked against the code. No falsified claims.
- Found during implementation: the CI workflow YAML was invalid since 1.6. Verdict: high. Fixed in `0d34aee`; see Implementation Notes.

Zero bad_spec or intent_gap entries, so no loopback was needed. Zero deferred.

## Design Notes

`event_id = command_id` mirrors the dispatcher, where both are the same uuid5, so `Nats-Msg-Id` dedupes a re-sent command. Response keys are camelCase, like `createdAt` in the register response (holocene view contract). The API checks the row exists before publishing, and the executor still handles a candybar deleted in between (`_fail_unknown_candybar`).

## Verification

**Commands:**
- `uv run pytest -q` -- all green.
- `uv run ruff check .` -- clean.
- `CANDYBAR_TEST_NATS=1 CANDYBAR_TEST_PG=1 uv run pytest tests/test_manual_invoke_integration.py tests/test_executor_integration.py -q` against throwaway nats:2.10.29 + postgres:17-alpine -- green.

**Manual checks (if no CLI):**
- Throwaway smoke: `candybar daemon --roles api,executor` with BLOODBANK_COMMANDS/EVENTS streams on a throwaway NATS; `candybar register` then `candybar run <id>` → row `trigger=manual`, `/metrics` shows the count; `candybar run <random-uuid>` → exit 1 with a 404 problem+json.
