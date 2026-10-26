# deploy

Placeholder — implemented by Story 1.9 (deployment envelope on the delo.sh stack).

One docker container composing all roles behind Traefik at
`candybar.delo.sh`, on the existing `proxy` network, with Prometheus scraping
`/metrics`. Secrets only as env vars holding `op://DeLoSecrets/...`
references; service references are `*.delo.sh` hostnames (AD-15).
