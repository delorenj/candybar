# Stitch Loop: Candybar Main Desktop Panel

Date: 2026-05-31
Operator: Jarad

## Inputs grounded from sibling repos

1) Bloodbank requires explicit kind-aware routing and strict contract visibility
- type: `bloodbank.v1.<domain>.<entity>.<action>`
- subject: `bloodbank.<evt|cmd|rpy>.v1.<domain>.<entity>.<action>`
- event envelopes require `ordering_key`
- command/reply lifecycle is first-class

2) Candystore already solves event summarization and session narrative
- `/events` + `/events/:id/summary` for feed and detail
- `/sessions/:id` + `/sessions/:id/summary` for correlation-centric timeline
- `summary/heatmap` + by-cli/by-project for aggregate context

3) Current Candybar implementation strengths
- Live stream subscription from NATS works
- Prefix-tree filters (log/sound/mute) are practical
- Fast, minimal, no framework bloat in UI layer

4) Current Candybar implementation gaps
- No command/reply lane (only `bloodbank.evt.>` currently subscribed)
- No correlation/session-first workflow
- No contract violation lane
- Details are raw-only; not summarized by task/session semantics

## Loop 1: Divergent variants

- 001-ops-overview: triage-first operational dashboard
- 002-session-investigator: correlation-first forensic view
- 003-contract-guard: envelope/schema conformance guardrail

## Loop 2: Evaluation matrix

| Dimension | 001 Ops overview | 002 Session investigator | 003 Contract guard |
|---|---|---|---|
| Live awareness | High | Medium | Low-Medium |
| Session debugging | Medium | High | Low |
| Contract hygiene | Medium | Medium | High |
| Cognitive load | Medium | Medium | Low-Medium |
| Incident triage speed | High | Medium | High for schema incidents |

## Recommended direction

Hybrid shell:
- Default mode: 001 Ops overview
- Right-side workflow tabs:
  - Investigate (embed 002 detail/timeline behaviors)
  - Contract (embed 003 violation inspector)

This keeps fast situational awareness while making deep investigation and contract hardening one click away.

## Concrete next implementation slice

1) Expand stream subscriber from `bloodbank.evt.>` to include command/reply subjects
2) Add event kind chips (event/command/reply) and kind filter toggles
3) Add detail drawer tabs: Summary / Raw / Related
4) Add correlation quick-open to session timeline
5) Add contract checks panel for:
   - subject-kind mismatch
   - missing ordering_key on events
   - snake_case extension aliases

## Loop 3: Live notifier overhaul (2026-06-01)

Objective from operator:
- exhaustive hierarchical multi-select for notification types
- global sound toggle
- rolling live list from selected types only
- no historical backfill

Taste/stitch decisions applied:
- Shell and visual language inherit from `001-ops-overview` (triage-first)
- Right inspector keeps `002`/`003` behaviors as lightweight tabs (Investigate/Contract)
- Left taxonomy panel upgraded from prefix toggles to exhaustive hierarchical type tree
- Feed explicitly marked live-only; selection changes reset visible feed to avoid historical carryover

Implementation mapping:
- Frontend layout + controls: `index.html`
- Hierarchical type tree, sound toggle, rolling live feed: `src/main.js`
- Schema root discovery hardening + broad bloodbank stream coverage: `src-tauri/src/lib.rs`

## Artifacts

- `001-ops-overview/index.html`
- `002-session-investigator/index.html`
- `003-contract-guard/index.html`
