# Candybar steward and row actions: contract

Status: v1, 2026-11-03. Consumers: Holocene's Candybar tab (HOLOC-24), the `candybar` CLI, any agent.
Owner of every write and of all stored data: Candybar (registry is the sole inventory; the daemon its only writer).

Decisions (owner-approved 2026-11-03):

- The runner for Enrich, Fix, Evaluate and their follow-ups is a Candybar role, `steward`, on the host
  (`candybar-steward.service`), triggered by a Bloodbank command. Holocene is only a client.
- Evaluate follow-ups run automatically: Suboptimal queues a `refactor` task, Drifted queues a `fix` task,
  Orphaned archives the candybar, Nominal does nothing. All changes are reversible.
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

Overview (template v1; every field required, arrays may be empty) = {
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
  cadenceNote: string       // whether the schedule fits the job, one sentence
}

Evaluation = {
  outcome: "Nominal" | "Suboptimal" | "Drifted" | "Orphaned",
  summary: string,          // one or two sentences, the verdict and the reason
  axes: { efficacy, cost, value, usage, alternatives }: { rating: "good" | "fair" | "poor", finding: string },
  recommendation: string,
  followUp: "none" | "refactor" | "fix" | "archive",
  reportKey: string,        // S3 key under delo/daily-reports/, e.g. 2026-11-03/candybar-evaluate-restic-vault.md
  evaluatedAt: ts, taskId: uuid
}
```

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
  outcome: string | null,          // evaluate: the Evaluation outcome; fix: "fixed" | "not_fixed"; enrich/refactor: "done"
  summary: string | null,          // one or two sentences for the UI
  reportKey: string | null,        // evaluate only
  invocationId: uuid | null,       // fix only: the proving run
  error: string | null,
  createdAt, startedAt: ts | null, finishedAt: ts | null
}
```

- `POST /candybars/{id}/steward` with `{ "action": "enrich" | "fix" | "evaluate" }` → 202 Task (status `queued`) and
  the command `bloodbank.cmd.cron.candybar.steward` `{ task_id, candybar_id, action }` (idempotency key = task id).
  - 409 with `taskId` in the problem body when the same candybar already has a queued or running task of that action.
  - 404 unknown candybar. Archived candybars may be enriched and evaluated, not fixed (409).
  - `refactor` is never accepted here; only an Evaluate follow-up creates it.
- `GET /candybars/{id}/steward/tasks?limit=N` (default 20, max 200) → `{ candybarId, tasks: Task[] }`, newest first.
- `GET /steward/tasks?active=1` → `{ tasks: Task[] }` queued and running across all candybars;
  `GET /steward/tasks?limit=N` → the newest N across all (default 100, max 500).
- `PATCH /steward/tasks/{taskId}` with any of `{status, sessionId, outcome, summary, reportKey, invocationId, error,
  startedAt, finishedAt}` → 200 Task. Worker-only by convention.
- Facts: `bloodbank.cron.steward_task.queued|started|completed|failed` with the Task (schemas in `schemas/cron/`;
  see Implementation notes for the entity spelling).

## 4. The steward role

`candybar daemon --roles steward`, host unit `deploy/host/candybar-steward.service` (an event-driven worker, not a
clock). It consumes the command with a durable consumer, at most 2 tasks at a time, and per task:

1. Marks the task `running`, resolves the candybar's context bundle: definition, runner (`/runner`), the ledger entry
   (`deploy/migration/ledger.yaml`, `source_of_truth`), the manifest path, and for `systemd` the unit text
   (`systemctl --user cat`), for `hermes` the job record, for `exec` the argv files; the last 30 invocations
   (results, exit codes, error tails); the profile.
2. Picks the working directory: the ledger repo's local checkout, else an exec `workdir`/argv path's git root, else a
   hermes job `workdir`, else this repo.
