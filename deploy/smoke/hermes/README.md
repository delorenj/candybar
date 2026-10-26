# hermes-runner canary (Story 7.4)

`deploy-smoke-hermes` triggers a Hermes job through the real
`hermes -p default cron run <id> --wait` path. The job is a no-agent script
pinned `scheduler: external`, with a yearly natural schedule so a gateway that
has not yet restarted onto the `scheduler=external` code never fires it itself.

Install (once per host; the job id goes into `candybar.toml`):

```bash
cp deploy/smoke/hermes/candybar-hermes-canary.sh ~/.hermes/scripts/
hermes cron create "0 0 1 1 *" --no-agent --script candybar-hermes-canary.sh \
  --name candybar-hermes-canary --deliver local --scheduler external
# put the printed job id in candybar.toml's job_id, then:
candybar register deploy/smoke/hermes
```

It never delivers anywhere and spends no tokens. After a redeploy,
`candybar run <id>` must record a `success` with real memory and CPU.
