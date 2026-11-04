# Candybar steward and row actions: contract

Status: v1.1, 2026-11-03 (round-1 review fixes: additive). Consumers: Holocene's Candybar tab (HOLOC-24), the `candybar` CLI, any agent.
Owner of every write and of all stored data: Candybar (registry is the sole inventory; the daemon its only writer).

Decisions (owner-approved 2026-11-03):

- The runner for Enrich, Fix, Evaluate and their follow-ups is a Candybar role, `steward`, on the host
  (`candybar-steward.service`), triggered by a Bloodbank command. Holocene is only a client.
- Evaluate follow-ups run automatically: Suboptimal queues a `refactor` task, Drifted queues a `fix` task,
  Orphaned archives the candybar, Nominal does nothing. All changes are reversible. Protected candybars (tagged
  `smoke`, `canary`, `watchdog` or `policy`; named in `deploy/scheduling-policy.json`; backup jobs: tagged
  `backups`/`backup` or named `*backup*`/`*restic*`) are never archived automatically: their Orphaned follow-up is
  only `flagged` for the operator (owner triage, 2026-11-03).
- A Fix that the agent reports as repaired is proven by one manual invoke, waited on.
- Archived candybars are hidden from lists and the schedule; Holocene offers an "Archived (N)" view with Unarchive.

All timestamps ISO 8601 UTC. Errors `application/problem+json`. No in-app auth (edge only), as for every route.

## 1. Archive

- The definition shape (`GET /candybars`, `GET /candybars/{id}`, `definition.changed`, the `created`/`updated`
  facts) gains `archived: bool` (default `false`). Additive; see holocene-view-contract.md.
- `POST /candybars/{id}/archived` with `{ "archived": bool }` → 200 detail shape.
  - `true` sets `archived = true` and `enabled = false` in one write; one `bloodbank.cron.candybar.updated` fact
    with the changed fields (`{archived, enabled}`).
  - `false` clears `archived` only; the candybar stays disabled until `POST /enabled` (or `candybar resume`).
  - `POST /candybars/{id}/enabled {enabled:true}` on an archived candybar → 409 (unarchive first).
- CLI: `candybar archive <name>`, `candybar unarchive <name>`; `candybar ls` hides archived unless `--archived`.

## 2. Profile (overview, project, evaluation)

Off the view contract, like `/runner`. One profile per candybar, created lazily.

```
Profile = {
  candybarId,
  overview: Overview | null,
  templateVersion: int | null,          // the overview template version that produced it
  project: { id: string, name: string, confidence: number, method: "ledger" | "jev" | "manual" } | null,
  enrichedAt: ts | null, enrichTaskId: uuid | null,
  evaluation: Evaluation | null,
  updatedAt: ts
}

Overview (template v1+; every field required, arrays may be empty, array items never empty or blank) = {
  summary: string,          // one sentence, <= 200 chars: what it does and why it exists
  purpose: string,          // 2-4 sentences: the outcome it serves and who relies on it
  behavior: string[],       // ordered steps of what one run actually does
  inputs: string[],         // files, services, env, secrets (as op:// refs, never values) it reads
  outputs: string[],        // what it produces and where
  sideEffects: string[],    // writes, sends, deletes, restarts, external calls, costs
  dependencies: string[],   // services, CLIs, repos, credentials it needs to succeed
  failureModes: string[],   // how it fails, and what each failure looks like
  verification: string[],   // how to confirm a run worked (exact checks)
  runbook: string[],        // exact commands: run now, inspect, logs, pause
  source: { repo: string | null, paths: string[], runner: "exec" | "systemd" | "hermes" | "n8n" },
                            // runner: the steward writes the candybar's actual runner kind here
  cadenceNote: string       // whether the schedule fits the job, one sentence
}

Evaluation = {
  outcome: "Nominal" | "Suboptimal" | "Drifted" | "Orphaned",
  summary: string,          // one or two sentences, the verdict and the reason
  axes: { efficacy, cost, value, usage, alternatives }: { rating: "good" | "fair" | "poor", finding: string },
  recommendation: string,
  followUp: "none" | "refactor" | "fix" | "archive",
  followUpStatus: "queued" | "done" | "flagged" | "failed" | null,   // what became of the follow-up (null: none)
  followUpTaskId: uuid | null,                                       // the fix / refactor task, when one runs
  reportKey: string,        // S3 key under delo/daily-reports/, e.g.
                            // 2026-11-03/candybar-evaluate-restic-vault-2130-01a1a47d.md (HHMM New York, task id[:8])
  evaluatedAt: ts, taskId: uuid
}
```

