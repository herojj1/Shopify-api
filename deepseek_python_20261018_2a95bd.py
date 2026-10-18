"""
site_fetcher.py — Shopify site discovery + pool.

* Keeps only stores with at least one variant priced MIN_PRICE–MAX_PRICE ($0.10–$3.00).
* Seeds are validated in small batches (SEEDS_PER_CYCLE) to avoid 429 floods.
* If HARVEST_PROXY_URL is set, all discovery/validation uses that proxy.
* Sites that require CAPTCHA at checkout are marked and excluded from pick_site().
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("sites")

# ── paths ──────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
CACHE_FILE    = BASE_DIR / "site_cache.json"
STATE_FILE    = BASE_DIR / "harvest_state.json"
KEYWORDS_FILE = BASE_DIR / "keywords.txt"
SEEDS_FILE    = BASE_DIR / "seeds.txt"

# ── config ─────────────────────────────────────────────────────────────────
MAX_PRICE         = float(os.environ.get("HARVEST_MAX_PRICE", "3.00"))
MIN_PRICE         = float(os.environ.get("HARVEST_MIN_PRICE", "0.10"))
POOL_MAX          = int(os.environ.get("SITE_POOL_MAX", "800"))
HARVEST_INTERVAL  = int(os.environ.get("HARVEST_INTERVAL_SECS", "900"))
PAGES_PER_KEYWORD = int(os.environ.get("HARVEST_PAGES_PER_KEYWORD", "3"))
VALIDATE_WORKERS  = int(os.environ.get("HARVEST_VALIDATE_WORKERS", "8"))
REVALIDATE_HOURS  = int(os.environ.get("HARVEST_REVALIDATE_HOURS", "24"))
SEEDS_ONLY        = os.environ.get("SEEDS_ONLY", "").lower() in ("1", "true", "yes")
DEBUG_DISCOVERY   = os.environ.get("HARVEST_DEBUG", "1").lower() in ("1", "true", "yes")
SEEDS_PER_CYCLE   = int(os.environ.get("HARVEST_SEEDS_PER_CYCLE", "40"))
SCRAPE_PER_CYCLE  = int(os.environ.get("HARVEST_SCRAPE_PER_CYCLE", "100"))

# Optional proxy used for ALL harvester requests (discovery + validation).
# Without it, seed-heavy runs get 429-flooded from Render's datacenter IP.
HARVEST_PROXY = (os.environ.get("HARVEST_PROXY_URL", "") or "").strip() or None

# ── state ──────────────────────────────────────────────────────────────────
_LOCK      = threading.Lock()
_POOL: list[str]             = []
_CAPTCHA_SITES: set[str]     = set()
_FAIL: dict[str, int]        = {}
_LAST_USE: dict[str, float]  = {}
_LAST_GOOD: dict[str, float] = {}
_SEEN: set[str]              = set()
_SEEDS_VALIDATED: set[str]   = set()   # seeds processed this process (won't repeat)
_RR = 0
_STATS = {
    "candidates_seen":    0,
    "candidates_tested":  0,
    "candidates_kept":    0,
    "candidates_dropped": 0,
    "captcha_marked":     0,
    "payment_blocked":    0,
    "rate_limited":       0,
    "last_cycle_ts":      0,
    "last_cycle_found":   0,
    "cycles":             0,
    "discover_hits":      {"seeds": 0, "bing_rss": 0, "ddg_html": 0, "ddg_lite": 0},
}

_KW_CURSOR = {"kw_index": 0, "page": 0}


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("read %s failed: %s", path.name, exc)
    return default


def _save_json(path: Path, data: Any) -> None:
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("write %s failed: %s", path.name, exc)


# ── HTTP helpers (proxy-aware) ────────────────────────────────────────────

def _http_get(url: str, timeout: float = 12.0, **kw) -> httpx.Response:
    """GET with optional harvest proxy."""
    if HARVEST_PROXY:
        kw["proxy"] = HARVEST_PROXY
    return httpx.get(url, timeout=timeout, follow_redirects=True, **kw)


def _http_post(url: str, timeout: float = 12.0, **kw) -> httpx.Response:
    if HARVEST_PROXY:
        kw["proxy"] = HARVEST_PROXY
    return httpx.post(url, timeout=timeout, follow_redirects=True, **kw)


# ── URL normalisation ─────────────────────────────────────────────────────

_URL_RE = re.compile(r"https?://[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_BLOCK_HOSTS = (
    "google.", "bing.", "duckduckgo.", "facebook.", "youtube.",
    "twitter.", "x.com", "instagram.", "linkedin.", "pinterest.",
    "wikipedia.", "youtu.be", "tiktok.", "reddit.", "microsoft.",
    "apple.com", "amazon.", "ebay.", "shopify.com",
)


def _normalise(raw: Any) -> str | None:
    if isinstance(raw, dict):
        raw = raw.get("url") or raw.get("site") or raw.get("shop_url") or raw.get("shopUrl") or raw.get("link")
    if not isinstance(raw, str):
        return None
    s = raw.strip().rstrip("/")
    if not s:
        return None
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    try:
        p = urllib.parse.urlparse(s)
        host = (p.hostname or "").lower()
        if "." not in host or " " in host:
            return None
        if any(bad in host for bad in _BLOCK_HOSTS):
            return None
        return f"https://{host}"
    except Exception:
        return None


def _extract_urls(text: str) -> list[str]:
    out: list[str] = []
    for m in _URL_RE.finditer(text):
        u = _normalise(m.group(0))
        if u:
            out.append(u)
    return out


# ── Keywords ──────────────────────────────────────────────────────────────

_DEFAULT_KEYWORDS = [
    "sample", "samples", "mini", "travel size",
    "sticker", "sticker pack", "stickers", "patch", "pin", "keychain",
    "candy", "chocolate", "gummy", "lollipop",
    "coffee sample", "tea sample", "seeds",
    "digital", "ebook", "template", "preset", "printable",
    "postcard", "greeting card",
    "lip balm", "face mask", "nail polish",
    "hot sauce", "spice packet", "soap sample", "soap bar",
    "candle sample", "wax melt",
    "earrings", "ring", "bracelet", "enamel pin", "challenge coin",
    "wristband", "keyring", "lanyard", "carabiner",
    "temporary tattoo", "flash tattoo",
    "sticker set", "sticker bundle", "decals",
    "gift tag", "labels", "notecard",
    "protein bar", "energy bar", "snack bar",
    "perfume sample", "fragrance sample",
    "shampoo sample", "serum sample",
    "art print", "mini print", "poster mini",
]


def _load_keywords() -> list[str]:
    if KEYWORDS_FILE.is_file():
        kws = [ln.strip() for ln in KEYWORDS_FILE.read_text(encoding="utf-8").splitlines()]
        kws = [k for k in kws if k and not k.startswith("#")]
        if kws:
            return kws
    return list(_DEFAULT_KEYWORDS)


# ── Discovery sources ─────────────────────────────────────────────────────

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _bing_rss(keyword: str, page: int, timeout: float = 12.0) -> list[str]:
    q = urllib.parse.quote_plus(f"site:myshopify.com {keyword}")
    first = page * 10 + 1
    url = f"https://www.bing.com/search?q={q}&first={first}&count=20&format=rss"
    headers = {
        "user-agent": _UA,
        "accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    try:
        r = _http_get(url, timeout=timeout, headers=headers)
        if r.status_code != 200:
            log.info("[disc] bing_rss %r p%d -> HTTP %d", keyword, page, r.status_code)
            return []
        out: list[str] = []
        try:
            root = ET.fromstring(r.text)
            for item in root.iter():
                tag = item.tag.split("}")[-1]
                if tag == "link" and item.text:
                    u = _normalise(item.text.strip())
                    if u:
                        out.append(u)
        except ET.ParseError:
            out = _extract_urls(r.text)
        log.info("[disc] bing_rss %r p%d -> %d urls", keyword, page, len(out))
        return out
    except Exception as exc:
        log.info("[disc] bing_rss %r p%d -> exc %s", keyword, page, exc)
        return []


def _ddg_html(keyword: str, timeout: float = 3.0) -> list[str]:
    q = urllib.parse.quote_plus(f"site:myshopify.com {keyword}")
    url = f"https://html.duckduckgo.com/html/?q={q}"
    headers = {
        "user-agent": _UA,
        "accept": "text/html,application/xhtml+xml",
        "accept-language": "en-US,en;q=0.9",
    }
    try:
        r = _http_get(url, timeout=timeout, headers=headers)
        if r.status_code != 200:
            return []
        out: list[str] = []
        for m in re.finditer(r'uddg=([^&"]+)', r.text):
            u = _normalise(urllib.parse.unquote(m.group(1)))
            if u:
                out.append(u)
        if not out:
            out = _extract_urls(r.text)
        log.info("[disc] ddg_html %r -> %d urls", keyword, len(out))
        return out
    except Exception as exc:
        log.info("[disc] ddg_html %r -> exc %s", keyword, exc)
        return []


def _load_seeds(limit: int) -> list[str]:
    """Return up to `limit` seeds that haven't been validated yet this process."""
    if not SEEDS_FILE.is_file():
        return []
    out: list[str] = []
    for ln in SEEDS_FILE.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        u = _normalise(ln)
        if not u:
            continue
        if u in _SEEN or u in _SEEDS_VALIDATED:
            continue
        out.append(u)
        if len(out) >= limit:
            break
    return out


