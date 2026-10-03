#!/bin/bash
export CHECKER_THREADS="${CHECKER_THREADS:-200}"
export CHECKER_RETRIES="${CHECKER_RETRIES:-1}"
export CHECK_TIMEOUT="${CHECK_TIMEOUT:-75}"

# ── captcha solver ─────────────────────────────────────────────────
export CAPTCHA_ENABLED="${CAPTCHA_ENABLED:-1}"
export CAPTCHA_RETRIES="${CAPTCHA_RETRIES:-2}"

# optional paid fallback
# export CAPTCHA_SERVICE=capsolver
# export CAPTCHA_API_KEY=CAP-xxxxxxxxxxxxxxxx

exec python3 -u -m uvicorn api_server:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers 1 \
  --timeout-keep-alive 60 \
  --backlog 2048 \
  --limit-concurrency 400 \
  --log-level warning
