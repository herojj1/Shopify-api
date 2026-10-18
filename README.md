# CardCheckout API

Merged Shopify checkout engine. Auto-harvests $0.10–$3.00 stores.
Works with or without proxy. Skips captcha-protected sites automatically.

## Install & Run

    pip install -r requirements.txt
    PORT=8081 python3 api_server.py

## Endpoints

### GET / — reference format

    curl "http://localhost:8081/?4031630721650843|06|2029|910&proxy=196.244.48.124:12345:naveed:Qwerty_123ABC"

Auto-picks a live $0-3 store. Add `&url=https://store.myshopify.com` to force one.
Omit `&proxy=` to run without a proxy.

### GET /check — standard format

    curl "http://localhost:8081/check?cc=4031630721650843|06|2029|910&proxy=..."

### POST /check

    curl -X POST http://localhost:8081/check \
      -H 'Content-Type: application/json' \
      -d '{"cc":"4031630721650843|06|2029|910","proxy":"..."}'

### Pool tools

    curl http://localhost:8081/sites
    curl http://localhost:8081/sites/harvest
    curl http://localhost:8081/sites/captcha/clear
    curl http://localhost:8081/health

## Response

    {
      "cc":       "4031630721650843|06|2029|910",
      "Gateway":  "Shopify Payments",
      "Response": "ORDER_PLACED",
      "Price":    8.99,
      "Currency": "USD",
      "Brand":    "MASTERCARD",
      "Issuer":   "KB KOOKMIN CARD CO., LTD",
      "Country":  "SOUTH KOREA 🇰🇷",
      "Status":   true,
      "Proxy":    "Live",
      "Time":     "11.99s"
    }

## Harvester

- **Price range:** $0.10–$3.00 per product
- **Sources:** Bing RSS (primary), DuckDuckGo fallback
- **Captcha-aware:** sites returning `CAPTCHA_REQUIRED` are excluded from the pool
- **Cycle:** every 15 min, ~10 keywords per cycle
- **Persists** to `site_cache.json` / `harvest_state.json`
- **Manual seeds:** add to `seeds.txt`

## Env

| Var | Default | Purpose |
|---|---|---|
| `PORT` | `8081` | Listen port |
| `CHECKER_THREADS` | `100` | Check worker pool |
| `CHECKER_TIMEOUT` | `120` | Per-check hard timeout |
| `HARVEST_INTERVAL_SECS` | `900` | Harvest cycle interval |
| `HARVEST_MAX_PRICE` | `3.00` | Max product price accepted |
| `HARVEST_MIN_PRICE` | `0.10` | Min product price accepted |
| `SITE_POOL_MAX` | `800` | Max sites kept in memory |
