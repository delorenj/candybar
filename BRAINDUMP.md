---
schema-version: ''
vault-id: ''
asset-kind: ''
content-role: ''
specialist: ''
metadata-quality: ''
pillar: ''
triage-release: ''
wiki-candidate: ''
wiki-action: ''
decision-id: ''
category: ''
subcategory: ''
domain: ''
subdomain: ''
tags: ''
source: ''
captured: ''
route-path: ''
route-confidence: ''
route-reason: ''
pipeline-status: ''
classification: monolog
wax-item-id: doc-da05aa3094fe2561@doc-standard-Wax-4a02b5d4cb30
wax:
  passes:
    classification:
      state: completed
      at: '2026-10-26T01:04:27.816Z'
      version: 1
      command_id: a5f3275d-0cff-5074-8f8e-4b4a3fb702cb
      attempt: 1
modified: 2026-10-26 01:03:33
---

# Cron Overhaul
Currently, crons are scattered and distributed across different services. Hermes has its own cron framework. Some are just added to user `crontab`

This is an attempt to start from scratch and have a custom bloodbank-driven 33GOD cron system optimized the pipeline.

## Candybar - Braindump
- Publisher / Heartbeat style. 
    - Heartbeat event fired every minute
- Business logic defined as a cron Subscriber
- Any logic scheduled to be invoked is encapsulated in a `Candybar`
- A `Candybar` implements 
    - `invoke` 
    - `schedule`  (the standard cron schedule format)
    - `result` (success/fail)
- Every candybar `invoke` runtime is tracked via prometheus
    - total mem
    - total run time in ms
    - total token spend
    - avg CPU/GPU load
- Every candybar registry entry tracks metadata
    - total times invoked
    - total successful runs
- SDKs available for node, python, and rust
- Any executable can be registered as a Candybar
- Candybars are given a name, description, and can be tagged
- Full inventory of Candybars stored in registry (postgres db)
- Manually invoke any of them with `candybar run {candybarRegistryId}`
## Holocene Candybar View - Braindump

- Main view is similar to gmail inbox (layout-wise, not functionally)
    - Full-ish width rectangular box rows
    - "Current" row
        - next to fire
        - appears solid, full color, not translucent
    - All rows above current are previously invoked crons
        - grayed out, disabled-looking state (but not disabled)
    - All rows below are upcoming crons
        - all have countdowns indicating when they fire
    - Autoscrolls through, setting the next "current" row as the day progresses
- Easy toggle to hide "noisy" crons
    - any cron that repeats every <10m
    - can add individual cron (or batch) to the "noise" collection
- Two Named Tab Views
    - Definitions
        - View all cron definitions of the day (or any window of time)
        - Create new crons
    - Schedule
        - The main view
        - live scrolling filterable cron jobs
        - skip a cron
        - skip the rest of the day
