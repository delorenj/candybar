# mac-jev-rollup: Damian's Mac roll-up sidecar

The Mac half of the big-chungus `jev-rollup` consumer (mirror plan W9-5). It reads the local
Candystore (`127.0.0.1:8683`, `bloodbank.conversation.turn.annotated` joined to the source
`turn.completed` reply text), writes one note per project per day and composes `DAY.md`.

**Bus-fed: it reads no credential.** The v2.0 engine only reads Candystore, so there is no Jev
key on this path (the Mac's `damian-studio` Jev key is for the extractor tools, not this one),
nothing to resolve and no key file. `jev_hostenv.py` is only used to find `JEV_ROLLUP_DIR`.

## Where the code lives (NOT in this repo)

The source of truth is `delorenj/agents` `tools/jev-clairic` (private). This repo is public, so the
five tool files are **not** kept here; they are copied to the peer host out of band, the same way
Jarad's curated skills reach Damian's Mac:

| file | agents path |
|---|---|
| `jev-rollup`, `jev-rollup-run`, `jev-env.sh`, `jev_hostenv.py` | `tools/jev-clairic/bin/` |
| `jev-rollup-cadence.sh` | `tools/jev-clairic/hermes/` |

`VENDORED.sha256` pins the bytes (agents commit `85b9498`) and `install.sh` refuses to install on a
mismatch. Deliver from big-chungus:

```sh
src=~/.agents/tools/jev-clairic; stage=/tmp/jev-rollup-src
ssh mac "mkdir -p $stage" && scp $src/bin/{jev-rollup,jev-rollup-run,jev-env.sh,jev_hostenv.py} $src/hermes/jev-rollup-cadence.sh mac:$stage/
scp VENDORED.sha256 install.sh mac:$stage/ && ssh mac "JEV_ROLLUP_SRC=$stage sh $stage/install.sh && rm -rf $stage"
```

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
