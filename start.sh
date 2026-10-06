#!/bin/bash
set -e

export CHECKER_THREADS="${CHECKER_THREADS:-200}"
export CHECKER_RETRIES="${CHECKER_RETRIES:-1}"
export CHECK_TIMEOUT="${CHECK_TIMEOUT:-75}"

export CAPTCHA_ENABLED="${CAPTCHA_ENABLED:-1}"
export CAPTCHA_AUTO_SOLVE="${CAPTCHA_AUTO_SOLVE:-1}"
export CAPTCHA_RETRIES="${CAPTCHA_RETRIES:-2}"
export CAPTCHA_POLL_SEC="${CAPTCHA_POLL_SEC:-3}"
export CAPTCHA_MAX_WAIT="${CAPTCHA_MAX_WAIT:-120}"
export CAPTCHA_STORE="${CAPTCHA_STORE:-captcha_state.json}"

# paid provider fallback (leave blank to skip)
# export CAPTCHA_SERVICE=capsolver
# export CAPTCHA_API_KEY=CAP-xxxxxxxxxxxxxxxx

exec python3 -u api.py