- `followUpStatus`: `queued` while the follow-up task is queued or running (also when an equal task was already
  active: `followUpTaskId` names it); `done` once an archive happened or the task succeeded with `fixed` /
  `done_proven`; `failed` when the task failed or ended `not_fixed` / `done_not_proven`; `flagged` when nothing was
  done automatically and the operator decides (a protected candybar's archive, an archived candybar's fix or
  refactor, a fix that would collide with an active refactor or the reverse, a refactor that was `not_applied`).
- `GET /candybars/{id}/profile` → 200 Profile (an empty profile with nulls if none yet); 404 unknown candybar.
- `GET /profiles` → `{ profiles: Profile[] }` for every candybar that has one (one call for list views).
- `PUT /candybars/{id}/profile` with any of `{overview, templateVersion, project, enrichedAt, enrichTaskId, evaluation}`
  → 200 Profile. Merge per top-level key. Used by the steward and by a manual project override (`method: "manual"`).
  `overview` and `evaluation` are validated against the shapes above (422 otherwise).

## 3. Steward tasks

```
Task = {
  id, candybarId,
  action: "enrich" | "fix" | "evaluate" | "refactor",
  status: "queued" | "running" | "succeeded" | "failed",
  model: "automaticai/auto" | "automaticai/auto/fast",   // evaluate: auto; enrich, fix, refactor: auto/fast
  parentTaskId: uuid | null,       // a follow-up points at the evaluate that queued it
  sessionId: string | null,        // the OpenCode session (ses_...)
  outcome: string | null,          // evaluate: the Evaluation outcome; fix: "fixed" | "not_fixed";
                                   // refactor: "done_proven" | "done_not_proven" | "not_applied"; enrich: "done"
  summary: string | null,          // one or two sentences for the UI
  reportKey: string | null,        // evaluate only
  invocationId: uuid | null,       // fix and refactor: the proving run
  error: string | null,            // "cancelled by operator" for a cancelled task
  createdAt, startedAt: ts | null, finishedAt: ts | null,
  costUsd: number | null,          // the OpenCode session's cost as reported (the gateway may report 0)
  tokens: { input, output, reasoning, cache: { read, write } } | null   // the session's token usage
}
```

- `POST /candybars/{id}/steward` with `{ "action": "enrich" | "fix" | "evaluate" }` → 202 Task (status `queued`) and
  the command `bloodbank.cmd.cron.candybar.steward` `{ task_id, candybar_id, action }` (idempotency key = task id).
  - 409 with `taskId` in the problem body when the same candybar already has a queued or running task of that action,
    and for `fix` also while a `fix` or `refactor` is queued or running (one change to a job at a time).
  - 404 unknown candybar. Archived candybars may be enriched and evaluated, not fixed (409).
  - `refactor` is never accepted here; only an Evaluate follow-up creates it.
- `GET /candybars/{id}/steward/tasks?limit=N` (default 20, max 200) → `{ candybarId, tasks: Task[] }`, newest first.
- `GET /steward/tasks?active=1` → `{ tasks: Task[] }` queued and running across all candybars;
  `GET /steward/tasks?limit=N` → the newest N across all (default 100, max 500).
- `PATCH /steward/tasks/{taskId}` with any of `{status, sessionId, outcome, summary, reportKey, invocationId, error,
  startedAt, finishedAt, costUsd, tokens}` → 200 Task. Worker-only by convention.
- `POST /steward/tasks/{taskId}/cancel` → 200 Task: a queued or running task becomes `failed` with error
  `"cancelled by operator"` (its `failed` fact first); 409 when it is already finished, 404 unknown. CLI:
  `candybar steward cancel <task-id>`, which also interrupts the task's OpenCode session at once; the steward notices
  within a minute (its keepalive) and stops the run.
