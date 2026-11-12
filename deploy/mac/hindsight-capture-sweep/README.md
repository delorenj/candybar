# mac-hindsight-capture-sweep: Damian's Mac session write-back sweep

Candybar is the only clock on Damian's Mac (operator decision 2026-11-12).
This is the Mac's replacement for the Linux `hindsight-capture-sweep.timer`
(see `deploy/migration/ledger.yaml`, `systemd:hindsight-capture-sweep.timer`),
which does not exist on macOS. Registered only in the Mac's registry; the
big-chungus job of the same purpose is `hindsight-capture-sweep`.

What it does: every 10 minutes (UTC) it runs
`session_capture.py sweep` with the hook-hub's own venv python. The sweep
flushes session buffers idle past `HINDSIGHT_CAPTURE_IDLE_S`, confirms
submitted batches and retries failed ones, so a session that died without a
SessionEnd is still written to its Hindsight bank. It prints one JSON line
(`examined`, `flushed`, `busy`). `jot_flush.py sweep` is deliberately NOT
included: the jot-flush hook is kept disabled on the Mac (OpenRouter). The API
URL and key come from `~/.hindsight/config` (rendered by `env-op-resolve`).

Register and prove, on the Mac, from a copy of this directory:

```sh
candybar register deploy/mac/hindsight-capture-sweep   # created disabled
candybar show mac-hindsight-capture-sweep
candybar run mac-hindsight-capture-sweep --wait        # manual terminal run
candybar resume mac-hindsight-capture-sweep
candybar history mac-hindsight-capture-sweep -n 5      # a trigger=scheduled row
```

Stop it with `candybar pause mac-hindsight-capture-sweep`.
