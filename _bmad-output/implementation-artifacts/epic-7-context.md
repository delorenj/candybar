# Epic 7 Context: Cron Migration onto Candybar

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Move every `migrate`-classified job on big-chungus (70 of 117 host jobs: user crontab lines, systemd user timers, Hermes cron jobs) under Candybar without changing what it does or when it fires on the wall clock. Foundation stories 7.1-7.6 add what migration needs (per-candybar timezone, a host executor, runner kinds, a ledger with rollback, an off-candybar watchdog); eleven waves (7.7-7.17) then move the jobs, each cutover recorded and reversible.

## Stories

- Story 7.1: Per-candybar IANA timezone
- Story 7.2: Host executor
- Story 7.3: Runner kinds (exec, systemd)
- Story 7.4: Hermes runner
- Story 7.5: Migration ledger, rollback and the migrate CLI
- Story 7.6: Off-candybar self-watchdog
- Stories 7.7-7.17: Migration waves 1-11

## Requirements & Constraints

- Wall-clock semantics are preserved: systemd `OnCalendar` is local time (America/New_York) and US DST ends 2026-11-01, so a candybar's cron must be evaluated in its own IANA zone with Vixie-cron DST rules (a fixed-hour job in the repeated hour fires once; one in the spring-forward gap fires once after the gap; `*` hour fields keep real-time cadence).
- Jobs need the host (`systemctl --user`, `op`, docker socket, `~/`, zellij, hermes CLI), so execution moves out of the container.
- Excluded jobs stay on their scheduler: vendor-owned units, liveness guards for things Candybar depends on, sub-minute cadence, dead or paused jobs.
- Every cutover is reversible: a committed ledger holds the exact disable and rollback commands, and the job's source of truth (installer, repo unit file) is changed so a re-install cannot resurrect the old scheduler.
- Jobs with external side effects are never manually run to test; their first scheduled fire is the proof.
- Ticks, tick ids, idempotency keys, events and API timestamps stay UTC everywhere.

## Technical Decisions

- One shared fire-time helper is the only source of fire instants; dispatcher match, API window/next, misfire and backlog checks all call it.
- The executor role runs as a host `systemd --user` unit with a single durable (no routing field); the container keeps heartbeat, dispatcher and api.
- Runner kinds: `exec`, `systemd` (start an existing unit, meter from its properties), `hermes` (trigger a Hermes job and wait).
- Default overlap policy is skip; stale executor backlog coalesces to the latest fire.
- The watchdog lives off Candybar (a systemd timer), never as a candybar.

## Cross-Story Dependencies

- 7.1 (timezone) and 7.2 (host executor) gate every wave; 7.3 gates all systemd-unit waves; 7.4 gates the Hermes wave (7.17); 7.5 gates every wave's cutover; 7.6 gates declaring the migration finished.
- The Hermes runner needs a `scheduler: external` change in delorenj/hermes-agent (deployed with one batched gateway restart in wave 11).
