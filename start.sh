#!/usr/bin/env bash
# CardCheckout API launcher
set -e

export CHECKER_THREADS="${CHECKER_THREADS:-100}"
export CHECKER_RETRIES="${CHECKER_RETRIES:-1}"
export CHECKER_TIMEOUT="${CHECKER_TIMEOUT:-120}"
export PORT="${PORT:-8081}"

export HARVEST_INTERVAL_SECS="${HARVEST_INTERVAL_SECS:-900}"
export HARVEST_MAX_PRICE="${HARVEST_MAX_PRICE:-5.00}"
export HARVEST_MIN_PRICE="${HARVEST_MIN_PRICE:-0.10}"
export HARVEST_PAGES_PER_KEYWORD="${HARVEST_PAGES_PER_KEYWORD:-3}"
export HARVEST_VALIDATE_WORKERS="${HARVEST_VALIDATE_WORKERS:-24}"
export HARVEST_REVALIDATE_HOURS="${HARVEST_REVALIDATE_HOURS:-24}"
export SITE_POOL_MAX="${SITE_POOL_MAX:-800}"

exec python3 -u -m uvicorn api_server:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --workers 1 \
  --timeout-keep-alive 60 \
  --log-level warning
