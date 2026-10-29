# deploy/migration — the cron-migration ledger (Story 7.5, AD-21)

## Scheduling ownership policy (CAND-50)

Candybar owns recurring application clocks. Run `uv run candybar doctor --repo .` in CI
and before adding a timer; use `uv run candybar doctor --repo . --live --json`
for a **read-only** host audit. Exit 0 means the audited scope passes, 1 means
bypass clocks were found, and 2 means an inventory source is unavailable or
malformed (never treat 2 as green). `--write-inventory /absolute/path.json`
saves an identity-only snapshot. `--inventory /path.json` audits an offline
fixture. An optional `--n8n-export /path/to/workflows.json` checks active
ScheduleTrigger/cron nodes without assuming access to an n8n API.

The repo gate flags new `.timer` files but accepts `.service` units, including
Candybar-owned runners. The live audit covers all local Hermes profile job
stores, current-user and system systemd timers, current-user and `/etc`
crontabs. It does **not** cover other users' private crontabs, remote hosts, or
n8n without an export. Existing approved independent clocks and vendor/admin
maintenance have *exact* owner/reason exemptions in `deploy/scheduling-policy.json`;
there are no wildcard exemptions. A native timer still fails even if its
`.service` is the registered runner. Unknown/drift are observations, not
permission to disable or migrate other jobs en masse.

Rollback exceptions are **conditional**, not a policy label: doctor rereads this
checkout's canonical `deploy/migration/ledger.yaml`, under the migration directory
lock, and requires the exact ledger id, native timer, API UUID and runner identity,
`status: rolled-back`, `disable_pending: false`, and an observed disabled
registration. Live audits refresh the registry while holding that lock; an active
migration, missing/malformed ledger or registry, or absent exact registration is
unknown, never pass. An enabled exact registration plus native timer is a duplicate
clock failure. Neither `CANDYBAR_LEDGER`, an alternate policy's ledger path nor an
offline inventory's claimed ledger status is evidence. Offline fixtures supply only
registry observations and still use the canonical ledger; they are not live-host
certification.

The stdout transition wrapper persists `<state-file>.pending.json` (payload,
delivery route and candidate detector state), fsyncs file and directory **before**
transport, and replays it under the existing detector lock before running a new
check. After confirmation it commits that candidate, then checks current health;
a recovered transient therefore yields the original alert followed by a separate,
ordered recovery. Persistent failures retain the original pending bytes. Malformed
pending data or a changed route is unknown and fails without overwriting the
outbox: inspect and repair the file/route rather than deleting it to make green.
Existing detector state and `.delivery-health` markers remain compatible; state
symlink aliases use the same resolved lock/outbox. Delivery is **at least once**:
remote acceptance followed by interruption before local commit/removal can cause
a duplicate. The Hermes endpoint has no idempotency-key contract here; this is not
exactly-once delivery and does not add a scheduler or inference.

The Candybar-owned `deploy/policy/candybar.toml` direct-exec job emits one
aggregate count-only new/cleared condition notification per changed audit via
the existing `33god-pm` route; no timer names, provider quotas, models, or
job contents enter Telegram. It was registered disabled and drilled first;
its committed manifest reflects the enabled production state. Its
report is `/home/delorenj/.local/state/candybar-scheduling-policy/report.json`.
AD-22's independent `candybar-watchdog.timer` remains outside Candybar because
Candybar cannot watch its own clock. CI checks a clean fixture and proves a
rogue-timer fixture exits 1; machine drift is reported, not an automatic CI
failure for unrelated existing clocks. Fleet agents can run the exact doctor
command above; global agent instructions belong to Flume, not this repo.

## Ledger

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
the CLI exits 3; re-run `rollback` when the API is back. This is an
**availability-first** rollback, not a strict single-clock guarantee under an
API-only outage: Candybar may remain enabled while the restored native scheduler
also fires. Doctor must fail/return actionable unknown until the exact registration
is disabled and `disable_pending` clears; do not treat rollback-ready as proof of
single-clock safety. The executor skips queued scheduled fires only when it can
read the Candybar registration as disabled.
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
