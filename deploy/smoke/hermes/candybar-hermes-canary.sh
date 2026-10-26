#!/bin/sh
# Canary for the hermes runner (Story 7.4): the Hermes no-agent job behind
# deploy-smoke-hermes. No agent, no side effects: one line to stdout, saved
# locally by Hermes (deliver=local).
echo "candybar hermes canary ok $(date -u +%FT%TZ)"
