# USER.md — Jarad DeLorenzo

The operator behind this agent. Loaded into context at session start so the
agent doesn't have to re-discover who it's working with.

## Identity

- Name: Jarad DeLorenzo
- Email: jaradd@gmail.com (personal), jarad@automaticai.io (business)
- GitHub: `delorenj`
- Telegram: see `TELEGRAM_ALLOWED_USERS` in this agent's `.env`
- Role: One-man dev team running the 33GOD ecosystem and delonet homelab

## How Jarad works

- Pivots fast — instructions and architecture choices can flip between
  sessions. Trust *current* code and `git log` over older documentation.
- Prefers terse responses, decision-forward. No throat-clearing. No
  "I'll help you with that". When unsure, asks one specific question, not
  three vague ones.
- Wants agents to act, then report. Not act, then ask if it was OK.
- Uses Hindsight for durable memory. Expects agents to retain useful learnings
  themselves rather than waiting to be asked.
- Tracks confidence with `llr` (= `fdfind --type f --hidden --exclude .git -X
  ls -lt --time=ctime -r`). Newer mtime = more authoritative.

## Tooling he ships with

- Copier (`gh:delorenj/CommonProject`) for new project bootstrap
- BMAD method (`_bmad/`, `_bmad_output/`) for spec-driven work
- Plane (`plane.delo.sh`) for ticket tracking — workspaces: 33god,
  lasertoast, intelliforia
- Bloodbank for cross-agent event flow
- Hindsight for shared memory (banks: per-repo, `infra`, `33GOD`)
- 1Password CLI (`op`) for all secrets
- mise for env management

## What he hates

- Stale documentation that contradicts current state
- Manual setup steps that should be scripted
- Backwards-compatibility shims for code that just shipped
- Long preambles before the actual answer
- Hardcoded paths/IPs that break on the next machine

## What he loves

- Single source of truth (Holyfields-style schema registries)
- Reproducible bootstrap (Copier + provision scripts)
- Auto-checkpointed state (this very runtime repo!)
- Agents that emit Bloodbank events so the rest of the system can react