def _discover(limit: int = 200) -> list[str]:
    """Return fresh candidates (limited seeds + scraped URLs) not yet tested."""
    out: list[str] = []

    # ── 1. Seeds (throttled: SEEDS_PER_CYCLE at a time)
    seeds = _load_seeds(SEEDS_PER_CYCLE)
    seed_added = 0
    for s in seeds:
        _SEEDS_VALIDATED.add(s)
        _SEEN.add(s)
        out.append(s)
        seed_added += 1
    if seed_added:
        _STATS["discover_hits"]["seeds"] += seed_added
        log.info("[disc] seeds: %d new (validated this process: %d)",
                 seed_added, len(_SEEDS_VALIDATED))

    if SEEDS_ONLY:
        return out

    # ── 2. Scraped URLs (up to SCRAPE_PER_CYCLE)
    kws = _load_keywords()
    if not kws:
        return out

    scrape_limit = min(SCRAPE_PER_CYCLE, max(10, limit - len(out)))
    attempts = 0
    max_attempts = 10
    bing_empty_streak = 0

    while len(out) < len(seeds) + scrape_limit and attempts < max_attempts:
        attempts += 1
        kw_idx = _KW_CURSOR["kw_index"] % len(kws)
        page   = _KW_CURSOR["page"]
        kw     = kws[kw_idx]

        urls = _bing_rss(kw, page)
        _STATS["discover_hits"]["bing_rss"] += len(urls)

        if len(urls) == 0:
            bing_empty_streak += 1
            if bing_empty_streak >= 2:
                log.info("[disc] bing empty x%d — trying ddg fallback", bing_empty_streak)
                urls = _ddg_html(kw)
                _STATS["discover_hits"]["ddg_html"] += len(urls)
                bing_empty_streak = 0
        else:
            bing_empty_streak = 0

        page += 1
        if page >= PAGES_PER_KEYWORD:
            page = 0
            kw_idx += 1
        _KW_CURSOR["kw_index"] = kw_idx
        _KW_CURSOR["page"]     = page

        for u in urls:
            if u not in _SEEN:
                _SEEN.add(u)
                out.append(u)

        time.sleep(random.uniform(0.4, 0.9))

    _save_state()
    log.info("[disc] cycle: fresh=%d seeds=%d scraped=%d attempts=%d",
             len(out), seed_added, len(out) - seed_added, attempts)
    return out