- Facts: `bloodbank.cron.steward_task.queued|started|completed|failed` with the Task (schemas in `schemas/cron/`;
  see Implementation notes for the entity spelling).

## 4. The steward role

`candybar daemon --roles steward`, host unit `deploy/host/candybar-steward.service` (an event-driven worker, not a
clock). It consumes the command with a durable consumer, at most 2 tasks at a time, and per task:

1. Marks the task `running` (only from `queued`), resolves the candybar's context bundle: definition, runner
   (`/runner`), the ledger entry (`deploy/migration/ledger.yaml`, `source_of_truth`), the manifest path, and for
   `systemd` the unit text (`systemctl --user cat`), its FragmentPath and the logs it appends to, for `hermes` the job
   record, for `exec` the argv files, for `n8n` the workflow export; the last 30 invocations (results, exit codes,
   error tails); the profile.
2. Picks the working directory, the job's home: the ledger repo's local checkout, unless that repo is this candybar
   repo (it only records the migration: the ledger lives here), which counts as no repo; else the runner's home: a
   hermes job `workdir`, a systemd unit's FragmentPath (symlinks resolved; it may live in another repo such as
   `~/docker`) or its ExecStart script's git root, an exec `workdir`/argv path's git root, an n8n workflow export's
   repo; a home inside this repo only when nothing else is found; else this repo.
