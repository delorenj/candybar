"""The candybar steward (steward-contract.md section 4).

A host-side role (`candybar daemon --roles steward`, unit
`deploy/host/candybar-steward.service`) that consumes
`bloodbank.cmd.cron.candybar.steward` and runs enrich / fix / evaluate /
refactor tasks through an OpenCode session. `model` holds the contract
shapes the API shares; the role lives in `role`.
"""