# ── Validation ────────────────────────────────────────────────────────────

def _validate(shop_url: str, timeout: float = 12.0) -> tuple[str, float]:
    """
    Returns (status, lowest_price).
    status: 'ok' | 'dead' | 'rate_limited' | 'error'
    """
    try:
        r = _http_get(
            f"{shop_url}/products.json?limit=250",
            timeout=timeout,
            headers={"user-agent": _UA, "accept": "application/json"},
        )
    except Exception:
        return "dead", 0.0
    if r.status_code == 429:
        return "rate_limited", 0.0
    if r.status_code != 200:
        return "dead", 0.0
    ctype = r.headers.get("content-type", "").lower()
    if "json" not in ctype and not r.text.lstrip().startswith("{"):
        return "dead", 0.0
    try:
        data = r.json()
    except Exception:
        return "dead", 0.0
    products = data.get("products") if isinstance(data, dict) else None
    if not isinstance(products, list) or not products:
        return "dead", 0.0
    low: float | None = None
    for p in products:
        for v in p.get("variants", []) or []:
            if v.get("available") is False:
                continue
            try:
                price = float(v.get("price") or 0)
            except (ValueError, TypeError):
                continue
            if price < MIN_PRICE or price > MAX_PRICE:
                continue
            if low is None or price < low:
                low = price
    if low is None:
        return "dead", 0.0
    return "ok", low


