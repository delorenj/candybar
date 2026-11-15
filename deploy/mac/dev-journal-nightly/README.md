# mac-dev-journal-nightly: Damian's morning Dev Journal

Candybar is the only clock on Damian's Mac. This fires
`delonet-daily-report/scripts/nightly.sh nightly` from the Mac's skills clone
(`~/.local/share/33god/skills`) at 06:00 America/New_York. `nightly.sh` verifies
yesterday's report, generates it if missing, and emails it to Damian once. It
reads `~/.config/delonet-daily-report/report.json` and
`~/.config/33god/host.env` (names and `op://` references only; the Resend key
resolves through the interim store, no vault token on the Mac).

Registered only in the Mac's registry (127.0.0.1:8700). big-chungus has its own
dev-journal jobs; never register this one there.

Missed-fire, DST and idempotency policy is spelled out at the top of
`candybar.toml`. In short: the latest missed fire runs once at wake (AD-6), a
second run for the same published report sends nothing
(`DDR_DELIVER_ONCE=1`), and 06:00 is unaffected by DST.

Register and prove, on the Mac, from a copy of this directory:

```sh
candybar register deploy/mac/dev-journal-nightly   # created disabled
candybar show mac-dev-journal-nightly
candybar run mac-dev-journal-nightly --wait --timeout 1800   # manual terminal run
candybar resume mac-dev-journal-nightly
candybar next
candybar history mac-dev-journal-nightly -n 5      # a trigger=scheduled row
```

Backfill a missed day: `nightly.sh nightly --date YYYY-MM-DD`.
Stop it with `candybar pause mac-dev-journal-nightly`.
