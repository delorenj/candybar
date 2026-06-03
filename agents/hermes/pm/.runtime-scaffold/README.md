# {{agent_id}} — runtime

This is the HERMES_HOME for the **{{display_name}}** agent. It is git-tracked
so memory, SOUL evolution, and conversation history are durable across machines
and recoverable on failure.

## What's in here

| Path | Tracked? | Purpose |
| --- | --- | --- |
| `config.yaml` | yes | Inference + skill config (cloned from global at provision time) |
| `SOUL.md` | yes | The agent's personality, evolves over time |
| `memories/MEMORY.md` | yes | The condensed mental-model summary loaded each session |
| `memories/USER.md` | yes | The operator's persona (Jarad DeLorenzo, ...) |
| `sessions/sessions.db` | yes (LFS) | SQLite store of every conversation |
| `bloodbank-consumer.py` | yes | NATS subscriber for repo-scoped events |
| `decisions/` | yes | Agent-emitted decisions, one file per important call |
| `.env` | **no** | API keys + Telegram bot token (per-machine secret) |
| `auth.json` | **no** | Deprecated local OAuth store; fleet auth defaults to `HERMES_OAUTH_FILE=~/.hermes/auth.json` |
| `audio_cache/`, `image_cache/` | **no** | Regenerable caches |
| `sandboxes/` | **no** | Per-session ephemeral execution dirs |
| `bloodbank-inbox/` | **no** | Inbox queue for incoming bloodbank events |

## Checkpoint cadence

- Hourly: systemd `--user` timer `hermes-{{agent_id}}-checkpoint.timer`
- On session end: hermes Stop hook (TODO: hook script in `~/.hermes/hooks/`)

The checkpoint script lives in the parent's `.scripts/checkpoint.sh`. It
`git add -A`, commits only if dirty, and pushes to `origin`.

## Restoring on a new machine

```bash
cd /path/to/parent-project
git submodule update --init --recursive
git -C agents/hermes/{{role}}/runtime lfs pull
# Provide the per-runtime secrets the submodule deliberately excluded:
cp ~/path/to/your/.env       agents/hermes/{{role}}/runtime/.env
# OAuth provider credentials are shared across the fleet. If needed, login once:
agents/hermes/{{role}}/hermes auth add openai-codex
agents/hermes/{{role}}/hermes status
```

## DO NOT manually edit on multiple machines

This repo is a single-writer system. Either run the agent on machine A or
machine B — not both. The checkpoint commit will fast-forward; concurrent
edits cause merge pain. If you really need to fork the agent (e.g. for
experimentation), branch this repo.