3. Opens an OpenCode session over the v2 HTTP API on the host (`~/.local/state/opencode/service.json` gives the
   background service's URL and Basic-auth password; see Implementation notes) with `location.directory` = the working directory and the task's model,
   (enrich and evaluate: `permissions` denying `edit`, `write` and `patch`), sends the prompt rendered from
   `deploy/steward/templates/<action>.md`, and waits (enrich 15 min, fix and refactor 30 min, evaluate 20 min; then
   `interrupt` and fail). Every exit that is not a valid answer interrupts the session.
4. Reads the last fenced ```json block of the final assistant message (only that message), validates it, and
   applies it:
   - enrich: `overview` → profile (+ `templateVersion`; `source.runner` set to the real runner kind); then the
     project: the job's own checkout (the ledger repo's, or the home step 2 found; never this repo merely because the
     ledger lives here) matched to a pjangler project (`GET http://127.0.0.1:8764/v1/registry`) is
     `method: "ledger"`, confidence 1; otherwise Jev
     (`https://jev.delo.sh/typesafe/v1/systemone`, model `jev-1.13.0`, one `noul` per project, accept P >= 0.6,
     highest wins) over the name, description, tags, overview and paths; no match leaves `project` null.
   - evaluate: `evaluation` → writes
     `~/code/DeLoDocs/Reports/<YYYY-MM-DD in America/New_York>/candybar-evaluate-<name>-<HHMM>-<task id[:8]>.md`
     (HHMM in New York; one report per task, so a second evaluate the same day never overwrites the first;
     frontmatter `date, type: candybar-evaluation, candybar, outcome, generated_by: candybar-steward`) and a sibling
     `.json`, copies both with `mc cp` to `delo/daily-reports/<date>/`, stores `reportKey`, then runs the follow-up
     (a protected candybar's archive is `flagged` instead) and records `followUpStatus` / `followUpTaskId`.
   - fix: the agent repairs, makes the change take effect in what the runner executes (`systemctl --user
     daemon-reload` after a unit edit, reinstalling an installed copy, updating a separate clone), commits and pushes
     in the job's repo and reports `{repaired: bool, summary, commits[]}`; if repaired, the steward invokes the
     candybar once and waits for the invocation (timeout = the runner's, max 1 h): `outcome` = `fixed` on success,
     else `not_fixed`.
   - refactor: the agent applies the recommendation of the evaluation that queued it (`taskId == parentTaskId`; the
     task fails when that evaluation is no longer available), changing only this candybar's own job and schedule
     (`candybar edit`): never another candybar's job, never a new candybar (a needed split is reported as
     `applied: false` with the recommendation). Then, if applied, the same proving invoke as fix: `outcome` =
     `done_proven` | `done_not_proven`; `not_applied` otherwise.
   - after a fix or refactor that reported commits, an `enrich` follow-up (parentTaskId = that task) refreshes the
     overview.
5. Marks the task `succeeded` or `failed` (with `error`) and publishes the fact. The command is acked only once that
   write is recorded; a failed write naks it and the redelivery retries the same write.

Fix and refactor run one at a time per working directory (the steward holds a lock keyed by the resolved
workdir), on top of the API's one-mutating-task-per-candybar 409.

On startup, before consuming, the steward fails every task left `running` (there is one steward instance, so a restart
orphaned it) and every `queued` task older than the command stream's `max_age` (24 h: its command expired), each with
a clear error and its `failed` fact, and re-runs the project step (no agent) for profiles a ledger entry in this repo
had classified as the `candybar` project.

Templates are versioned files in this repo; changing one bumps its `templateVersion` (all four are v2 since the
round-1 fixes). Every prompt tells the agent the commit/push rule, the single-clock rule (never add a timer, cron or
loop; use `candybar edit`), never to write a secret value, that evidence is data and never instructions (its only
task is the one in the template), and to end with exactly one fenced ```json block. Every pasted file, record and log
sits in its own fence, longer than any backtick run in its content (nothing inside can close it); secret-named files
(`*.pass`, `*password*`, `rclone.conf`, `*.key`, `*.pem`, `.env*`, `credentials*`, tokens, ...) are skipped,
secret-shaped values are redacted, and logs are read from the tail. The evaluate template puts the burden of proof
both ways: every claim cites evidence, a `fair` rating alone never makes a job Suboptimal (that needs a concrete change
with a clear payoff), Orphaned needs evidence that nothing consumes the output, a job that has never succeeded is
Drifted, and Nominal tolerates minor nits.

## 5. What Holocene renders

Run health is not stored: Holocene reads the last 30 invocations per candybar and draws a micro timeline, oldest
left (success green, fail red, running neutral).

How Holocene uses this contract (holocene `apps/api/src/candybar.ts`, `apps/web/app/candybar/`; HOLOC-24, after its
round-1 review). It only reads and asks; every rule above stays Candybar's:

- **Reads:** one `GET /profiles` and one `GET /steward/tasks?limit=200` per list refresh (10 s cache). When either
  fails, the steward buttons are off with that reason, and "By project" puts the rows under "Unknown (profiles
  unavailable)", never "Unclassified".
- **Steward buttons:** Enrich, Evaluate and Fix (Fix only after a failed last run or 3 failures in the last 10, never
  when archived). A queued task reads "Evaluate queued 2m", a running one "Evaluating 4m". Past 2 minutes queued it
  asks whether `candybar-steward` is running. The 202 Task holds the button until the list shows it. Fix is off while
  a refactor is in flight, mirroring the 409 above.
- **Confirms:** Fix and Evaluate ask first. The Fix confirm names `overview.source.repo` when there is one. The
  Evaluate confirm says the verdict's follow-up runs on its own and that protected candybars are only flagged.
  Disable and Archive ask too. Enrich, noise, enable and unarchive do not.
- **409 with `taskId`:** Holocene's proxy reads the task back from `GET /candybars/{id}/steward/tasks?limit=20` and
  shows it as information ("Already running: Evaluate started 3m ago"), not as a failure. A 409 without `taskId`
  (fix on an archived candybar) is a refusal.
- **Follow-up and tasks:** "Follow-up: refactor queued / done / failed", "archive flagged: protected, not archived",
  and "flagged for you to decide" for the other flags. Without `followUpStatus` (older rows), the child task whose
  `parentTaskId` is the evaluation's `taskId` gives the status. Refactor and fix outcomes appear as words ("done,
  proven by a run", "not applied"). `costUsd` and `tokens` show as "$0.42 · 1,540 tokens"; `cache` is flattened and
  not counted.
- **Skip rest of day:** the date is computed in the candybar's own time zone, and the confirm names that date, the
  zone and the hours of the viewer's day it covers ("on 2026-11-04 (UTC), 7:00 PM tonight to 7:00 PM tomorrow EST").
  Holocene's proxy takes the confirmed date as `expectDate` and refuses (409) rather than skip another day. The
  `{date}` sent to Candybar is unchanged.
- **Archive in the schedule stream:** a `definition.changed` whose definition says `archived: true` drops that
  candybar's rows, including past runs a windowed stream still sends. Later row events for it are ignored until a
  `definition.changed` says `archived: false`.
- **Not used yet:** `POST /steward/tasks/{taskId}/cancel` (Holocene offers no Cancel) and `PUT .../profile` (no
  manual project override).

## Implementation notes (candybar, 2026-11-03)

Minimal deviations made while building it, each the smallest change that works:

- **Fact types** are `bloodbank.cron.steward_task.{queued,started,completed,failed}`, not `steward.task.*`: Bloodbank
  types are exactly four tokens (`docs/event-naming.md`), so the entity is one token, `steward_task`. The command is
  `bloodbank.cron.candybar.steward` as written. Bus payloads use the bus's snake_case (`task_id`, `candybar_id`,
  `parent_task_id`, ...) plus the candybar's `name`; the REST shapes stay camelCase as above.
  Registered in Bloodbank (entity `steward_task`, event action `queued`, command action `steward`; schemas under
  `schemas/bloodbank/cron/`) in bloodbank `38633e9`.
- **OpenCode service file:** the background service's URL and password live in
  `$XDG_STATE_HOME/opencode/service.json` (default `~/.local/state/...`), the file the `opencode` client itself
  maintains (its URL moves on restart). `~/.config/opencode/service.json` holds only a password and is a fallback for
  it. `CANDYBAR_OPENCODE_URL` / `CANDYBAR_OPENCODE_SERVICE_FILE` override.
- **Empty profile:** `GET /candybars/{id}/profile` with no stored profile answers every field null, `updatedAt` too.
- **Working directory:** the ledger repo's checkout is found under `~/code` (and one level below) by name, or as the
  git root of a path the ledger entry, the unit's FragmentPath or its ExecStart names, and is accepted only when its
  `origin` points at that repo. A ledger repo that is this candybar repo is no repo (step 2). Beyond the contract's
  list: an `n8n` runner's home is the workflows repo holding its export (`~/code/delonet-n8n-workflows/workflows`,
  `CANDYBAR_N8N_EXPORTS` overrides), a hermes job with no `workdir` falls back to its jobs file's git root, and a
  candybar defined by a manifest in this repo outside `deploy/migration/` (smoke canaries, the policy job) has its home
  here. The prompt names the ledger note so the agent knows the ledger entry only records the migration.
- **Read-only sessions:** enrich and evaluate sessions are created with OpenCode `permissions`
  (`Permission.Ruleset`, `[{action, resource, effect}]`, checked against the live 2.1.4 `/openapi.json`) denying
  `edit` (the edit/write/patch tools' permission), `write` and `patch`; session rules are evaluated after the agent's,
  and `GET /api/session/{id}` echoes them. `shell` is not denied (the evidence needs read-only commands), so a shell
  write is not blocked by the server: the template's read-only rule covers that.
- **Stuck tasks:** a redelivery of a task this process is still running is left to the live run; the move to
  `running` is refused unless the task is `queued`; a terminal write that keeps failing is retried on redelivery up to
  30 deliveries, after which the startup reaper settles it.
- **Session spend:** `costUsd` / `tokens` come from `GET /api/session/{id}` at the end of the session (cumulative
  over the repair turn), on success and failure alike; they are REST-only (not on the bus facts).
- **Answers:** an answer that does not validate gets one repair turn in the same session before the task fails.
- **Enrich project:** a stored `method: "manual"` project is never replaced; a Jev or registry outage leaves the stored
  project unchanged (the overview is still written) and says so in the task summary.
- **Evaluate:** the follow-up is derived from the outcome (the agent's `followUp` is ignored). A failed report upload
  fails the task before anything is stored.
- **Tasks:** `PATCH /steward/tasks/{id}` refuses an impossible status move (409: a terminal task never changes, a
  queued one cannot succeed without running) and stamps `startedAt` / `finishedAt` when the caller does not. A
  redelivered command whose task is already `running` (the steward died mid-task) fails it as abandoned instead of
  running it twice. A refactor request is a 422 (the body only accepts enrich, fix, evaluate).
