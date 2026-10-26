#!/bin/sh
# Container boot: apply registry migrations, then exec the daemon so it
# receives docker stop's SIGTERM directly (no shell in between).
set -e

alembic -c deploy/alembic.ini upgrade head
exec candybar daemon "$@"
