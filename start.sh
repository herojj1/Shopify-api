#!/usr/bin/env bash
# Shopify captcha-solving API — start script.
#
# Required env:
#   NOCAPTCHA_API_KEY   your nocaptchaai.com key
#
# Optional env:
#   PORT                default 5000
#   CAPTCHA_TIMEOUT     default 120 (seconds per captcha solve)
#   CHROME_BINARY       full path to chrome/chromium (auto-detected if unset)
#   CHROMEDRIVER_PATH   full path to chromedriver (auto-detected if unset)

set -e

: "${NOCAPTCHA_API_KEY:?NOCAPTCHA_API_KEY is required}"
export NOCAPTCHA_API_KEY

PORT="${PORT:-5000}"
CAPTCHA_TIMEOUT="${CAPTCHA_TIMEOUT:-120}"

echo "[start] port=$PORT  captcha_timeout=${CAPTCHA_TIMEOUT}s"
python3 -u shopify_captcha_api.py