def _validate_batch(urls: list[str]) -> int:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    added = 0
    with ThreadPoolExecutor(max_workers=VALIDATE_WORKERS, thread_name_prefix="validate") as ex:
        futs = {ex.submit(_validate, u): u for u in urls}
        for fut in as_completed(futs):
            u = futs[fut]
            try:
                status, _ = fut.result()
            except Exception:
                status = "dead"
            _STATS["candidates_tested"] += 1
            if status == "rate_limited":
                _STATS["rate_limited"] += 1
                # Don't mark in _SEEDS_VALIDATED so it retries next cycle
                _SEEDS_VALIDATED.discard(u)
                continue
            if status == "ok":
                with _LOCK:
                    if u not in _POOL and len(_POOL) < POOL_MAX:
                        _POOL.append(u)
                        _LAST_GOOD[u] = time.time()
                        added += 1
                        _STATS["candidates_kept"] += 1
                        log.info("[pool] + %s", u)
            else:
                _STATS["candidates_dropped"] += 1
    return added


def _revalidate_old() -> int:
    cutoff = time.time() - REVALIDATE_HOURS * 3600
    stale = [u for u in _POOL if _LAST_GOOD.get(u, 0) < cutoff]
    if not stale:
        return 0
    log.info("[harvest] revalidating %d stale sites", len(stale))
    from concurrent.futures import ThreadPoolExecutor, as_completed
    removed = 0
    with ThreadPoolExecutor(max_workers=VALIDATE_WORKERS, thread_name_prefix="reval") as ex:
        futs = {ex.submit(_validate, u): u for u in stale}
        for fut in as_completed(futs):
            u = futs[fut]
            try:
                status, _ = fut.result()
            except Exception:
                status = "dead"
            if status == "ok":
                with _LOCK:
                    _LAST_GOOD[u] = time.time()
            elif status == "dead":
                with _LOCK:
                    if u in _POOL:
                        _POOL.remove(u)
                        removed += 1
            # rate_limited — leave as-is, retry next cycle
    return removed


# ── Persistence ───────────────────────────────────────────────────────────

def _save_cache() -> None:
    _save_json(CACHE_FILE, {
        "sites":         _POOL,
        "captcha_sites": list(_CAPTCHA_SITES),
        "last_good":     _LAST_GOOD,
        "ts":            time.time(),
    })


