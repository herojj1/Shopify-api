"""
CardCheckout API — Server Entry Point + Combo Mode
Single endpoint that finds shops AND checks cards in one call.
"""
import os
import asyncio
import concurrent.futures
import functools
import json as _json
import logging
import random
import time
import threading
import urllib.parse
import urllib.request
from typing import Optional, Tuple, List

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from checkout_engine import (
    run_checkout_for_card,
    normalize_proxy,
    parse_card_entry,
)

# ── Logging ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cardcheckout.api")

# ── Configuration ──────────────────────────────────────────────────────
THREAD_WORKERS = int(os.environ.get("CHECKER_THREADS", "200"))
MAX_RETRIES    = int(os.environ.get("CHECKER_RETRIES", "1"))
CHECK_TIMEOUT  = float(os.environ.get("CHECK_TIMEOUT", "75"))

# Cloudflare worker that returns shop lists. Set this env var on Railway.
FETCHER_URL    = os.environ.get(
    "FETCHER_URL",
    "https://shopify-fetcher.YOUR-SUBDOMAIN.workers.dev",
)

_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=THREAD_WORKERS,
    thread_name_prefix="chk",
)

_active_checks      = 0
_active_checks_lock = threading.Lock()


def _inc_active():
    global _active_checks
    with _active_checks_lock:
        _active_checks += 1


def _dec_active():
    global _active_checks
    with _active_checks_lock:
        _active_checks -= 1


# ── FastAPI app ────────────────────────────────────────────────────────
app = FastAPI(
    title="CardCheckout API",
    version="3.0.0",
    description="Shopify card-check API + combo auto-search.",
    docs_url=None,
    redoc_url=None,
)


