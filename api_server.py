import os
import asyncio
import concurrent.futures
import functools
import logging
import random
import time
import threading
from typing import Optional, Tuple

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
    version="2.5.0",
    description="Shopify card-check API.",
    docs_url=None,
    redoc_url=None,
)


_DOCS_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>CardCheckout API</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet"/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#03030a;color:#f1f5f9;font-family:'Inter',sans-serif;line-height:1.6;min-height:100vh}
.wrap{max-width:880px;margin:0 auto;padding:40px 24px 80px}
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
button:hover{transform:translateY(-1px)}
pre{background:rgba(0,0,0,.5);padding:14px;border-radius:8px;font-family:'JetBrains Mono',monospace;font-size:12px;overflow-x:auto;margin-top:14px;display:none;white-space:pre-wrap}
pre.show{display:block}
</style>
</head>
<body>
<div class="wrap">
<h1>CardCheckout <span>API</span></h1>
<p class="sub">Shopify checkout verification engine.</p>

<div class="ep" id="e1"><div class="ep-h" onclick="tog('e1')">
<span class="mtag GET">GET</span><span class="path">/health</span><span class="desc">Liveness</span>
</div><div class="body">
<button onclick="go('health')">Send</button>
<pre id="r1"></pre>
</div></div>

<div class="ep" id="e2"><div class="ep-h" onclick="tog('e2')">
<span class="mtag GET">GET</span><span class="path">/check</span><span class="desc">Query params</span>
</div><div class="body">
<input type="text" id="gc" placeholder="card: 4111111111111111|12|2026|123"/>
<input type="text" id="gu" placeholder="url: https://store.myshopify.com"/>
<input type="text" id="gp" placeholder="proxy: http://user:pass@host:port"/>
<button onclick="go('get')">Send</button>
<pre id="r2"></pre>
</div></div>

<div class="ep" id="e3"><div class="ep-h" onclick="tog('e3')">
<span class="mtag POST">POST</span><span class="path">/check</span><span class="desc">JSON body</span>
</div><div class="body">
<input type="text" id="pc" placeholder="card: 4111111111111111|12|2026|123"/>
<input type="text" id="pu" placeholder="shop_url: https://store.myshopify.com"/>
<input type="text" id="pp" placeholder="proxy: http://user:pass@host:port"/>
<button onclick="go('post')">Send</button>
<pre id="r3"></pre>
</div></div>

</div>
<script>
function tog(id){document.getElementById(id).classList.toggle('open')}
async function go(t){
  try{
    let r,out;
    if(t==='health'){r=await fetch('/health');out=document.getElementById('r1');}
    else if(t==='get'){
      const p=new URLSearchParams({card:gc.value,url:gu.value,proxy:gp.value,low:'true'});
      r=await fetch('/check?'+p);out=document.getElementById('r2');
    } else {
      r=await fetch('/check',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({card:pc.value,shop_url:pu.value,proxy:pp.value,low:true})});
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


# ── Models ─────────────────────────────────────────────────────────────
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


# ── Validation ─────────────────────────────────────────────────────────
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


# ── Routes ─────────────────────────────────────────────────────────────
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
    }


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


@app.on_event("shutdown")
async def _shutdown():
    logger.info("shutting down thread pool…")
    _pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    logger.info("CardCheckout API — port=%d threads=%d retries=%d",
                port, THREAD_WORKERS, MAX_RETRIES)
    uvicorn.run(
        app, host="0.0.0.0", port=port,
        log_level="warning", backlog=2048,
        limit_concurrency=THREAD_WORKERS * 2,
        timeout_keep_alive=60,
    )
