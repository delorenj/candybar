"""Cron-migration support (Story 7.5, AD-21): the ledger and `candybar migrate`.

`ledger` holds the committed record of every migrated job, `native` probes and
drives the scheduler a job is leaving (systemd timer, user crontab line,
Hermes ticker), `client` is the REST client of the api role, and `ops` is the
five verbs: plan, cutover, verify, rollback, status. Nothing here touches the
registry directly (AD-10): every candybar change goes through the API.
"""
