# mac-skillex-hermes-resync: strict Hermes desks follow the catalog

Candybar is the only clock on Damian's Mac. Every 15 minutes this runs
`hermes-skillex-resync.py` (from the skillex repo, `scripts/`) against the Mac's
Skillex catalog checkout (`~/code/skillex`). A strict desk records the catalog
commit it was last synced against, so any catalog commit leaves it
`sync pending` (`skillex profile show` exit 6); the job strict-syncs the pending
desks and re-checks them. Foreign or unowned content is never touched (exit 3,
`refused`): fix that desk with `hermes-skillex-cutover.py`, preview first.

It replaces the launchd agent `io.33god.skillex-hermes-resync` (StartInterval 900,
WatchPaths, RunAtLoad), retired under mirror W5-4. The plist is kept in
`~/.config/33god/config-backups/` on the Mac for rollback.

Registered only in the Mac's registry (127.0.0.1:8700). big-chungus has its own
`skillex-hermes-resync`; never register this one there.

The script lives outside the diverged `~/code/skillex` checkout, in
`~/.local/share/33god/skillex-hermes-resync/` (copied from the skillex repo's
`scripts/`; the Mac checkout is not a push source). Refresh both files from
skillex `scripts/` after a skillex release.

Missed-fire, overlap and idempotency policy is spelled out at the top of
`candybar.toml`.

Register and prove, on the Mac, from a copy of this directory:

```sh
candybar register deploy/mac/skillex-hermes-resync   # created disabled
candybar show mac-skillex-hermes-resync
candybar run mac-skillex-hermes-resync --wait --timeout 900   # manual terminal run
cat ~/.local/state/skillex/hermes-resync.last.json            # status ok, counts.error 0
candybar resume mac-skillex-hermes-resync
candybar next
candybar history mac-skillex-hermes-resync -n 5               # a trigger=scheduled row
```

Stop it with `candybar pause mac-skillex-hermes-resync`.