3. Opens an OpenCode session over the v2 HTTP API on the host (`~/.local/state/opencode/service.json` gives the
   background service's URL and Basic-auth password; see Implementation notes) with `location.directory` = the working directory and the task's model,
   sends the prompt rendered from `deploy/steward/templates/<action>.md`, and waits (enrich 15 min, fix and refactor
   30 min, evaluate 20 min; then `interrupt` and fail).
4. Reads the last fenced ```json block of the final answer, validates it, and applies it:
   - enrich: `overview` → profile (+ `templateVersion`); then the project: the ledger repo's local path matched to a
     pjangler project (`GET http://127.0.0.1:8764/v1/registry`) is `method: "ledger"`, confidence 1; otherwise Jev
     (`https://jev.delo.sh/typesafe/v1/systemone`, model `jev-1.13.0`, one `noul` per project, accept P >= 0.6,
     highest wins) over the name, description, tags, overview and paths; no match leaves `project` null.
   - evaluate: `evaluation` → writes `~/code/DeLoDocs/Reports/<YYYY-MM-DD in America/New_York>/candybar-evaluate-<name>.md`
     (frontmatter `date, type: candybar-evaluation, candybar, outcome, generated_by: candybar-steward`) and a sibling
     `.json`, copies both with `mc cp` to `delo/daily-reports/<date>/`, stores `reportKey`, then runs the follow-up.
   - fix: the agent repairs, commits and pushes in the job's repo and reports `{repaired: bool, summary, commits[]}`;
     if repaired, the steward invokes the candybar once and waits for the invocation (timeout = the runner's, max
     1 h): `outcome` = `fixed` on success, else `not_fixed`.
   - refactor: the agent applies the evaluation's recommendation (code, model, or `candybar edit --schedule`),
     commits and pushes; `outcome: "done"`.
5. Marks the task `succeeded` or `failed` (with `error`) and publishes the fact.

Templates are versioned files in this repo; changing one bumps its `templateVersion`. Every prompt tells the agent
the commit/push rule, the single-clock rule (never add a timer, cron or loop; use `candybar edit`), never to write a
secret value, and to end with exactly one fenced ```json block.

## 5. What Holocene renders

Run health is not stored: Holocene reads the last 30 invocations per candybar and draws a micro timeline, oldest
left (success green, fail red, running neutral).

## Implementation notes (candybar, 2026-11-03)

Minimal deviations made while building it, each the smallest change that works:

- **Fact types** are `bloodbank.cron.steward_task.{queued,started,completed,failed}`, not `steward.task.*`: Bloodbank
  types are exactly four tokens (`docs/event-naming.md`), so the entity is one token, `steward_task`. The command is
  `bloodbank.cron.candybar.steward` as written. Bus payloads use the bus's snake_case (`task_id`, `candybar_id`,
  `parent_task_id`, ...) plus the candybar's `name`; the REST shapes stay camelCase as above.
- **OpenCode service file:** the background service's URL and password live in
  `$XDG_STATE_HOME/opencode/service.json` (default `~/.local/state/...`), the file the `opencode` client itself
  maintains (its URL moves on restart). `~/.config/opencode/service.json` holds only a password and is a fallback for
  it. `CANDYBAR_OPENCODE_URL` / `CANDYBAR_OPENCODE_SERVICE_FILE` override.
- **Empty profile:** `GET /candybars/{id}/profile` with no stored profile answers every field null, `updatedAt` too.
- **Working directory:** the ledger repo's checkout is found under `~/code` (and one level below) by name, or as the
  git root of a path the ledger entry names (unit file, jobs file, rollback commands), and is accepted only when its
  `origin` points at that repo. Between the hermes step and "this repo", a `systemd` runner falls back to the git root
  of the script its `ExecStart` runs.
- **Answers:** an answer that does not validate gets one repair turn in the same session before the task fails.
- **Enrich project:** a stored `method: "manual"` project is never replaced; a Jev or registry outage leaves the stored
  project unchanged (the overview is still written) and says so in the task summary.
- **Evaluate:** the follow-up is derived from the outcome (the agent's `followUp` is ignored). A failed report upload
  fails the task before anything is stored.
- **Tasks:** `PATCH /steward/tasks/{id}` refuses an impossible status move (409: a terminal task never changes, a
  queued one cannot succeed without running) and stamps `startedAt` / `finishedAt` when the caller does not. A
  redelivered command whose task is already `running` (the steward died mid-task) fails it as abandoned instead of
  running it twice. A refactor request is a 422 (the body only accepts enrich, fix, evaluate).
