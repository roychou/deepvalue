#!/usr/bin/env bash
#
# Tedium Premium app entrypoint. Ensures the state dirs exist on the mounted volumes, then:
#   - no args  -> run supercronic (the scheduled weekly session + daily watchdog), or
#   - args     -> run them directly, e.g. a one-off manual/preview session:
#       docker compose run --rm app python -m deepvalue.forward.run --as-of "$(date +%F)" --execute ibkr
set -euo pipefail

mkdir -p /app/data/forward/logs /app/data/cache/edgar

if [ "$#" -gt 0 ]; then
    exec "$@"
fi
exec supercronic /app/deploy/app/crontab
