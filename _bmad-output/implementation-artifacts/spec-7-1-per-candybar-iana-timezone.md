---
title: 'Story 7.1: Per-candybar IANA timezone'
type: 'feature'
created: '2026-10-26'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '1479aa698c0ea37b07b5bf9ec52b1398e72c4755'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-candybar-2026-10-25/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-7-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Candybar evaluates every cron in UTC, so migrating a local-time systemd timer or crontab line shifts its wall-clock schedule by an hour across the 2026-11-01 fall-back. croniter alone also fires a fixed-hour cron twice in the repeated hour and `match()` misses spring-forward gap fires.

**Approach:** Each candybar carries an IANA `timezone` (default `UTC`). One shared `fire_times(schedule, tz, start_utc, end_utc)` helper, applying Vixie-cron DST rules over croniter's naive wall-clock walk, is the only source of fire instants; the dispatcher tick match, the API window/next rows, misfire's latest-fire and skips all call it. Everything stored or emitted stays UTC.

## Boundaries & Constraints

**Always:** `candybars.timezone` is text NOT NULL default `UTC` via alembic (pre-Epic-7 rows unchanged); the name is zoneinfo-validated at registration and PATCH; the cron string is stored exactly as written; ticks, tick_id, idempotency keys, events and API timestamps stay UTC; the `holocene-view-contract.md` field addition ships in the same commit; `skip-rest-of-day`'s `date` is the candybar-local date.

**Never:** a second fire-time implementation (no croniter match on aware datetimes outside the helper); changing `schedule` rows' shape (timezone is a definitions field only); executor, runner or migration-ledger work (stories 7.2-7.5); migrating any job.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Fall-back, fixed hour | `40 1 * * *` America/New_York, 2026-11-01 | one fire at 05:40Z, none at 06:40Z | N/A |
| Spring-forward gap, fixed hour | `30 2 * * *` America/New_York, 2027-03-14 | one fire at 07:00Z (03:00 EDT) | N/A |
| Interval through both transitions | `*/30 * * * *` | a fire at every real half hour | N/A |
| Hourly | `0 * * * *` | every real hour, repeated hour twice | N/A |
| Gap, wildcard hour | `30 * * * *` on the gap day | no catch-up of 02:30 | N/A |
| Rest-of-day skip | `date` 2026-11-01, New York | covers 04:00Z Nov 1 .. 04:59Z Nov 2 (25 h) | N/A |
| Unknown zone at registration / PATCH | `timezone: "Mars/Olympus"` | 422 problem+json, nothing written or published | invalid -> 422 |
| Unresolvable stored zone | row written around the API | dispatcher leaves that candybar out (logged), others dispatch | log + skip |

</frozen-after-approval>

## Code Map

- `src/candybar/dispatcher/firetimes.py` -- NEW: `fire_times`, chunked lazy wall-to-UTC mapping, `last_fire_before`, `local_day_start`; the only croniter walk
- `src/candybar/dispatcher/schedule.py` -- `matches_tick`/`fires_between`/`next_fire`/`previous_fire` now thin views taking `tz`; `SkipSet.tz`, `group_skips(rows, zones)`, `widen_skip_window`
- `src/candybar/timezones.py` -- NEW: `resolve_timezone` (IANA-only, rejects `localtime`)
- `src/candybar/registry/{models,manifest,store}.py` + `deploy/alembic/versions/0005_candybar_timezone.py` -- column, validation, `ScheduleEntry.timezone/.tz`
- `src/candybar/dispatcher/worker.py` -- per-entry tz in match/misfire, skip read widened, bad-zone entries left out
- `src/candybar/api/{app,schedule,stream,shapes}.py` -- register/PATCH/definitions carry `timezone`; window/next/skip/stream read in the candybar zone
- `src/candybar/events/{envelope,builders}.py`, `schemas/cron/candybar.created.json` (+ bloodbank canonical copy) -- additive optional `timezone` on `created`
- `_bmad-output/specs/spec-cron-framework/holocene-view-contract.md`, `README.md` -- field and semantics

## Tasks & Acceptance

**Execution:**
- [x] `src/candybar/dispatcher/firetimes.py` -- implement `fire_times` with the AD-16 DST rules -- the single source of fire instants
- [x] `src/candybar/dispatcher/schedule.py`, `worker.py` -- route every match/walk through it with the entry's tz; local-date skips -- one source of truth
- [x] `registry/*`, `deploy/alembic/versions/0005_*` -- column, default, validation -- AC1
- [x] `api/*`, `events/*`, schemas, contract, README -- carry and document `timezone` -- AC1
- [x] `tests/test_firetimes.py`, `tests/test_api_timezone.py`, integration tests -- pin the matrix, reference-check around transitions in 5 zones, live registry round trip

