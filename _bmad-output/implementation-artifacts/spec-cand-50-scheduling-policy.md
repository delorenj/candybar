---
title: 'CAND-50: Candybar-owned watchdog and scheduling policy'
type: feature
created: '2026-10-29'
status: in-progress
route: dispatch
baseline_commit: e81c135829971e7f347f712a6579d928452cdfaf
context: []
---

<frozen-after-approval reason="explicit delegated operator directive authorizes implementation and ordinary cutover">
## Intent
Candybar must own recurring application clocks, including crank-watchdog. Move its existing no-agent script onto an exec runner, preserving stdout transition notifications to the configured 33god-pm Telegram target, then detect scheduling bypasses before deployment and on the machine.

## Boundaries & Constraints
Always: sequential WIP1; direct exec and zero inference; disabled registration and isolated drills before cutover; retire legacy before enabling; preserve rollback and real invocation IDs. Lint is read-only, permits services, external Hermes executions and explicit owner/reason exceptions. AD-22 self-watchdog and canonical infra guards remain independent.
Never: edit watchdog detection; claim its separate review accepted; change auth or model settings; restart gateway/executor; bulk disable/migrate other schedules; invent bus contracts; work in Flume's parallel scope.

## I/O & Edge-Case Matrix
| State | Expected |
|---|---|
| Detector silent | no notification |
| Detector transition stdout, exit 0 | exact existing Telegram route, commit state after delivery |
| Detector or notifier fails | fail invocation; preserve prior transition state for retry |
| Rogue timer / active user crontab / internal recurring Hermes | fail with 'not a Candybar; register' |
| Service / disabled job / registered external Hermes / explicit exception | pass |
| Bad/unreachable inventory | unknown, nonzero, never green |
</frozen-after-approval>

## Code Map
- `README.md`: runner contract and APIs; no-agent means exec, not Hermes runner.
- `src/candybar/cli/main.py`: add thin doctor command; migrations already own inventory IDs.
- `deploy/migration/ledger.yaml`, planning cron-inventory: evidence and canonical exclusions; no blanket grandfathering.
- `deploy/watchdog/`: AD-22 exception; do not change its scheduling.
- Installed `~/.hermes/profiles/33god-pm/scripts/crank-watchdog.{sh,py}`: reuse unchanged, transitions on stdout exit 0; supports isolated state/source fixtures.

## Tasks & Acceptance
- [x] `src/candybar/transition_exec.py`, manifest and tests: bounded transition delivery through supported `hermes -p 33god-pm send`, transactional state and silence proof.
- [x] `deploy/migration/ledger.yaml`, crank manifest and evidence report: disabled registration, test, cutover, scheduled proof, rollback commands.
- [x] `src/candybar/scheduling_policy.py`, CLI, policy config and tests: repo-unit lint and live read-only systemd/crontab/all-profile Hermes inventory; optional explicit n8n export; unknown on failed probes.
- [x] `.github/workflows/ci.yml`, README: fixture/repo gate and reusable exact command for fleet policy; Candybar drift job with transition-only ops notification.
- Given migration ready, when cutover executes, then legacy clock is disabled/external and only Candybar fires the watchdog with real runner/result evidence.
- Given fixtures and live host, when doctor runs, then unauthorized clocks fail and unavailable sources stay unknown; remaining drift is reported without mutation.

## Implementation Notes
CAND-50 independent HOLD repair (F1/F2), authorized by the delegated operator directive: persist payload plus candidate state before transport, replay before fresh detection, preserve uncertain pending data, and verify conditional rollback exemptions against fresh canonical ledger and exact disabled registry identity. At-least-once delivery only; rollback remains availability-first during API outage. Changes exclude CAND-49 scoped activation paths. Parent performs independent re-review; ticket remains in progress.

Component board CAND-50, delegated owner. Component manifest has no projected PM. g33 preflight PASS (legacy board warning); CodeGraph status inspected, no sidecar index. The user's explicit sequential scope and deploy authorization resolve checkpoint decisions; no additional admin approval required.

## Verification
`uv run pytest -q`; `uv run ruff check .`; deployment hygiene scripts; `uv run candybar doctor --repo .`; live doctor JSON saved under component BMAD. First scheduled watchdog fire supplies invocation/tick/result readback. No broad health claim.
