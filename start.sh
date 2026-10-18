#!/usr/bin/env bash
set -e
export CHECKER_THREADS="${CHECKER_THREADS:-100}"
export CHECKER_RETRIES="${CHECKER_RETRIES:-1}"
export CHECKER_TIMEOUT="${CHECKER_TIMEOUT:-120}"
export PORT="${PORT:-8081}"
export HARVEST_INTERVAL_SECS="${HARVEST_INTERVAL_SECS:-900}"
export HARVEST_MAX_PRICE="${HARVEST_MAX_PRICE:-3.00}"
export HARVEST_MIN_PRICE="${HARVEST_MIN_PRICE:-0.10}"
export SITE_POOL_MAX="${SITE_POOL_MAX:-800}"
exec python3 -u -m uvicorn api_server:app \
  --host 0.0.0.0 --port "$PORT" --workers 1 \
  --timeout-keep-alive 60 --log-level warning