**Acceptance Criteria:**
- Given the registry, when this lands, then `candybars.timezone` exists (NOT NULL default `UTC`), is zoneinfo-validated at registration and PATCH, and the definitions shape gains `timezone` with the contract updated in the same commit.
- Given any caller, when it needs a fire instant, then it goes through `fire_times` (dispatcher tick match is "tick in fire_times(tick, tick+1m)"; API window/next, misfire latest-fire, skips use the same helper).
- Given New York, then the matrix DST rows hold exactly (pinned by tests), and an hourly cron is unaffected.
- Given `skip-rest-of-day`, then `date` is the candybar-local date across both transitions.
- Given any event, API response or stored timestamp, then it is UTC.

## Implementation Notes

- Reference check: `tests/test_firetimes.py` compares `fire_times` with an independent minute-by-minute Vixie reference around every 2026-27 transition of New_York, London, Lord_Howe (30-min DST), St_Johns (-3:30) and Dublin (negative DST), for 14 schedules.
- Decisions: hour field `*` or `*/1` is "wildcard" for the fold/gap rules; `localtime` and `Factory` are refused as zone names; skip-window reads are widened by one day each way (a local date differs from the UTC date by at most a day) and `SkipSet` decides precisely; `created` gains an optional `timezone` (additive; bloodbank canonical schema updated first, then the mirror); schedule rows do not gain `timezone`.
- Performance: next-fire lookups walk in chunks that stretch to the next wall fire, ~40-250 us per candybar (the 5.3 latency budget test still passes).
- Defensive: a stored zone that stops resolving leaves that one candybar out of dispatch (logged each tick) and reads as UTC in the read API, so one bad row cannot wedge the tick.

## Spec Change Log

## Review Triage Log

Layers (blind-hunter, edge-case-hunter, verification-gap) ran inline: no subagent capability in this session, and the build is non-interactive. Diff reviewed: everything since `baseline_commit`, untracked files included.

| # | Finding | Verdict | Evidence / route |
|---|---------|---------|------------------|
| 1 | A stream `updated` fact that changes `timezone` before the row commits computes `next_row` in the new zone but grouped skips with the old zone | medium | Real but narrow (the event-first window). Patched: `_refresh_current` groups skips after applying the overlay (`stream.py`) -- patch |
| 2 | A wall time that falls in a gap at local midnight (Sao Paulo 2018-11-04) could make `local_day_start` start the day at a non-existent instant | false | `_map_wall` returns the transition instant for a gap; pinned by `test_a_local_midnight_inside_a_gap_starts_the_day_at_the_transition` |
| 3 | `localtime` is listed by `available_timezones()` and would let the host's zone move a candybar's schedule | medium | Real. Patched: `resolve_timezone` refuses `localtime` and `Factory`; tested -- patch |
| 4 | next-fire cost grew from ~20 us to a chunked walk (the 5.3 latency budget regressed to 65 ms first) | medium | Real. Patched: 2-minute first chunk, 4x growth on empty chunks, and each empty chunk stretches to the next wall fire croniter already saw; 40-250 us per candybar and the budget test passes -- patch |
| 5 | `fire_times` validates eagerly but `fires_between` is a generator, so its errors surface on first iteration | false | Unchanged from the pre-7.1 behaviour (it was already a generator); `matches_tick` stays eager |
| 6 | Probing offsets every 2 days could miss two transitions inside one step | false | No tz database zone has an offset change and its reversal within two days after 1970; the reference test scans five zones including a 30-minute DST zone and negative-DST Dublin |
| 7 | `SkipSet.tz` and the `tz` argument of `next_fire` can disagree | low | Both come from one candybar in every call site (`group_skips(rows, zones)` is built from the same candybar list); rejected, fix would add a parameter-consistency guard |
| 8 | The `created` event gains `timezone` while consumers validate against the canonical schema | medium | Real contract change. Patched order: bloodbank canonical schema (additive, optional) committed first (`1d858f2`), mirror byte-identical here -- patch |
| 9 | Unresolvable stored zone wedges the tick for every candybar | high | Real. Patched: dispatcher leaves only that candybar out and logs it every tick; API reads it as UTC; tested (`test_dispatcher_leaves_out_a_candybar_whose_stored_zone_is_unknown`) -- patch |
| 10 | `skip-rest-of-day` rows for a local date can sit outside the UTC-date window the store reads | high | Real (04:30Z Nov 2 is local Nov 1). Patched: callers widen the skip read by one day each way; live registry test `test_rest_of_day_is_a_candybar_local_date_through_the_real_registry` -- patch |
| 11 | Older `created` facts (no `timezone`) fail the new model | false | `timezone` defaults to `UTC` on `CandybarCreatedData`; parsed old payloads pass (`test_events`) |
| 12 | No test runs the helper on CPython 3.13 (the container) | maybe-false | The venv is 3.14; settled by the live check after deploy (the same helper run inside the 3.13 image) |

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all pass (live-PG/NATS suites with CANDYBAR_TEST_PG/NATS against throwaway containers)
- `uv run ruff check src tests` -- expected: clean
