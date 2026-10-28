# n8n-runner canary

`deploy-smoke-n8n` proves the `n8n` runner end to end every 15 minutes: the
executor POSTs to `https://n8n.delo.sh/webhook/candybar/canary`, and the
workflow answers through a Respond to Webhook node with

```json
{"candybar": {"status": "success", "message": "n8n canary ok (scheduled)", "tokenSpend": 0}}
```

Send `data = { fail = true }` (e.g. a one-off copy of the manifest) and it
answers `status: "fail"` instead, which proves the failure path.

Recreate the workflow (it is also exported by delonet-n8n-workflows):

```bash
K=$(op read "op://DeLoSecrets/5pyy6ct5pi7eaz436g2qkn6pjq/api key")
ID=$(curl -s -H "X-N8N-API-KEY: $K" -H 'content-type: application/json' \
  -d @deploy/smoke/n8n/canary-workflow.json https://n8n.delo.sh/api/v1/workflows | jq -r .id)
curl -s -X POST -H "X-N8N-API-KEY: $K" https://n8n.delo.sh/api/v1/workflows/$ID/activate
candybar register deploy/smoke/n8n
```
