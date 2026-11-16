# mac-jev-rollup: Damian's Mac roll-up sidecar

The Mac half of the big-chungus `jev-rollup` consumer (mirror plan W9-5). It reads the local
Candystore (`127.0.0.1:8683`, `bloodbank.conversation.turn.annotated` joined to the source
`turn.completed` reply text), writes one note per project per day and composes `DAY.md`.

**Bus-fed: it reads no credential.** The v2.0 engine only reads Candystore, so there is no Jev
key on this path (the Mac's `damian-studio` Jev key is for the extractor tools, not this one),
nothing to resolve and no key file. `jev_hostenv.py` is only used to find `JEV_ROLLUP_DIR`.

## Where the code lives, and why it is vendored here

The source of truth is `delorenj/agents` `tools/jev-clairic` (private; the Mac cannot and by
design should not clone it). This directory is a byte-for-byte copy of the five files the
sidecar needs and nothing else (no crank tooling, no extractor wrappers, no `jev-secrets`):

| file | agents path |
|---|---|
| `jev-rollup`, `jev-rollup-run`, `jev-env.sh`, `jev_hostenv.py` | `tools/jev-clairic/bin/` |
| `jev-rollup-cadence.sh` | `tools/jev-clairic/hermes/` |

Vendored from agents commit `85b9498` (W9-1: host overlay, trustworthy cadence exit status).
`VENDORED.sha256` pins the bytes and `install.sh` refuses to install on a mismatch. Re-vendor by
copying the five files again and re-running `sha256sum` (drift check: compare against
`~/.agents/tools/jev-clairic` on big-chungus). `jev_hostenv.py` is also byte-identical to the copy
in the activity-report and delonet-daily-report skills.

## Install on the Mac and register the clock

```sh
sh deploy/mac/jev-rollup/install.sh          # copies to ~/.local/share/33god/jev-rollup, links ~/.local/bin
candybar register deploy/mac/jev-rollup      # created disabled
candybar run mac-jev-rollup --wait           # a terminal tick
candybar resume mac-jev-rollup
candybar history mac-jev-rollup -n 5
```

Stop: `candybar pause mac-jev-rollup`.

## Output

`JEV_ROLLUP_DIR` (environment, then `~/.config/33god/host.env`) names the notes directory. Unset,
it is `~/.local/state/jev-rollup/rollups/<YYYY-MM-DD>/<project>.md` plus `DAY.md`, never inside
anyone's vault by default. Pointing it at Damian's vault is his decision (one line in
`host.env`). State: `~/.local/state/jev-rollup/{cursor.json,inbox/}`.

## Missed-fire policy, idempotency

See the header of `candybar.toml`. Ticks are cursor-based and re-running one is safe (it reads
events newer than the cursor); `roll` folds and deletes the inbox, so a repeat finds it empty.
