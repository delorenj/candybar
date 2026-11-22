# deploy/migration: remote natives the ledger cannot drive

`ledger.yaml` models local natives only (`systemd-timer`, `crontab`, `hermes`), and `candybar migrate` and
`candybar doctor --live` probe this host. A native clock on another host is migrated by hand, in the same safe
order the tool uses, and recorded here. One scheduler per job at every step.

## launchd:sh.delo.mobile-deploy-hub.catchup (hub-mac, gui/502) - cut over 2026-11-21

- Job: the iPad catch-up pass of delorenj/mobile-deploy-hub, `catchup.mjs ios`, a LaunchAgent with `StartInterval 300`
  and `RunAtLoad`. It must stay a LaunchAgent: devicectl and the keychain only work in the logged-in GUI session.
- Candybar: `mobile-deploy-hub-ios-catchup` (project candybar, `delorenj/mobile-deploy-hub`
  `.candybars/mobile-deploy-hub-ios-catchup.candybar.toml`, `*/5 * * * *` America/New_York, exec runner on
  big-chungus). Its desk script `kick.mjs` runs `launchctl kickstart gui/$(id -u)/<label>` over ssh `hub-mac`, waits
  (bounded) until the pass stops and reports `last exit code`. Mac away is a success with `HUB_MAC_AWAY`; a pass
  already running is a success with `HUB_MAC_BUSY`; a nonzero exit is a failure.
- Retired re-enabler: hub commit b05f557 removed `StartInterval` and `RunAtLoad` from
  `tools/catchup/units/sh.delo.mobile-deploy-hub.catchup.plist`; `install.sh` renders that plist, so a release no
  longer re-installs a clock. The LaunchAgent stays, as the on-demand executor.
- Order performed: candybar registered paused; hub commit pushed; on the Mac the installed plist replaced with the
  interval-free render, `plutil -lint`, `launchctl bootout`, wait, `bootstrap`; `launchctl print` showed no
  `run interval` and `runs = 0`; then candybar resumed.
- Disable the candybar (stop the clock): `uv run candybar pause mobile-deploy-hub-ios-catchup`.
- Rollback to the native clock (pause first): `git revert b05f557` in the hub, or restore the two plist keys, then
  `ssh hub-mac` and re-run `tools/catchup/units/install.sh <ref>` from the Mac's release tree (renders, lints,
  bootout, bootstrap).
- Probe: `ssh hub-mac 'launchctl print gui/$(id -u)/sh.delo.mobile-deploy-hub.catchup'` shows no `run interval`
  line; `runs` climbs by one per candybar fire.
- Not covered: `candybar doctor --live` reads this host only, so it cannot see this Mac. The probe above is the check.
- Exact commands (from big-chungus, the order performed): render and lint the interval-free plist, swap and re-bootstrap:
  `sed 's#@HOME@#/Users/delorenj#g' tools/catchup/units/sh.delo.mobile-deploy-hub.catchup.plist | ssh hub-mac 'cat > /tmp/l.plist.part'`, then
  `ssh hub-mac 'label=sh.delo.mobile-deploy-hub.catchup; d=gui/$(id -u); p=$HOME/Library/LaunchAgents/$label.plist; plutil -lint /tmp/l.plist.part && mv /tmp/l.plist.part "$p" && { launchctl bootout $d/$label; sleep 1; launchctl bootstrap $d "$p"; }; launchctl print $d/$label | grep "run interval"'`
  (prints nothing). The interval version was copied to `/tmp/<label>.plist.rollback-old` on the Mac first (tmp, may be gone).
- Deviation: the Mac's `~/.local/share/mobile-deploy-hub/releases/v1.2.41` still holds the old plist, so running THAT
  release's `install.sh` would restore the interval; the next `mise run hosts:install` replaces it.
- First scheduled fire proof (2026-11-21 20:00 EST): invocation `6b2691f7-d926-50e6-9ada-541d5e173bb4` success, 2.9 s;
  `~/Library/Logs/mobile-deploy-hub/catchup.log` on hub-mac has `2026-11-22T01:00:00Z HUB_LOCK mac-ios` and
  `HUB_CATCHUP_IDLE ios live ...` at the candybar's second (the old native passes ran at :x1:54); `launchctl print` shows
  `runs = 1`, `last exit code = 0`, no `run interval`, `properties = inferred program` (no `runatload`).
