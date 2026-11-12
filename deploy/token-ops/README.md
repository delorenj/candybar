# Token-ops vault delivery — FLUME-82

## Observed / applied / pending

- **Observed:** API registration `token-usage-report` (`01a1793b-f875-73c7-b1cd-4df0d7f28cbd`) still fires `53 5 * * *`; `token-optimization` (`01a1793c-0b29-7ef2-ac1d-aa1bc158ce1c`) still fires `23 6 * * *`. Both use the maintained Hermes runner, America/New_York. API readbacks are in Peter's `reports/token-ops-delivery/registrations-{before,after}.json`.
- **Applied:** only Peter Griffin's two Hermes jobs were edited via `hermes -p peter-griffin cron edit`: routine delivery is `local`; `failure_deliver=telegram:7564050286` and `scheduler=external` are unchanged. Prompts publish finalized artifacts with this adapter and preserve urgent ticket-backed alerts. No restart, deploy, push, other profile change or new clock.
- **Executed:** `python deploy/migration/token_ops_vault.py publish --date 2026-11-10` and `--date 2026-11-11` wrote real reports under the configured vault. Repeats returned `changed=false`; the November 10 publication verified 65 source hashes. Original artifacts are not rewritten. November 11 includes available Q3 report and Q1 action, not fabricated missing checkpoints.
- **Pending:** restore the previously requested four-checkpoint schedule in Candybar and replace the direct Hermes runner with the verified-completion wrapper. Do not call this cadence installed. The proposed manifests have `.pending` suffixes so discovery cannot register them accidentally. Existing authoritative migration manifests and ledger are unchanged until a coordinated update is possible.

## Why registration is blocked

`uv run candybar doctor --repo . --live --json` exits **2**: repository policy passes, but the machine result is **unknown** for `systemd:user:newapi-allowances.timer` because the canonical migration directory lock is held. A readonly `/proc` / `lslocks` check found `candybar watch /home/delorenj/code/33GOD --if-present` holding the migration directory and sync lock, with other sync processes queued (PID is transient). A bounded `candybar sync ... --dry-run --json` timed out. Do not kill the watcher, bypass the lock, rewrite the ledger concurrently, or weaken doctor to finish this change. Route to the Candybar/infra owner on FLUME-82.

This is **not** an independent Hermes ticker: live identity inventory shows both Peter jobs enabled and `scheduler=external`. The AD-22 watchdog and unrelated approved clocks remain untouched. Doctor does not establish a clean whole host until the unknown is resolved.

## Adapter

`deploy/migration/token_ops_vault.py` is deterministic execution, not a plugin clock:

```text
publish --date YYYY-MM-DD   # publish existing finalized evidence, no inference
run report|action          # run the existing external Hermes job once, then verify/publish
notify-failure             # explicit Telegram failure hook, no inference
```

The configured vault comes from `~/.config/delonet-daily-report/report.json`, `distribution.vault`; `by-date` currently resolves to `/home/delorenj/code/DeLoDocs/Daily Reports/<covered-date>/token-ops.md`. Only that owned note is replaced; `dev-journal.md` and its daily delivery are unchanged. The publisher validates report completion, calendar bounds and all manifest hashes, includes finalized action receipts, shows missing quarters explicitly, and preserves original evidence timestamps. It reads back the note hash, records publication receipts under Peter's `reports/token-ops-delivery`, and retains earlier note generations on content changes. Exact repeats do not rewrite the note or receipt.

The `run` path uses the same `hermes cron run --wait --timeout 3600` integration as the maintained Hermes runner. It preserves external pins, continuity, join semantics and native failure targets. It requires matching evidence after the terminal attempt, and writes input+output tokens to Candybar's AD-8 `metrics.token_spend`. `on_failure` pages for execution/publication failure. Urgent incident alerts remain explicit, ticket-backed Hermes sends; quiet routine delivery never authorizes suppressing them. The notifier was tested with offline transports; no fake test alert was sent to the operator.

## Coordinated cutover after owner resolves the lock

1. Re-read both jobs, registrations, runners, current manifests and ledger. Preserve unrelated edits. Confirm no attempt is active and avoid the next firing guard; do not rerun an optimization job as a publication test.
2. Validate `proposed/*.pending` with `CandybarManifest.model_validate(tomllib.loads(...)["candybar"])`. Copy the definitions into the existing `deploy/migration/manifests/token-{usage-report,optimization}/candybar.toml` under Candybar's canonical migration directory lock.
3. Under the same lock, change only those two ledger entries' `candybar` definitions to the identical new cron/description/exec runner, append an honest history note, and save using the canonical ledger loader/writer. Keep native historical schedule and original cutover/verification evidence; do not relabel old once-daily executions as proof of new cadence. Record the new source commit. Commit relevant manifests and ledger before API update. Do not push without approval.
4. Sync the exact legacy manifests with Candybar's supported `sync <absolute-manifest>` path (after releasing your write lock), or use the documented API PATCH for those exact IDs under the serialized reconciliation path. Preserve enabled/archived state. Read back both registration and `/runner` endpoints, confirming schedule, timezone, argv, timeout and failure hook. If any step fails, restore coherent prior definitions rather than leaving the manifest/ledger/API divergent.
5. Leave both Hermes jobs external and routine-local; do not create a plugin ticker, systemd timer or background agent daemon. Confirm the informational job schedules remain `7 0,6,12,18 * * *` / `37 0,6,12,18 * * *`.
6. A live side-effecting action job is not a canary. Inspect the next actual scheduled invocation and its terminal result, measured token spend and matching vault publication receipt. No polling loop or extra schedule is needed. A dispatch receipt alone does not prove execution or vault publication.
7. Re-run `uv run candybar doctor --repo . --live --json` from this checkout; retain any finding. Verify competing native clocks are off, retain execution evidence, and only then claim the cadence changed.

## Tests

```bash
uv run pytest -q tests/test_token_ops_vault.py tests/test_hermes_runner.py
uv run ruff check deploy/migration/token_ops_vault.py tests/test_token_ops_vault.py
```

Executed: **56 passed, 2 skipped** (19 publisher/wrapper checks; maintained runner suite included). Offline fixtures test idempotence, covered-date midnight handling, DST 5/7-hour quarters, source/journal preservation, corruption/path escape refusal, missing/unfinished artifacts, identity checks, external/local execution guard, token accounting and confirmed failure transport. Fixture/mock responses are not live execution proof; the separate real vault publications above are.