def _load_cache() -> None:
    global _POOL, _LAST_GOOD, _SEEN, _CAPTCHA_SITES
    data  = _load_json(CACHE_FILE, {})
    sites = data.get("sites") or []
    if isinstance(sites, list):
        _POOL = [u for u in (_normalise(x) for x in sites) if u][:POOL_MAX]
    lg = data.get("last_good") or {}
    if isinstance(lg, dict):
        _LAST_GOOD = {k: float(v) for k, v in lg.items() if isinstance(v, (int, float))}
    cap = data.get("captcha_sites") or []
    if isinstance(cap, list):
        _CAPTCHA_SITES = {u for u in (_normalise(x) for x in cap) if u}
    _SEEN = set(_POOL) | _CAPTCHA_SITES
    log.info("[sites] loaded %d sites (%d captcha-skipped)", len(_POOL), len(_CAPTCHA_SITES))


def _save_state() -> None:
    _save_json(STATE_FILE, {"cursor": _KW_CURSOR, "stats": _STATS})


def _load_state() -> None:
    data = _load_json(STATE_FILE, {})
    cur = data.get("cursor") or {}
    if isinstance(cur, dict):
        _KW_CURSOR["kw_index"] = int(cur.get("kw_index", 0))
        _KW_CURSOR["page"]     = int(cur.get("page", 0))
    st = data.get("stats") or {}
    if isinstance(st, dict):
        for k in _STATS:
            if k in st:
                _STATS[k] = st[k]


# ── Background harvester ──────────────────────────────────────────────────

_HARVEST_THREAD: threading.Thread | None = None
_HARVEST_STOP = threading.Event()


def _harvest_loop() -> None:
    log.info("[harvest] thread started (interval %ds, seeds_only=%s, max_price=$%.2f, proxy=%s)",
             HARVEST_INTERVAL, SEEDS_ONLY, MAX_PRICE, "yes" if HARVEST_PROXY else "no")
    if _HARVEST_STOP.wait(3):
        return
    while not _HARVEST_STOP.is_set():
        try:
            _STATS["cycles"] += 1
            t0 = time.time()
            removed = _revalidate_old()
            fresh   = _discover(limit=SEEDS_PER_CYCLE + SCRAPE_PER_CYCLE)
            _STATS["candidates_seen"] += len(fresh)
            added   = _validate_batch(fresh)
            _STATS["last_cycle_ts"]    = int(t0)
            _STATS["last_cycle_found"] = added
            _save_cache()
            _save_state()
            log.info("[harvest] cycle %d: discovered=%d added=%d removed=%d pool=%d captcha=%d 429s=%d (%.1fs)",
                     _STATS["cycles"], len(fresh), added, removed,
                     len(_POOL), len(_CAPTCHA_SITES), _STATS["rate_limited"],
                     time.time() - t0)
        except Exception as exc:
            log.exception("[harvest] cycle error: %s", exc)
        if _HARVEST_STOP.wait(HARVEST_INTERVAL):
            return


def start_harvester() -> None:
    global _HARVEST_THREAD
    if _HARVEST_THREAD and _HARVEST_THREAD.is_alive():
        return
    _HARVEST_THREAD = threading.Thread(target=_harvest_loop, daemon=True, name="harvest")
    _HARVEST_THREAD.start()


def stop_harvester() -> None:
    _HARVEST_STOP.set()


def harvest_once(limit: int = 200) -> dict:
    removed = _revalidate_old()
    fresh   = _discover(limit=limit)
    _STATS["candidates_seen"] += len(fresh)
    added   = _validate_batch(fresh)
    _STATS["last_cycle_ts"]    = int(time.time())
    _STATS["last_cycle_found"] = added
    _save_cache(); _save_state()
    return {
        "discovered": len(fresh),
        "added":      added,
        "removed":    removed,
        "pool":       len(_POOL),
        "captcha":    len(_CAPTCHA_SITES),
        "sources":    dict(_STATS["discover_hits"]),
    }


# ── Public pool API ───────────────────────────────────────────────────────

def _score(url: str) -> tuple[int, float]:
    return (_FAIL.get(url, 0), _LAST_USE.get(url, 0.0))


