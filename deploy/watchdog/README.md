# deploy/watchdog — the off-candybar self-watchdog (AD-22, Story 7.6)

A `systemd --user` timer that watches candybar from OUTSIDE candybar. A
scheduler must never be its own watchdog, so this unit pair is never
registered as a candybar — `scripts/check-watchdog-not-a-candybar.sh` fails CI
if it ever is.

Every 2 minutes `candybar-watchdog.sh` checks:

1. `https://candybar.delo.sh/healthz` answers 200 — the whole edge path plus
   the heartbeat/dispatcher/api container.
2. The every-minute canary candybar `watchdog-canary` (manifest in
   `deploy/smoke/watchdog-canary/`) has a terminal invocation finished within
   3 minutes and `result == success` — proves heartbeat → dispatcher → NATS →
   host executor → registry → API read path end to end.
3. `candybar-executor.service` is active — the host executor (AD-17).

Paging (via `~/.local/bin/ntfy-alert`): exactly one page on the OK→FAIL
transition naming the failing checks, and one on recovery. No re-page while
an outage persists; state lives in `~/.local/state/candybar-watchdog/`.
The script never restarts anything — page-only.

Ok-stamp: on full success the script touches
`~/.local/state/candybar-watchdog/ok`. infra-doctor's `file_age` probe
`candybar-watchdog-alive` (delorenj/infra `infra-doctor/config/probes.yaml`,
600s) fails when the stamp goes stale, so a stopped watchdog timer cannot go
unnoticed — the watchdog has a watchdog.

| file | role |
|---|---|
| `candybar-watchdog.sh` | the three checks, transition paging, ok-stamp |
| `candybar-watchdog.service` | oneshot running the script |
| `candybar-watchdog.timer` | `OnCalendar=*:0/2`, `Persistent=true` |
| `install.sh` | idempotent link + enable + start |

Install: `deploy/watchdog/install.sh`. Remove:
`systemctl --user disable --now candybar-watchdog.timer` and delete the
infra-doctor probe (or it will page about the stale stamp, correctly).

Simulated-outage drill: `systemctl --user stop candybar-executor.service`,
wait for the next 2-minute boundary — exactly one page, no stamp. Restart the
executor — one recovery page and a fresh stamp.
