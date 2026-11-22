# Remote natives the ledger cannot drive

`ledger.yaml` knows three native kinds (`systemd-timer`, `crontab`, `hermes`), all probed and disabled on the host
the CLI runs on. A native clock on another machine (a launchd job on a Mac) cannot be an entry: the loader would
reject the kind, and `candybar migrate` would break for every job. Such a cutover is done by hand in the same safe
order the tool uses (register the candybar disabled, retire the re-enabler, retire the native clock, enable) and is
recorded here with its exact commands. Doctor `--live` does not see these hosts either; the proof is the remote
host's own output, quoted below.

## launchd:sh.delo.mobile-deploy-hub.catchup (hub-mac, gui/502)

| | |
|---|---|
| status | cut over 2026-11-21 (wave: mobile-deploy-hub catch-up clean-up) |
| native clock | LaunchAgent `sh.delo.mobile-deploy-hub.catchup`, `StartInterval 300` + `RunAtLoad`, 529 runs, last exit 0 |
| candybar | `mobile-deploy-hub-ios-catchup` (`01a200bb-b5e9-7802-8d34-2fade600fec8`), `*/5 * * * *` America/New_York, exec runner; project candybar in delorenj/mobile-deploy-hub `.candybars/mobile-deploy-hub-ios-catchup.candybar.toml`, desk `.candybars/mobile-deploy-hub-ios-catchup/` |
| design | the LaunchAgent is kept as the on-demand executor (devicectl and the keychain only work inside the logged-in GUI session); candybar kickstarts it over ssh, waits, reports `last exit code`. Mac away is a success with `HUB_MAC_AWAY`, a pass already running is a success with `HUB_MAC_BUSY`, a nonzero pass is a failure |
| source of truth | delorenj/mobile-deploy-hub `tools/catchup/units/sh.delo.mobile-deploy-hub.catchup.plist` and `install.sh`, commit `b05f557` (the re-enabler: `install.sh` re-bootstraps this plist on every `hosts:install`, now interval-free) |
| deviation | the hub release dir on the Mac (`~/.local/share/mobile-deploy-hub/releases/v1.2.41`) still holds the old plist; running ITS `install.sh` would restore the interval. The next `mise run hosts:install` replaces it |

Disable the native clock (what was done; run from big-chungus):

```sh
sed 's#@HOME@#/Users/delorenj#g' tools/catchup/units/sh.delo.mobile-deploy-hub.catchup.plist | ssh hub-mac 'cat > /tmp/l.plist.part'
ssh hub-mac 'label=sh.delo.mobile-deploy-hub.catchup; d=gui/$(id -u); p=$HOME/Library/LaunchAgents/$label.plist
  plutil -lint /tmp/l.plist.part && mv /tmp/l.plist.part "$p" && { launchctl bootout $d/$label 2>/dev/null; sleep 1; launchctl bootstrap $d "$p"; }
  launchctl print $d/$label | grep "run interval"   # must print nothing'
```

Rollback (pause the candybar FIRST so two clocks never run together):

```sh
uv run candybar pause mobile-deploy-hub-ios-catchup
ssh hub-mac 'label=sh.delo.mobile-deploy-hub.catchup; d=gui/$(id -u); p=$HOME/Library/LaunchAgents/$label.plist
  cp /tmp/$label.plist.rollback-old "$p"   # the interval version saved at cutover (or re-render the hub plist at commit aea988d)
  launchctl bootout $d/$label; sleep 1; launchctl bootstrap $d "$p"; launchctl print $d/$label | grep "run interval"'
```

Proof of the first scheduled fire (2026-11-21 20:00 EST): invocation `6b2691f7-d926-50e6-9ada-541d5e173bb4` success,
2.9 s, and on the Mac `~/Library/Logs/mobile-deploy-hub/catchup.log` has `2026-11-22T01:00:00Z HUB_LOCK mac-ios` /
`HUB_CATCHUP_IDLE ios live ...` at the candybar's second (the old native passes were at :x1:54), `launchctl print`
shows `runs = 1`, `last exit code = 0`, no `run interval` line and `properties = inferred program` (no `runatload`).
