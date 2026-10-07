# Shopify CAPTCHA-REQUIRED Solver API

Wraps the full Shopify checkout flow and auto-solves `CAPTCHA_REQUIRED`
challenges with NoCaptchaAI + a headless browser. Returns a normal
charged / approved / declined / error JSON, plus a `CaptchaSolved` flag.

## Files

| file                    | purpose                                                   |
|-------------------------|-----------------------------------------------------------|
| `shopify_captcha_api.py`| Flask API + aiohttp checkout engine + captcha wiring      |
| `recaptcha_solver.py`   | Selenium + NoCaptchaAI solver (source: your own file)     |
| `_gql_docs.py`          | The 4 GraphQL documents pasted from your original api.py  |
| `requirements.txt`      | Python deps                                               |
| `start.sh`              | Env-validating launcher                                   |

## Setup

```bash
pip install -r requirements.txt

# Chrome + chromedriver must be on PATH, or set these:
export CHROME_BINARY=/path/to/chrome
export CHROMEDRIVER_PATH=/path/to/chromedriver

export NOCAPTCHA_API_KEY=your_key_here
export PORT=5000
