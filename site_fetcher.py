"""
site_fetcher.py — Self-hosted Shopify site discovery + pool.

Discovery sources:
  1. DuckDuckGo Lite  (site:myshopify.com <keyword>)
  2. Bing HTML        (site:myshopify.com <keyword>)
  3. seeds.txt        (manual domains)

Validation per candidate:
  • GET /products.json?limit=250 with fast timeout
  • Must return JSON with a "products" list
  • Shop must NOT be password-locked (401/403 on /products.json -> skip)
  • At least one variant with: available=True, MIN_PRICE <= price <= MAX_PRICE

Persistence:
  site_cache.json    — working pool (auto-pruned, TTL refreshed by re-check)
  harvest_state.json — keyword cursor, discovery stats

Background thread runs cycles every HARVEST_INTERVAL seconds.
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
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("sites")

# ── paths ──────────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).parent
CACHE_FILE     = BASE_DIR / "site_cache.json"
STATE_FILE     = BASE_DIR / "harvest_state.json"
KEYWORDS_FILE  = BASE_DIR / "keywords.txt"
SEEDS_FILE     = BASE_DIR / "seeds.txt"

# ── config ─────────────────────────────────────────────────────────────────
MAX_PRICE         = float(os.environ.get("HARVEST_MAX_PRICE", "5.00"))
MIN_PRICE         = float(os.environ.get("HARVEST_MIN_PRICE", "0.10"))
POOL_MAX          = int(os.environ.get("SITE_POOL_MAX", "800"))
HARVEST_INTERVAL  = int(os.environ.get("HARVEST_INTERVAL_SECS", "900"))       # 15 min
PAGES_PER_KEYWORD = int(os.environ.get("HARVEST_PAGES_PER_KEYWORD", "3"))
VALIDATE_WORKERS  = int(os.environ.get("HARVEST_VALIDATE_WORKERS", "24"))
REVALIDATE_HOURS  = int(os.environ.get("HARVEST_REVALIDATE_HOURS", "24"))

# ── state ──────────────────────────────────────────────────────────────────
_LOCK      = threading.Lock()
_POOL: list[str]             = []           # working sites
_FAIL: dict[str, int]        = {}
_LAST_USE: dict[str, float]  = {}
_LAST_GOOD: dict[str, float] = {}
_SEEN: set[str]              = set()        # every domain ever tested
_RR = 0
_STATS = {
    "candidates_seen":    0,
    "candidates_tested":  0,
    "candidates_kept":    0,
    "candidates_dropped": 0,
    "last_cycle_ts":      0,
    "last_cycle_found":   0,
    "cycles":             0,
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


# ═══════════════════════════════════════════════════════════════════════════
#  URL normalisation
# ═══════════════════════════════════════════════════════════════════════════

_URL_RE = re.compile(r"https?://[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_BLOCK_HOSTS = (
    "google.", "bing.", "duckduckgo.", "facebook.", "youtube.",
    "twitter.", "x.com", "instagram.", "linkedin.", "pinterest.",
    "wikipedia.", "youtu.be", "tiktok.", "reddit.",
)


def _normalise(raw: Any) -> str | None:
    if isinstance(raw, dict):
        raw = raw.get("url") or raw.get("site") or raw.get("shop_url") or raw.get("shopUrl")
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


# ═══════════════════════════════════════════════════════════════════════════
#  Keywords
# ═══════════════════════════════════════════════════════════════════════════

_DEFAULT_KEYWORDS = [
    "sample", "samples", "trial", "mini", "travel size",
    "sticker", "sticker pack", "sticker sheet", "stickers",
    "patch", "pin", "keychain", "magnet", "bookmark",
    "candy", "chocolate", "gummy", "gummies", "lollipop",
    "tea sample", "coffee sample", "coffee beans", "coffee",
    "seeds", "seed packet", "wildflower seeds",
    "digital", "ebook", "template", "preset", "presets",
    "printable", "planner", "wallpaper", "svg", "png",
    "postcard", "greeting card", "thank you card",
    "lip balm", "lip gloss", "face mask", "sheet mask",
    "nail polish", "mini polish", "sample size",
    "hair tie", "scrunchie", "bandana", "pin badge",
    "hand sanitizer", "hand cream", "hand salve",
    "hot sauce", "hot sauce sample", "spice packet",
    "soap sample", "soap bar", "bath bomb",
    "candle sample", "tea light", "wax melt",
    "earrings", "ring", "bracelet", "necklace",
    "enamel pin", "lapel pin", "coin", "challenge coin",
    "wristband", "keyring", "key ring", "lanyard", "carabiner",
    "temporary tattoo", "tattoo", "flash tattoo",
    "sticker set", "sticker bundle", "decals",
    "greeting cards", "notecard", "note card",
    "gift tag", "gift tags", "labels",
    "pin set", "coin set", "sticker lot",
    "matcha sample", "herbal tea", "loose leaf",
    "protein sample", "protein bar", "energy bar",
    "snack bar", "granola", "honey sample",
    "perfume sample", "fragrance sample", "cologne sample",
    "shampoo sample", "conditioner sample",
    "face cream sample", "serum sample",
    "art print", "mini print", "photo print",
    "poster mini", "sticker bomb",
]


def _load_keywords() -> list[str]:
    if KEYWORDS_FILE.is_file():
        kws = [ln.strip() for ln in KEYWORDS_FILE.read_text(encoding="utf-8").splitlines()]
        kws = [k for k in kws if k and not k.startswith("#")]
        if kws:
            return kws
    return list(_DEFAULT_KEYWORDS)


# ═══════════════════════════════════════════════════════════════════════════
#  Discovery
# ═══════════════════════════════════════════════════════════════════════════

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _ddg_lite(keyword: str, page: int, timeout: float = 12.0) -> list[str]:
    """Scrape DuckDuckGo Lite. page is 0-based, 20 results per page."""
    data = {
        "q":  f"site:myshopify.com {keyword}",
        "s":  str(page * 20),
        "dc": str(page * 20 + 1),
        "o":  "json",
        "api":"d.js",
        "kl": "us-en",
    }
    headers = {
        "user-agent":   _UA,
        "accept":       "text/html,application/xhtml+xml",
        "content-type": "application/x-www-form-urlencoded",
    }
    try:
        r = httpx.post("https://lite.duckduckgo.com/lite/",
                       data=data, headers=headers,
                       timeout=timeout, follow_redirects=True)
        if r.status_code != 200:
            return []
        return _extract_urls(r.text)
    except Exception as exc:
        log.debug("ddg %r page %d failed: %s", keyword, page, exc)
        return []


def _bing(keyword: str, page: int, timeout: float = 12.0) -> list[str]:
    """Scrape Bing HTML. page is 0-based, 10 results per page."""
    q = urllib.parse.quote_plus(f"site:myshopify.com {keyword}")
    first = page * 10 + 1
    url = f"https://www.bing.com/search?q={q}&first={first}&count=20"
    headers = {
        "user-agent":      _UA,
        "accept":          "text/html,application/xhtml+xml",
        "accept-language": "en-US,en;q=0.9",
    }
    try:
        r = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
        if r.status_code != 200:
            return []
        return _extract_urls(r.text)
    except Exception as exc:
        log.debug("bing %r page %d failed: %s", keyword, page, exc)
        return []


def _load_seeds() -> list[str]:
    if not SEEDS_FILE.is_file():
        return []
    out: list[str] = []
    for ln in SEEDS_FILE.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        u = _normalise(ln)
        if u:
            out.append(u)
    return out


_STAGE: list[str] = []


def _discover(limit: int = 120) -> list[str]:
    """Run a discovery cycle. Returns candidates not yet tested."""
    kws   = _load_keywords()
    seeds = _load_seeds()
    for s in seeds:
        if s not in _SEEN:
            _SEEN.add(s)
            _STAGE.append(s)

    found: list[str] = []
    attempts = 0
    max_attempts = max(8, limit // 8)

    while len(found) + len(_STAGE) < limit and attempts < max_attempts:
        attempts += 1
        kw_idx = _KW_CURSOR["kw_index"] % len(kws)
        page   = _KW_CURSOR["page"]
        kw     = kws[kw_idx]

        source = _bing if attempts % 2 == 0 else _ddg_lite
        urls   = source(kw, page)

        page += 1
        if page >= PAGES_PER_KEYWORD:
            page = 0
            kw_idx += 1
        _KW_CURSOR["kw_index"] = kw_idx
        _KW_CURSOR["page"]     = page

        for u in urls:
            if u in _SEEN:
                continue
            _SEEN.add(u)
            found.append(u)

        time.sleep(random.uniform(0.8, 1.8))

    _save_state()
    return found


# ═══════════════════════════════════════════════════════════════════════════
#  Validation
# ═══════════════════════════════════════════════════════════════════════════

def _validate(shop_url: str, timeout: float = 12.0) -> tuple[bool, float]:
    """Return (is_working, lowest_price)."""
    try:
        r = httpx.get(
            f"{shop_url}/products.json?limit=250",
            headers={"user-agent": _UA, "accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
        )
    except Exception:
        return False, 0.0
    if r.status_code != 200:
        return False, 0.0
    ctype = r.headers.get("content-type", "").lower()
    if "json" not in ctype and not r.text.lstrip().startswith("{"):
        return False, 0.0
    try:
        data = r.json()
    except Exception:
        return False, 0.0
    products = data.get("products") if isinstance(data, dict) else None
    if not isinstance(products, list) or not products:
        return False, 0.0

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
        return False, 0.0
    return True, low


def _validate_batch(urls: list[str]) -> int:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    added = 0
    with ThreadPoolExecutor(max_workers=VALIDATE_WORKERS, thread_name_prefix="validate") as ex:
        futs = {ex.submit(_validate, u): u for u in urls}
        for fut in as_completed(futs):
            u = futs[fut]
            try:
                ok, _ = fut.result()
            except Exception:
                ok = False
            _STATS["candidates_tested"] += 1
            if ok:
                with _LOCK:
                    if u not in _POOL and len(_POOL) < POOL_MAX:
                        _POOL.append(u)
                        _LAST_GOOD[u] = time.time()
                        added += 1
                        _STATS["candidates_kept"] += 1
            else:
                _STATS["candidates_dropped"] += 1
    return added


# ═══════════════════════════════════════════════════════════════════════════
#  Revalidation
# ═══════════════════════════════════════════════════════════════════════════

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
                ok, _ = fut.result()
            except Exception:
                ok = False
            with _LOCK:
                if ok:
                    _LAST_GOOD[u] = time.time()
                elif u in _POOL:
                    _POOL.remove(u)
                    removed += 1
    return removed


# ═══════════════════════════════════════════════════════════════════════════
#  Persistence
# ═══════════════════════════════════════════════════════════════════════════

def _save_cache() -> None:
    _save_json(CACHE_FILE, {
        "sites":     _POOL,
        "last_good": _LAST_GOOD,
        "ts":        time.time(),
    })


def _load_cache() -> None:
    global _POOL, _LAST_GOOD, _SEEN
    data  = _load_json(CACHE_FILE, {})
    sites = data.get("sites") or []
    if isinstance(sites, list):
        _POOL = [u for u in (_normalise(x) for x in sites) if u][:POOL_MAX]
    lg = data.get("last_good") or {}
    if isinstance(lg, dict):
        _LAST_GOOD = {k: float(v) for k, v in lg.items() if isinstance(v, (int, float))}
    _SEEN = set(_POOL)
    log.info("[sites] loaded %d from cache", len(_POOL))


def _save_state() -> None:
    _save_json(STATE_FILE, {"cursor": _KW_CURSOR, "stats": _STATS})


def _load_state() -> None:
    data = _load_json(STATE_FILE, {})
    cur  = data.get("cursor") or {}
    if isinstance(cur, dict):
        _KW_CURSOR["kw_index"] = int(cur.get("kw_index", 0))
        _KW_CURSOR["page"]     = int(cur.get("page", 0))
    st = data.get("stats") or {}
    if isinstance(st, dict):
        for k in _STATS:
            if k in st:
                _STATS[k] = st[k]


# ═══════════════════════════════════════════════════════════════════════════
#  Background harvester
# ═══════════════════════════════════════════════════════════════════════════

_HARVEST_THREAD: threading.Thread | None = None
_HARVEST_STOP = threading.Event()


def _harvest_loop() -> None:
    log.info("[harvest] thread started (interval %ds)", HARVEST_INTERVAL)
    if _HARVEST_STOP.wait(5):
        return
    while not _HARVEST_STOP.is_set():
        try:
            _STATS["cycles"] += 1
            t0 = time.time()

            removed = _revalidate_old()
            fresh   = _discover(limit=200)
            _STATS["candidates_seen"] += len(fresh)
            added   = _validate_batch(fresh)

            _STATS["last_cycle_ts"]    = int(t0)
            _STATS["last_cycle_found"] = added
            _save_cache()
            _save_state()

            log.info(
                "[harvest] cycle %d: discovered=%d added=%d removed_stale=%d pool=%d (%.1fs)",
                _STATS["cycles"], len(fresh), added, removed, len(_POOL), time.time() - t0,
            )
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
    }


# ═══════════════════════════════════════════════════════════════════════════
#  Public pool API
# ═══════════════════════════════════════════════════════════════════════════

def _score(url: str) -> tuple[int, float]:
    fails = _FAIL.get(url, 0)
    last  = _LAST_USE.get(url, 0.0)
    return (fails, last)


def pick_site(exclude: set[str] | None = None) -> str | None:
    global _RR
    if not _POOL:
        return None
    exclude = exclude or set()
    with _LOCK:
        pool = [u for u in _POOL if u not in exclude] or list(_POOL)
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


def refresh_sites(force: bool = False) -> int:
    if force:
        try:
            harvest_once()
        except Exception as exc:
            log.warning("refresh_sites force failed: %s", exc)
    return len(_POOL)


def pool_size() -> int:
    return len(_POOL)


def snapshot() -> dict:
    with _LOCK:
        return {
            "count":            len(_POOL),
            "stats":            dict(_STATS),
            "top":              sorted(_POOL, key=_score)[:10],
            "harvest_interval": HARVEST_INTERVAL,
            "max_price":        MAX_PRICE,
            "min_price":        MIN_PRICE,
        }


# ── boot ───────────────────────────────────────────────────────────────────
_load_cache()
_load_state()
if not KEYWORDS_FILE.is_file():
    KEYWORDS_FILE.write_text("\n".join(_DEFAULT_KEYWORDS) + "\n", encoding="utf-8")
if not SEEDS_FILE.is_file():
    SEEDS_FILE.write_text(
        "# One domain per line. Every line goes straight into the validation queue.\n"
        "# Example:\n"
        "# mystore.myshopify.com\n",
        encoding="utf-8",
    )
start_harvester()
