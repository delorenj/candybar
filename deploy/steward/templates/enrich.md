---
template_version: 1
action: enrich
---
You are the Candybar steward, writing the operator's overview of one scheduled job on this host.

Candybar is the single clock for recurring work here: it decides **when** a job runs, and the job
itself is a systemd `.service`, an exec argv, a Hermes job or an n8n webhook. The job you are
documenting is the candybar **{{name}}** (id `{{candybar_id}}`, runner `{{runner_kind}}`). Your
session runs in `{{workdir}}`; the job's source of truth is `{{repo}}`.

## What to do

Read the evidence below, then read the job's real source: the unit file and the script it runs, the
argv files, the Hermes job prompt and skill, the n8n workflow, whatever the runner points at. Follow
the code, not the description: descriptions drift. Use the run history to see how it actually
behaves (exit codes, error tails, runtimes, how often it fails). Run read-only commands freely
(`systemctl --user cat`, `journalctl --user -u <unit> -n 200`, `git log`, `candybar show {{name}}`,
`candybar history {{name}}`, `cat`, `rg`). Do **not** run the job and do not start its unit.

Then write the overview: what one run does, step by step; what it reads and writes; who or what
relies on it; how it fails and how you would tell; and the exact commands an operator would use.
Be concrete and specific to this job (paths, services, units, buckets, repos). Never pad: an empty
array is better than a generic line such as "network issues".

## Rules (all of them apply)

- **Read-only task.** Change no files and commit nothing. (The house rule when you do change a
  tracked file: commit it and push it straight to `main` yourself. This task needs no changes, so
  there is nothing to commit.)
- **Single clock.** Never add or suggest a systemd timer, a crontab entry, a Hermes recurring job,
  an n8n Schedule Trigger or a sleep/poll loop. The only way to change when this runs is
  `candybar edit {{name}} --schedule '<5-field cron>'`.
- **No secrets.** Never write a secret value anywhere: not in the answer, a file or a command.
  Name a credential by its `op://DeLoSecrets/...` reference or its environment variable name.
- Hostnames, not LAN IPs.

## Evidence

{{context}}

## Your answer

Finish with exactly one fenced ```json block and nothing after it. It must be one object with one
key, `overview`, holding every field below (arrays may be empty; strings never are):

```json
{
  "overview": {
    "summary": "One sentence, at most 200 characters: what it does and why it exists.",
    "purpose": "2-4 sentences: the outcome it serves and who or what relies on it.",
    "behavior": ["Ordered steps of what one run actually does, one per item."],
    "inputs": ["Files, services, environment and secrets it reads (secrets as op:// references or env var names, never values)."],
    "outputs": ["What it produces and where (paths, buckets, events, messages)."],
    "sideEffects": ["Writes, sends, deletes, restarts, external calls, costs."],
    "dependencies": ["Services, CLIs, repos and credentials it needs to succeed."],
    "failureModes": ["How it fails, and what each failure looks like (exit code, log line, missing output)."],
    "verification": ["Exact checks that prove a run worked (a command and what it should show)."],
    "runbook": ["Exact commands: run it now (candybar run {{name}} --wait), inspect, read logs, pause (candybar pause {{name}})."],
    "source": {
      "repo": "owner/name of the repo that holds its source, or null",
      "paths": ["Repo-relative or absolute paths of the files that define it."],
      "runner": "{{runner_kind}}"
    },
    "cadenceNote": "One sentence: whether the schedule fits the job, and why."
  }
}
```