def pick_site(exclude: set[str] | None = None) -> str | None:
    global _RR
    exclude = exclude or set()
    with _LOCK:
        pool = [u for u in _POOL if u not in exclude and u not in _CAPTCHA_SITES]
        if not pool:
            return None
        pool.sort(key=_score)
        take = max(1, len(pool) // 3)
        top  = pool[:take]
        idx  = _RR % len(top)
        _RR += 1
        chosen = top[idx]
        _LAST_USE[chosen] = time.time()
        return chosen


def report_success(url: str) -> None:
    with _LOCK:
        _FAIL.pop(url, None)
        _LAST_USE[url]  = time.time()
        _LAST_GOOD[url] = time.time()


def report_failure(url: str, weight: int = 1) -> None:
    with _LOCK:
        _FAIL[url] = _FAIL.get(url, 0) + weight
        _LAST_USE[url] = time.time()


def report_captcha(url: str) -> None:
    if not url:
        return
    u = _normalise(url)
    if not u:
        return
    with _LOCK:
        if u in _CAPTCHA_SITES:
            return
        _CAPTCHA_SITES.add(u)
        _STATS["captcha_marked"] += 1
        if u in _POOL:
            _POOL.remove(u)
        _LAST_USE[u] = time.time()
    log.warning("[captcha] marked: %s (total %d)", u, len(_CAPTCHA_SITES))


def report_payment_blocked(url: str) -> None:
    """Store doesn't support Shopify Payments for the requested currency — skip it."""
    if not url:
        return
    u = _normalise(url)
    if not u:
        return
    with _LOCK:
        _STATS["payment_blocked"] += 1
        _FAIL[u] = _FAIL.get(u, 0) + 5   # heavy penalty
        if u in _POOL:
            _POOL.remove(u)
        _LAST_USE[u] = time.time()
    log.warning("[payment] store doesn't support Shopify Payments: %s", u)


def is_captcha(url: str) -> bool:
    u = _normalise(url)
    if not u:
        return False
    with _LOCK:
        return u in _CAPTCHA_SITES


def clear_captcha(url: str | None = None) -> int:
    with _LOCK:
        if url is None:
            n = len(_CAPTCHA_SITES)
            _CAPTCHA_SITES.clear()
            return n
        u = _normalise(url)
        if u and u in _CAPTCHA_SITES:
            _CAPTCHA_SITES.remove(u)
            return 1
        return 0


def refresh_sites(force: bool = False) -> int:
    if force:
        try:
            harvest_once()
        except Exception as exc:
            log.warning("refresh_sites force failed: %s", exc)
    return len(_POOL)


def pool_size() -> int:
    return len(_POOL)


def captcha_size() -> int:
    return len(_CAPTCHA_SITES)


def snapshot() -> dict:
    with _LOCK:
        return {
            "count":            len(_POOL),
            "captcha_count":    len(_CAPTCHA_SITES),
            "stats":            dict(_STATS),
            "top":              sorted([u for u in _POOL if u not in _CAPTCHA_SITES], key=_score)[:10],
            "captcha_top":      sorted(_CAPTCHA_SITES)[:10],
            "harvest_interval": HARVEST_INTERVAL,
            "max_price":        MAX_PRICE,
            "min_price":        MIN_PRICE,
            "seeds_only":       SEEDS_ONLY,
            "harvest_proxy":    "yes" if HARVEST_PROXY else "no",
            "seeds_validated":  len(_SEEDS_VALIDATED),
        }


# ── Boot ──────────────────────────────────────────────────────────────────
_load_cache()
_load_state()
if not KEYWORDS_FILE.is_file():
    KEYWORDS_FILE.write_text("\n".join(_DEFAULT_KEYWORDS) + "\n", encoding="utf-8")
if not SEEDS_FILE.is_file():
    SEEDS_FILE.write_text(
        "# One domain per line. Every line goes straight into validation.\n"
        "# Example:\n"
        "# mystore.myshopify.com\n",
        encoding="utf-8",
    )
start_harvester()