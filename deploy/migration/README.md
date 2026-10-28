# deploy/migration — the cron-migration ledger (Story 7.5, AD-21)

`ledger.yaml` is the committed record of every job moved off its native
scheduler (systemd user timer, user crontab line, Hermes ticker) onto
candybar. One entry per inventory id from
[`cron-inventory.md`](../../_bmad-output/planning-artifacts/cron-inventory.md).
`candybar migrate` reads and writes it; **commit and push it after every
cutover, verify and rollback** — it is how a cutover is undone when the
daemon is down.

The wave stories (7.7-7.17) add entries and drive them with the CLI. No wave
hand-rolls registration SQL or toggles timers outside the ledger.

## Entry schema

```yaml
version: 1
jobs:
  - id: systemd:cache-prune-daily.timer        # the inventory id, verbatim
    wave: '7.7'
    candybar:                                  # the candybar.toml fields (never `enabled`)
      name: cache-prune-daily
      description: daily cache prune
      tags: [migrated, infra]
      schedule: 23 5 * * *                     # cron as written, read in `timezone`
      timezone: America/New_York
      runner: {kind: systemd, unit: cache-prune-daily.service}   # or entrypoint: /abs/path
    native:
      kind: systemd-timer                      # systemd-timer | crontab | hermes
      schedule: OnCalendar=*-*-* 05:23:00, RandomizedDelaySec=10m   # as found, never parsed
      jitter_s: 600                            # native start jitter (dropped by the migration)
      unit: cache-prune-daily.timer            # systemd-timer
      unit_file: /home/delorenj/code/infra/systemd/user/cache-prune-daily.timer  # filled by plan --write
      # crontab:  line: '<the exact crontab line>'  timezone: America/New_York  (cron = its 5 fields)
      # hermes:   profile: peter-griffin  job_id: b240575fae0c  jobs_file: ~/.hermes/profiles/peter-griffin/cron/jobs.json
    deviations: [RandomizedDelaySec=10m dropped]
    native_failing: false                      # true: already fails natively -> verify on result parity
    source_of_truth: {repo: delorenj/infra, path: systemd/user/cache-prune-daily.timer, commit: <sha>}
    disable: [systemctl --user disable --now cache-prune-daily.timer]
    rollback: [systemctl --user enable --now /home/delorenj/code/infra/systemd/user/cache-prune-daily.timer]
    status: planned                            # planned | cutover | verified | rolled-back | decommissioned
    candybar_id: null                          # set by cutover
    cutover_at: null
    verification: {invocation_id: null, result: null, at: null}
    disable_pending: false                     # rollback could not reach the API
    history: []                                # append-only: every plan --write/cutover/verify/rollback
```

`disable` and `rollback` are the **exact** commands, each run as
`bash -euo pipefail -c '<cmd>'`, in order. `candybar migrate plan --write`
fills the per-kind defaults into the file when both lists are empty:

| kind | disable | rollback |
| --- | --- | --- |
| `systemd-timer` | `systemctl --user disable --now <timer>` | `systemctl --user enable --now <resolved unit file>` |
| `crontab` | remove the exact line (`grep -vxF`) | append it back unless present |
| `hermes` | `hermes -p <profile> cron edit <job> --scheduler external` | `... --scheduler internal` |

The systemd rollback enables **by path**: on systemd 257, `disable` of a
`systemctl link`-ed unit removes the link, after which `enable <name>` fails
("Unit ... does not exist"). The `.service` stays loaded for the systemd
runner; only the `.timer` retires.

## Procedure (one job)

```bash
uv run candybar migrate plan <id> --write      # validate, probe, fill defaults; read the output
git commit deploy/migration/ledger.yaml         # commit what will run
uv run candybar migrate cutover <id> [--wait 600]
uv run candybar migrate verify <id>            # exit 3 until the first scheduled fire; then verified
uv run candybar migrate status [--json] [--probe] [--wave 7.7]
uv run candybar migrate rollback <id> [--reason ...]
```

Hermes entries (wave 7.17): the `scheduler: external` pin only takes effect
in the gateway after the batched `hermes-gateway-restart` of that wave
(Story 7.4), so no Hermes job is cut over before it; until then the ticker
would still fire a "retired" job.

Before `cutover`, the owning repo's source of truth must already stop
re-enabling the old scheduler (installer, `mise` task, README enable step),
committed and pushed, with that commit recorded in `source_of_truth.commit`.
Cutover refuses without it.

**cutover** = register the candybar `enabled: false` (or re-sync the one
recorded in `candybar_id`) → run `disable` and probe the native scheduler
retired → `POST /candybars/{id}/enabled {enabled: true}`. Any failure after
the native scheduler was touched runs `rollback` and leaves the candybar
disabled. Refused (exit 1) while a native run is in progress, or within the
guard of the next native fire or the next candybar fire — 2 minutes, or a
quarter of the job's period for jobs firing more often than every 8 minutes
(a strict 2 minutes would make `*/2` and minutely jobs uncuttable) — or while
a jittered native run of the previous occurrence may still be pending, or in
the first 15 seconds after a candybar fire (it is still being dispatched).
`--wait N` polls until the window opens.

**verify** = the first `scheduled`/`misfire` invocation since `cutover_at`.
Never `candybar run` a job with external side effects to test it. `success`
→ `verified` with its invocation id. `fail` → exit 1, unless
`native_failing: true` (result parity). It also fails loudly if the native
scheduler is active again (a re-install resurrected it: two schedulers).

**rollback** = run `rollback` and probe the native scheduler active, THEN
disable the candybar. With the API down the native scheduler is still
restored, the entry becomes `rolled-back` with `disable_pending: true`, and
the CLI exits 3; re-run `rollback` when the API is back. Meanwhile the host
executor skips any queued scheduled fire of a candybar it reads as disabled.
If the restore itself fails, the candybar is NOT disabled (the job keeps one
scheduler) and the CLI exits 1.

**decommission** = retire a job for good: `candybar migrate decommission <id>
--reason '...'`. The native scheduler is left retired (a still-active one is
retired with the entry's `disable` commands first), the candybar is disabled
through the API, and the entry becomes `decommissioned` with the reason in its
history. It is terminal: `rollback`, `cutover` and `verify` refuse it with a
message (the way back is a deliberate ledger edit after re-creating the
source). With the API down the entry still becomes `decommissioned` with
`disable_pending: true`, exit 3; re-run `decommission`. `status --probe` flags
a decommissioned job that something re-enabled as `RESURRECTED`, and `plan <id>`
reports an active native scheduler for it as a problem. After decommissioning,
delete the job's source (unit files, scripts, installers) so nothing
re-creates it; the candybar row itself stays, disabled, for its run history.

### Rollback by hand (daemon AND CLI unavailable)

Run the entry's `rollback` commands from this file, verbatim. Then, once the
API is back, `candybar migrate rollback <id>` (or
`curl -X POST .../candybars/<candybar_id>/enabled -d '{"enabled":false}'`) and
set the entry's `status: rolled-back` in a commit.

Exit codes: 0 ok, 1 refused/failed, 2 invalid ledger or unknown id, 3 pending.
API: `CANDYBAR_API_URL` (default `http://127.0.0.1:8700`). Ledger:
`--ledger`, `CANDYBAR_LEDGER`, else this file in the checkout.