_DOCS_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>CardCheckout API v3</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet"/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#03030a;color:#f1f5f9;font-family:'Inter',sans-serif;line-height:1.6;min-height:100vh}
.wrap{max-width:920px;margin:0 auto;padding:40px 24px 80px}
h1{font-size:34px;font-weight:800;letter-spacing:-1px;margin-bottom:8px}
h1 span{background:linear-gradient(135deg,#a78bfa,#38bdf8);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
p.sub{color:#94a3b8;margin-bottom:32px}
.ep{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.07);border-radius:12px;margin-bottom:10px;overflow:hidden}
.ep-h{display:flex;gap:14px;padding:14px 18px;cursor:pointer;align-items:center}
.mtag{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;padding:3px 10px;border-radius:6px;min-width:52px;text-align:center}
.GET{background:rgba(16,185,129,.1);color:#34d399;border:1px solid rgba(16,185,129,.22)}
.POST{background:rgba(139,92,246,.1);color:#a78bfa;border:1px solid rgba(139,92,246,.22)}
.path{font-family:'JetBrains Mono',monospace;font-size:13px}
.desc{font-size:12px;color:#94a3b8;margin-left:auto}
.body{display:none;border-top:1px solid rgba(255,255,255,.07);padding:18px;background:rgba(0,0,0,.2)}
.ep.open .body{display:block}
input[type=text]{width:100%;background:rgba(0,0,0,.4);border:1px solid rgba(255,255,255,.13);border-radius:8px;padding:9px 12px;color:#f1f5f9;font-family:'JetBrains Mono',monospace;font-size:12.5px;outline:none;margin-bottom:10px}
input:focus{border-color:#8b5cf6}
button{background:linear-gradient(135deg,#7c3aed,#4f46e5);border:none;border-radius:8px;padding:10px 22px;color:#fff;font-weight:700;font-size:13px;cursor:pointer}
pre{background:rgba(0,0,0,.5);padding:14px;border-radius:8px;font-family:'JetBrains Mono',monospace;font-size:12px;overflow-x:auto;margin-top:14px;display:none;white-space:pre-wrap;max-height:400px;overflow-y:auto}
pre.show{display:block}
</style>
</head>
<body>
<div class="wrap">
<h1>CardCheckout <span>API v3</span></h1>
<p class="sub">Single-check + combo auto-search endpoints.</p>

<div class="ep" id="e1"><div class="ep-h" onclick="tog('e1')">
<span class="mtag GET">GET</span><span class="path">/health</span><span class="desc">Liveness</span>
</div><div class="body"><button onclick="go('health')">Send</button><pre id="r1"></pre></div></div>

<div class="ep" id="e2"><div class="ep-h" onclick="tog('e2')">
<span class="mtag POST">POST</span><span class="path">/check</span><span class="desc">Single store check</span>
</div><div class="body">
<input type="text" id="c1" placeholder="card: 4111111111111111|12|2026|123"/>
<input type="text" id="u1" placeholder="shop_url: https://store.com"/>
<input type="text" id="p1" placeholder="proxy: http://user:pass@host:port"/>
<button onclick="go('check')">Send</button><pre id="r2"></pre>
</div></div>

<div class="ep" id="e3"><div class="ep-h" onclick="tog('e3')">
<span class="mtag POST">POST</span><span class="path">/combo</span><span class="desc">Auto-search + check</span>
</div><div class="body">
<input type="text" id="c2" placeholder="card: 4111111111111111|12|2026|123"/>
<input type="text" id="k2" placeholder="keyword: socks"/>
<input type="text" id="p2" placeholder="proxy: http://user:pass@host:port"/>
<input type="text" id="m2" placeholder="max_sites: 20" value="20"/>
<button onclick="go('combo')">Send</button><pre id="r3"></pre>
</div></div>
</div>
<script>
function tog(id){document.getElementById(id).classList.toggle('open')}
async function go(t){
  try{
    let r,out;
    if(t==='health'){r=await fetch('/health');out=document.getElementById('r1');}
    else if(t==='check'){
      r=await fetch('/check',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({card:c1.value,shop_url:u1.value,proxy:p1.value,low:true})});
      out=document.getElementById('r2');
    } else {
      r=await fetch('/combo',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({card:c2.value,keyword:k2.value,proxy:p2.value,max_sites:parseInt(m2.value)||20})});
      out=document.getElementById('r3');
    }
    const j=await r.json();
    out.textContent=JSON.stringify(j,null,2);out.classList.add('show');
  }catch(e){alert(e.message);}
}
</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════════
# MODELS
# ══════════════════════════════════════════════════════════════════════
class CheckRequest(BaseModel):
    card:     Optional[str] = None
    shop_url: Optional[str] = None
    proxy:    Optional[str] = None
    low:      bool          = True


class CheckResponse(BaseModel):
    Response:    str  = "ERROR"
    CC:          str  = ""
    Price:       str  = ""
    Gate:        str  = "Shopify"
    Site:        str  = ""
    Charged:     str  = "False"
    status_code: str  = ""
    error:       str  = ""
    retryable:   bool = False
    receipt_url: str  = ""


class ComboRequest(BaseModel):
    card:        Optional[str]  = None
    keyword:     Optional[str]  = None
    site_list:   Optional[List[str]] = None
    proxy:       Optional[str]  = None
    max_sites:   int            = 20
    concurrency: int            = 10
    low:         bool           = True


class ComboItem(BaseModel):
    site:        str
    status:      str = "ERROR"
    status_code: str = ""
    amount:      str = ""
    error:       str = ""
    retryable:   bool = False
    receipt_url: str = ""


class ComboResponse(BaseModel):
    keyword:      str  = ""
    sites_found:  int  = 0
    sites_tested: int  = 0
    hits:         int  = 0
    charged:      int  = 0
    approved:     int  = 0
    declined:     int  = 0
    errors:       int  = 0
    elapsed:      float = 0.0
    results:      List[ComboItem] = []


# ══════════════════════════════════════════════════════════════════════
# VALIDATION
# ══════════════════════════════════════════════════════════════════════
def _validate_proxy(raw: str) -> Tuple[Optional[str], Optional[CheckResponse]]:
    if not raw or not raw.strip():
        return None, CheckResponse(
            Response="ERROR", status_code="PROXY_REQUIRED",
            error="proxy is required — e.g. http://user:pass@1.2.3.4:8080",
            retryable=False,
        )
    try:
        return normalize_proxy(raw), None
    except Exception as exc:
        return None, CheckResponse(
            Response="ERROR", status_code="PROXY_INVALID",
            error=f"Invalid proxy format: {exc}", retryable=False,
        )


def _validate_card(raw: str) -> Tuple[Optional[str], Optional[CheckResponse]]:
    import datetime as _dt
    if not raw or not raw.strip():
        return None, CheckResponse(
            Response="ERROR", status_code="CARD_REQUIRED",
            error="card is required — format: number|mm|yyyy|cvv",
            retryable=False,
        )
    try:
        _num, _m, _y, _c = parse_card_entry(raw)
    except Exception as exc:
        return None, CheckResponse(
            Response="ERROR", status_code="CARD_INVALID",
            error=f"invalid card format: {exc}", retryable=False,
        )
    now = _dt.datetime.utcnow()
    if _y < now.year or (_y == now.year and _m < now.month):
        return None, CheckResponse(
            Response="ERROR", status_code="CARD_EXPIRED",
            error=f"card expired: {_m:02d}/{_y}", retryable=False,
        )
    return raw.strip(), None


def _validate_url(raw: str) -> Tuple[Optional[str], Optional[CheckResponse]]:
    import urllib.parse as _up
    if not raw or not raw.strip():
        return None, CheckResponse(
            Response="ERROR", status_code="URL_REQUIRED",
            error="shop url is required", retryable=False,
        )
    url = raw.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        parsed = _up.urlparse(url)
        hostname = parsed.hostname or ""
        if not hostname or "." not in hostname or " " in hostname:
            raise ValueError(hostname)
    except Exception:
        return None, CheckResponse(
            Response="ERROR", status_code="URL_INVALID",
            error=f"invalid shop url: {raw!r}", retryable=False,
        )
    return url, None


def _build_response(res, shop_url: str = "") -> CheckResponse:
    name = res.status.name
    return CheckResponse(
        Response    = name,
        CC          = res.card or "",
        Price       = res.amount or "",
        Gate        = "Shopify",
        Site        = shop_url or res.shop_url or "",
        Charged     = "True" if name == "CHARGED" else "False",
        status_code = res.status_code or "",
        error       = str(res.error) if res.error else "",
        retryable   = res.retryable,
        receipt_url = res.receipt_url or "",
    )


# ══════════════════════════════════════════════════════════════════════
# FETCHER BRIDGE — calls the Cloudflare worker
# ══════════════════════════════════════════════════════════════════════
def _sync_fetch_sites(keyword: str, limit: int) -> list:
    """Blocking call to the CF worker. Runs inside the thread pool."""
    url = (
        f"{FETCHER_URL.rstrip('/')}/?"
        f"keyword={urllib.parse.quote(keyword)}&"
        f"result={limit}&format=raw"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "CardCheckout-Combo/3.0",
        "Accept":     "application/json",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        data = _json.loads(r.read().decode("utf-8"))

    if not isinstance(data, list):
        return []

    sites = []
    for item in data:
        s = (item or {}).get("site")
        if s:
            sites.append(s)
    return sites


async def _fetch_sites(keyword: str, limit: int) -> list:
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_pool, _sync_fetch_sites, keyword, limit),
            timeout=30,
        )
    except asyncio.TimeoutError:
        logger.warning(f"fetcher timed out for '{keyword}'")
        return []
    except Exception as e:
        logger.warning(f"fetcher call failed: {e}")
        return []


def _dedupe_sites(sites: list) -> list:
    seen = set()
    out = []
    for s in sites:
        try:
            host = urllib.parse.urlparse(s).hostname or s
        except Exception:
            host = s
        h = (host or "").lower()
        if h and h not in seen:
            seen.add(h)
            out.append(s if s.startswith("http") else f"https://{h}")
    return out


# ══════════════════════════════════════════════════════════════════════
# CORE CHECK RUNNER (used by /check and /combo)
# ══════════════════════════════════════════════════════════════════════
async def _run_check(shop_url: str, card: str, proxy_url: str, low: bool) -> CheckResponse:
    loop     = asyncio.get_running_loop()
    attempts = 1 + MAX_RETRIES
    last: Optional[CheckResponse] = None

    for attempt in range(1, attempts + 1):
        t0 = time.perf_counter()
        _inc_active()
        try:
            fn  = functools.partial(run_checkout_for_card, shop_url, card, proxy_url, low)
            res = await asyncio.wait_for(
                loop.run_in_executor(_pool, fn),
                timeout=CHECK_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("attempt %d/%d hard-timeout after %.0fs",
                           attempt, attempts, CHECK_TIMEOUT)
            last = CheckResponse(Response="ERROR", status_code="TIMEOUT",
                                 error=f"engine hard-timeout after {CHECK_TIMEOUT}s",
                                 retryable=True)
            continue
        except Exception as exc:
            logger.warning("attempt %d/%d exception: %s", attempt, attempts, exc)
            last = CheckResponse(Response="ERROR", error=str(exc), retryable=True)
            continue
        finally:
            _dec_active()

        resp = _build_response(res, shop_url)
        logger.info(
            "attempt %d/%d | status=%-8s code=%-24s elapsed=%.1fs",
            attempt, attempts, resp.Response, resp.status_code or "-",
            time.perf_counter() - t0,
        )
        if resp.Response in ("CHARGED", "APPROVED"):
            logger.info("HIT | status=%s | amount=%s | site=%s",
                        resp.Response, resp.Price, shop_url)

        if not resp.retryable or attempt == attempts:
            return resp

        backoff = min(2.0, 0.4 * (2 ** (attempt - 1))) + (random.random() * 0.3)
        logger.info("retrying in %.2fs…", backoff)
        await asyncio.sleep(backoff)
        last = resp

    return last  # type: ignore


# ══════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════
@app.get("/", include_in_schema=False)
async def root():
    return HTMLResponse(_DOCS_HTML)


@app.get("/docs", include_in_schema=False)
async def custom_docs():
    return HTMLResponse(_DOCS_HTML)


@app.get("/health", tags=["meta"])
async def health():
    with _active_checks_lock:
        active = _active_checks
    return {
        "ok":            True,
        "threads":       THREAD_WORKERS,
        "retries":       MAX_RETRIES,
        "timeout":       CHECK_TIMEOUT,
        "active_checks": active,
        "fetcher_url":   FETCHER_URL,
        "combo_ready":   "YOUR-SUBDOMAIN" not in FETCHER_URL,
    }


# ── single check ──────────────────────────────────────────────────────
@app.get("/check", response_model=CheckResponse, tags=["check"])
async def check_get(
    card:  str = Query(..., description="number|mm|yyyy|cvv"),
    url:   str = Query(..., description="Shopify store URL"),
    proxy: str = Query(..., description="http://user:pass@host:port"),
    low:   str = Query(default="true"),
):
    card_val, err = _validate_card(card)
    if err:
        err.CC = card
        return err

    url_val, err = _validate_url(url)
    if err:
        err.CC = card
        err.Site = url
        return err

    proxy_val, err = _validate_proxy(proxy)
    if err:
        err.CC = card
        err.Site = url_val
        return err

    low_mode = low.strip().lower() in ("1", "true", "yes")
    try:
        return await _run_check(url_val, card_val, proxy_val, low_mode)
    except Exception as exc:
        logger.exception("unhandled error in _run_check (GET)")
        return CheckResponse(Response="ERROR", status_code="INTERNAL",
                             error=f"internal error: {exc}", retryable=True)


@app.post("/check", response_model=CheckResponse, tags=["check"])
async def check_post(req: CheckRequest):
    raw_card = req.card or ""
    raw_url  = req.shop_url or ""

    card_val, err = _validate_card(raw_card)
    if err:
        err.CC = raw_card
        return err

    url_val, err = _validate_url(raw_url)
    if err:
        err.CC   = raw_card
        err.Site = raw_url
        return err

    proxy_val, err = _validate_proxy(req.proxy or "")
    if err:
        err.CC   = raw_card
        err.Site = url_val
        return err

    try:
        return await _run_check(url_val, card_val, proxy_val, req.low)
    except Exception as exc:
        logger.exception("unhandled error in _run_check (POST)")
        return CheckResponse(Response="ERROR", status_code="INTERNAL",
                             error=f"internal error: {exc}", retryable=True)


# ── combo: auto-search + check ────────────────────────────────────────
async def _run_combo(req: ComboRequest) -> ComboResponse:
    t0 = time.time()

    card_val, err = _validate_card(req.card or "")
    if err:
        return ComboResponse(errors=1, results=[ComboItem(site="", error=str(err.error))])

    proxy_val, err = _validate_proxy(req.proxy or "")
    if err:
        return ComboResponse(errors=1, results=[ComboItem(site="", error=str(err.error))])

    # ── where do the sites come from? ─────────────────────────────
    raw_sites: list = []
    keyword = req.keyword or ""

    if req.site_list:
        for u in req.site_list:
            v, _ = _validate_url(u)
            if v:
                raw_sites.append(v)
    elif keyword:
        # over-fetch to allow for dedupe; cap at 5× max_sites
        raw_sites = await _fetch_sites(keyword, min(req.max_sites * 5, 500))
    else:
        return ComboResponse(errors=1,
                             results=[ComboItem(site="", error="keyword or site_list required")])

    sites_found = len(raw_sites)

    unique = _dedupe_sites(raw_sites)
    unique = unique[: max(1, min(req.max_sites, 100))]

    logger.info(f"combo: keyword={keyword!r} found={sites_found} unique={len(unique)}")

    if not unique:
        return ComboResponse(
            keyword=keyword, sites_found=sites_found,
            elapsed=round(time.time() - t0, 2),
        )

    # ── parallel checks with concurrency cap ─────────────────────
    concurrency = max(1, min(req.concurrency, 50))
    sem = asyncio.Semaphore(concurrency)

    async def bounded(url: str):
        async with sem:
            try:
                return await _run_check(url, card_val, proxy_val, req.low)
            except Exception as e:
                return CheckResponse(Response="ERROR", error=str(e), retryable=True)

    tasks   = [bounded(u) for u in unique]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    elapsed = round(time.time() - t0, 2)

    out: List[ComboItem] = []
    counters = {"hits": 0, "charged": 0, "approved": 0, "declined": 0, "errors": 0}

    for url, r in zip(unique, results):
        if isinstance(r, Exception):
            out.append(ComboItem(site=url, status="ERROR", error=str(r)[:100]))
            counters["errors"] += 1
            continue

        st = r.Response
        out.append(ComboItem(
            site        = url,
            status      = st,
            status_code = r.status_code or "",
            amount      = r.Price or "",
            error       = r.error or "",
            retryable   = r.retryable,
            receipt_url = r.receipt_url or "",
        ))

        if st == "CHARGED":
            counters["charged"] += 1; counters["hits"] += 1
        elif st == "APPROVED":
            counters["approved"] += 1; counters["hits"] += 1
        elif st == "DECLINED":
            counters["declined"] += 1
        else:
            counters["errors"] += 1

    return ComboResponse(
        keyword      = keyword,
        sites_found  = sites_found,
        sites_tested = len(unique),
        hits         = counters["hits"],
        charged      = counters["charged"],
        approved     = counters["approved"],
        declined     = counters["declined"],
        errors       = counters["errors"],
        elapsed      = elapsed,
        results      = out,
    )


@app.post("/combo", response_model=ComboResponse, tags=["combo"])
async def combo_post(req: ComboRequest):
    try:
        return await _run_combo(req)
    except Exception as exc:
        logger.exception("unhandled error in _run_combo (POST)")
        return ComboResponse(errors=1,
                             results=[ComboItem(site="", error=str(exc)[:100])])


@app.post("/auto", response_model=ComboResponse, tags=["combo"])
async def auto_post(req: ComboRequest):
    """Alias for /combo — same behavior."""
    return await combo_post(req)


@app.get("/combo", response_model=ComboResponse, tags=["combo"])
async def combo_get(
    card:      str = Query(...),
    keyword:   str = Query(...),
    proxy:     str = Query(...),
    max_sites: int = Query(default=20),
    low:       str = Query(default="true"),
):
    return await combo_post(ComboRequest(
        card=card, keyword=keyword, proxy=proxy,
        max_sites=max_sites,
        low=low.strip().lower() in ("1", "true", "yes"),
    ))


# ── shutdown ──────────────────────────────────────────────────────────
@app.on_event("shutdown")
async def _shutdown():
    logger.info("shutting down thread pool…")
    _pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    logger.info("CardCheckout API v3 — port=%d threads=%d fetcher=%s",
                port, THREAD_WORKERS, FETCHER_URL)
    uvicorn.run(
        app, host="0.0.0.0", port=port,
        log_level="warning", backlog=2048,
        limit_concurrency=THREAD_WORKERS * 2,
        timeout_keep_alive=60,
    )