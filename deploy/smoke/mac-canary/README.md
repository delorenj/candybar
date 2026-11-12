# mac-canary: the Mac's clock-liveness canary

Damian's Mac Studio runs its own Candybar (control plane in Colima, native
LaunchAgent executor, see `deploy/mac/README.md`). This manifest is that
install's canary, owned by the Mac and registered only in the Mac's registry.
Do not register it on big-chungus: the log it appends to is a per-host file.

What it does: once an hour, at minute 5 America/New_York, it appends one line
(an ISO 8601 timestamp with offset, then `/bin/hostname`) to
`$HOME/.local/state/33god/candybar-canary.log`. Nothing else. The argv is
host-neutral (`/bin/sh`, `/bin/date`, `/bin/hostname` and `$HOME`), so the same
file works on any Mac that runs the executor with `HOME` set.

Register and prove, on the Mac, from the repo root:

```sh
candybar register deploy/smoke/mac-canary          # created disabled (enabled = false)
candybar show mac-canary                           # read the registration back
candybar run mac-canary --wait                     # manual terminal invocation
tail -n 1 ~/.local/state/33god/candybar-canary.log # the effect
candybar resume mac-canary                         # enable the hourly schedule
candybar history mac-canary -n 5                   # a trigger=scheduled row, then the next log line
```

First proven on 2026-11-12 (W5-3): invocation `trigger=manual` at 09:01:53 EST and
`trigger=scheduled` at 09:05:00 EST, both `success`, exit 0, each with its log line.
Stop it with `candybar pause mac-canary`.
