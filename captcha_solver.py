"""
VXO captcha solver — v3 protobuf + v2 form + enterprise endpoints +
recaptcha.net domain trick + paid fallback.
"""
import base64
import html as _html
import json
import logging
import os
import random
import re
import struct
import time
from urllib.parse import urlparse

from curl_cffi.requests import Session

logger = logging.getLogger("vxo.captcha")

# ══════════════════════════════════════════════════════════════════════
# HARDCODE SITEKEYS HERE after calling /getkey
# ══════════════════════════════════════════════════════════════════════
SHOP_SITEKEYS: dict = {
    # "www.32degrees.com":    "6Lcxxxxx",
    # "www.bettertights.com": "6Lcxxxxx",
}

SERVICE  = os.environ.get("CAPTCHA_SERVICE", "").lower()
API_KEY  = os.environ.get("CAPTCHA_API_KEY", "")
POLL_SEC = float(os.environ.get("CAPTCHA_POLL_SEC", "3"))
MAX_WAIT = int(os.environ.get("CAPTCHA_MAX_WAIT", "120"))

ANCHOR_HOSTS = ["https://www.recaptcha.net", "https://www.google.com"]
ANCHOR_PATHS = ["/recaptcha/enterprise/anchor", "/recaptcha/api2/anchor"]
RELOAD_PATHS = ["/recaptcha/enterprise/reload", "/recaptcha/api2/reload"]

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"
_SITEKEY_CACHE: dict = {}
_JS_URL_RE = re.compile(r'<script[^>]+src="([^"]+)"')

_SITEKEY_PATTERNS = [
    r'recaptcha/enterprise\.js\?render=([A-Za-z0-9_\-]{20,})',
    r'recaptcha/api\.js\?render=([A-Za-z0-9_\-]{20,})',
    r'"recaptchaSiteKey"\s*:\s*"([^"]+)"',
    r'"recaptcha_site_key"\s*:\s*"([^"]+)"',
    r'"captchaSiteKey"\s*:\s*"([^"]+)"',
    r'"checkpointCaptchaSiteKey"\s*:\s*"([^"]+)"',
    r'"siteKey"\s*:\s*"([^"]+)"',
    r'"sitekey"\s*:\s*"([^"]+)"',
    r'data-sitekey\s*=\s*"([^"]+)"',
    r'grecaptcha\.execute\(\s*["\']([A-Za-z0-9_\-]{20,})["\']',
    r'\b(6L[A-Za-z0-9_\-]{38})\b',
    r'\b(6A[A-Za-z0-9_\-]{38})\b',
]


def _extract_from_body(body: str) -> str:
    if not body:
        return ""
    decoded = _html.unescape(body).replace("&quot;", '"')
    for pat in _SITEKEY_PATTERNS:
        m = re.search(pat, decoded)
        if m:
            key = m.group(1)
            if key.startswith(("6L", "6A")):
                return key
    return ""


def extract_sitekey(*bodies: str) -> str:
    for b in bodies:
        k = _extract_from_body(b)
        if k:
            return k
    return ""


def _collect_script_urls(checkout_html: str, shop_url: str) -> list:
    urls, seen = [], set()
    for m in _JS_URL_RE.finditer(checkout_html or ""):
        u = m.group(1)
        if u.startswith("//"):   u = "https:" + u
        elif u.startswith("/"):  u = shop_url.rstrip("/") + u
        elif not u.startswith("http"): continue
        if u not in seen:
            seen.add(u); urls.append(u)
    return urls


def probe_sitekey(client, shop_url: str, checkout_html: str, checkout_url: str) -> str:
    if shop_url in _SITEKEY_CACHE:
        return _SITEKEY_CACHE[shop_url]

    host = urlparse(shop_url).hostname or ""

    if host in SHOP_SITEKEYS and SHOP_SITEKEYS[host]:
        key = SHOP_SITEKEYS[host]
        logger.info(f"sitekey from hardcode ({host}): {key[:16]}…")
        _SITEKEY_CACHE[shop_url] = key
        return key

    key = _extract_from_body(checkout_html)
    if key:
        logger.info(f"sitekey from HTML: {key[:16]}…")
        _SITEKEY_CACHE[shop_url] = key
        return key

    js_urls = _collect_script_urls(checkout_html, shop_url)
    logger.info(f"sitekey not in HTML — scanning {len(js_urls)} JS bundles")

    for url in js_urls:
        try:
            r = client.get(url, headers={"Referer": checkout_url, "Accept": "*/*"})
            if r.status_code != 200:
                continue
            key = _extract_from_body(r.text)
            if key:
                logger.info(f"sitekey from JS {url[-40:]}: {key[:16]}…")
                _SITEKEY_CACHE[shop_url] = key
                return key
        except Exception:
            continue

    for m in re.finditer(r'<meta\s+name="(serialized-[^"]+)"\s+content="([^"]*)"',
                         checkout_html or ""):
        key = _extract_from_body(_html.unescape(m.group(2)))
        if key:
            logger.info(f"sitekey from meta {m.group(1)}: {key[:16]}…")
            _SITEKEY_CACHE[shop_url] = key
            return key

    logger.warning("no sitekey found — run /getkey and fill SHOP_SITEKEYS")
    return ""


def _to_base36(num: int) -> str:
    if num == 0: return "0"
    out = ""
    while num > 0:
        out = _B36[num % 36] + out
        num //= 36
    return out


def _generate_cb() -> str:
    a = _to_base36(random.randint(0, 2147483647))
    b = _to_base36(abs(random.randint(0, 2147483647) ^ int(time.time() * 1000)))
    return a + b


def _encode_co(url: str) -> str:
    p = urlparse(url)
    origin = f"{p.scheme}://{p.hostname}:443"
    return base64.b64encode(origin.encode()).decode().replace("=", ".")


def _scramble_oz(oz_bytes: bytes, ts_ms: int) -> str:
    z = ts_ms % 1000000
    m = random.randint(0, 254)
    out = bytearray([m])
    for i, b in enumerate(oz_bytes):
        out.append((b + len(oz_bytes) + (z + m) * (i + m)) % 256)
    return "0" + base64.urlsafe_b64encode(bytes(out)).decode().rstrip("=")


def _build_oz(site_url: str, screen, tz: int) -> bytes:
    ts_ms = int(time.time() * 1000)
    ts_b64 = base64.b64encode(str(ts_ms).encode()).decode().rstrip("=")
    nonce = base64.urlsafe_b64encode(random.randbytes(24)).decode().rstrip("=")
    oz = {
        5: str(random.randint(1000, 9999)),
        6: random.randint(1, 10),
        17: [nonce], 18: 2, 19: ts_b64, 28: site_url,
        65: random.randint(1000, 5000),
        73: json.dumps({"brands": [["Chromium", "131"], ["Not-A.Brand", "24"],
                                    ["Google Chrome", "131"]],
                        "mobile": False, "platform": "Windows"},
                       separators=(",", ":")),
        74: json.dumps({"mouse": [], "keyboard": [], "touch": [], "scroll": [],
                        "resize": [], "ua": DEFAULT_UA, "screen": list(screen),
                        "timezone": tz,
                        "canvas": str(random.randint(100000000, 4294967295)),
                        "webgl": "ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.5)"},
                       separators=(",", ":")),
    }
    return json.dumps(oz, separators=(",", ":")).encode()


def _telemetry_blob() -> str:
    ts_base = int(time.time() * 1000) - random.randint(8000, 25000)
    mouse = []
    for _ in range(random.randint(2, 4)):
        sx, sy = random.randint(200, 1400), random.randint(150, 800)
        ex, ey = random.randint(400, 1600), random.randint(300, 900)
        cx, cy = float(sx), float(sy)
        ts = ts_base
        for i in range(12):
            t = i / 11
            ease = t * t * (3 - 2 * t)
            cx += (sx + (ex - sx) * ease - cx) * 0.6 + random.gauss(0, 8)
            cy += (sy + (ey - sy) * ease - cy) * 0.6 + random.gauss(0, 8)
            ts += random.randint(40, 140)
            mouse.append([1, round(cx), round(cy), ts])
    scroll = [[2, random.randint(40, 220), ts_base + random.randint(500, 3000)],
              [2, random.randint(40, 220), ts_base + random.randint(4000, 9000)]]
    perf = [None, None, None,
            [9, round(random.uniform(5, 12), 8),
             round(random.uniform(0.005, 0.03), 8), random.randint(12, 24)],
            [random.randint(80, 140), round(random.uniform(0.2, 0.6), 8),
             round(random.uniform(0.003, 0.01), 8), random.randint(3, 8)],
            0, 0, 0]
    raw = json.dumps([mouse, scroll, perf,
                      ["www.googletagmanager.com", "www.google.com"],
                      [random.randint(6, 12), random.randint(400, 1200)]],
                     separators=(",", ":")).encode()
    return base64.b64encode(raw).decode().rstrip("=")


def _varint(v: int) -> bytes:
    bits = v & 0x7F
    v >>= 7
    out = b""
    while v:
        out += bytes([0x80 | bits])
        bits = v & 0x7F
        v >>= 7
    out += bytes([bits])
    return out


def _field(num: int, value) -> bytes:
    tag = num << 3
    if isinstance(value, int):   return _varint(tag | 0) + _varint(value)
    if isinstance(value, float): return _varint(tag | 1) + struct.pack("<d", value)
    if isinstance(value, str):   value = value.encode("utf-8")
    if isinstance(value, bytes):
        return _varint(tag | 2) + _varint(len(value)) + value
    return b""


def _pb_encode(fields: dict) -> bytes:
    return b"".join(_field(n, fields[n]) for n in sorted(fields)
                    if fields[n] is not None)


def _fetch_version(session: Session, sitekey: str, referer: str) -> str:
    for host in ANCHOR_HOSTS:
        for api_path in ("/recaptcha/api.js", "/recaptcha/enterprise.js"):
            try:
                r = session.get(f"{host}{api_path}",
                                params={"render": sitekey, "hl": "en"},
                                headers={"Referer": referer})
                m = re.search(r"/releases/([^/]+)/recaptcha", r.text)
                if m:
                    return m.group(1)
            except Exception:
                continue
    raise RuntimeError("could not fetch recaptcha version")


def _v3_solve(sitekey, site_url, proxy_url, user_agent, impersonate) -> str:
    kw = {"impersonate": impersonate, "timeout": 25}
    if proxy_url: kw["proxy"] = proxy_url
    with Session(**kw) as sess:
        sess.headers.update({"User-Agent": user_agent})
        try:
            version = _fetch_version(sess, sitekey, site_url)
        except Exception as e:
            logger.debug(f"v3 version fetch failed: {e}")
            return ""
        co = _encode_co(site_url)
        cb = _generate_cb()
        screen = random.choice([(1920, 1080), (1536, 864), (1440, 900), (1366, 768)])
        tz = random.choice([-480, -420, -360, -300, -240, 0, 60, 330])
        oz_bytes = _build_oz(site_url, screen, tz)
        ts_ms = int(time.time() * 1000)
        scrambled = _scramble_oz(oz_bytes, ts_ms)
        anchor_token = ""
        for host in ANCHOR_HOSTS:
            for path in ANCHOR_PATHS:
                try:
                    au = (f"{host}{path}"
                          f"?ar=1&k={sitekey}&co={co}&hl=en&v={version}"
                          f"&size=invisible&anchor-ms=20000&execute-ms=30000&cb={cb}")
                    r1 = sess.get(au, headers={"Referer": site_url})
                    m = re.search(r'id="recaptcha-token"\s+value="([^"]+)"', r1.text)
                    if m:
                        anchor_token = m.group(1)
                        break
                except Exception:
                    continue
            if anchor_token:
                break
        if not anchor_token:
            return ""
        payload = {
            1: version, 2: anchor_token, 4: scrambled,
            5: str(random.randint(-2147483648, 2147483647)),
            6: "q", 8: "submit", 14: sitekey, 16: scrambled,
            20: _telemetry_blob(), 22: "", 25: "W10", 28: 20000, 29: 30000,
        }
        body = _pb_encode(payload)
        for host in ANCHOR_HOSTS:
            for path in RELOAD_PATHS:
                try:
                    r2 = sess.post(f"{host}{path}?k={sitekey}", data=body,
                                   headers={"Content-Type": "application/x-protobuffer",
                                            "Referer": site_url, "Origin": host})
                    m = re.search(r'"rresp"\s*,\s*"([^"]+)"', r2.text)
                    if m and len(m.group(1)) > 20:
                        tok = m.group(1)
                        logger.info(f"v3 token OK via {host}{path} ({len(tok)} chars)")
                        return tok
                except Exception:
                    continue
    return ""


def _v2_solve(sitekey, site_url, proxy_url, user_agent, impersonate) -> str:
    kw = {"impersonate": impersonate, "timeout": 25}
    if proxy_url: kw["proxy"] = proxy_url
    with Session(**kw) as sess:
        sess.headers.update({"User-Agent": user_agent})
        try:
            version = _fetch_version(sess, sitekey, site_url)
        except Exception:
            return ""
        co = base64.b64encode(site_url.encode()).decode().rstrip("=")
        for host in ANCHOR_HOSTS:
            for anchor_path in ANCHOR_PATHS:
                try:
                    au = (f"{host}{anchor_path}"
                          f"?ar=1&k={sitekey}&co={co}&hl=en&v={version}&size=invisible")
                    r1 = sess.get(au, headers={"Referer": site_url})
                    if 'recaptcha-token" value="' not in r1.text:
                        continue
                    token1 = r1.text.split('recaptcha-token" value="')[1].split('"')[0]
                    for reload_path in RELOAD_PATHS:
                        r2 = sess.post(f"{host}{reload_path}?k={sitekey}", data={
                            "v": version, "reason": "q", "c": token1,
                            "k": sitekey, "co": co, "hl": "en", "size": "invisible",
                        }, headers={"Content-Type": "application/x-www-form-urlencoded",
                                    "Referer": au, "Origin": host})
                        m = re.search(r'\["rresp","([^"]+)"', r2.text)
                        if m and m.group(1):
                            tok = m.group(1)
                            logger.info(f"v2 token OK via {host}{reload_path}")
                            return tok
                except Exception:
                    continue
    return ""


def _paid_solve(sitekey, page_url, proxy_url, impersonate) -> str:
    if not (SERVICE and API_KEY and sitekey):
        return ""
    deadline = time.time() + MAX_WAIT
    kw = {"impersonate": impersonate, "timeout": 25}
    if SERVICE == "2captcha":
        try:
            with Session(**kw) as s:
                body = {"key": API_KEY, "method": "userrecaptcha",
                        "googlekey": sitekey, "pageurl": page_url,
                        "invisible": "1", "json": "1", "enterprise": "1"}
                if proxy_url:
                    for scheme in ("http://", "https://", "socks5://", "socks5h://"):
                        if proxy_url.startswith(scheme):
                            body["proxy"] = proxy_url[len(scheme):]
                            body["proxytype"] = scheme.rstrip(":/").upper()
                            break
                j = s.post("https://2captcha.com/in.php", data=body).json()
        except Exception:
            return ""
        if j.get("status") != 1: return ""
        tid = j["request"]
        while time.time() < deadline:
            time.sleep(POLL_SEC)
            try:
                with Session(**kw) as s:
                    j = s.get("https://2captcha.com/res.php", params={
                        "key": API_KEY, "action": "get", "id": tid, "json": "1"}).json()
            except Exception:
                continue
            if j.get("status") == 1: return j["request"]
            if j.get("request") == "CAPCHA_NOT_READY": continue
            return ""
        return ""
    if SERVICE == "capsolver":
        task = {"type": "ReCaptchaV3EnterpriseTask",
                "websiteURL": page_url, "websiteKey": sitekey,
                "pageAction": "submit"}
        if proxy_url: task["proxy"] = proxy_url
        try:
            with Session(**kw) as s:
                j = s.post("https://api.capsolver.com/createTask",
                           json={"clientKey": API_KEY, "task": task}).json()
        except Exception:
            return ""
        if j.get("errorId", 0) != 0: return ""
        tid = j["taskId"]
        while time.time() < deadline:
            time.sleep(POLL_SEC)
            try:
                with Session(**kw) as s:
                    j = s.post("https://api.capsolver.com/getTaskResult",
                               json={"clientKey": API_KEY, "taskId": tid}).json()
            except Exception:
                continue
            if j.get("status") == "ready":
                sol = j.get("solution", {}) or {}
                tok = sol.get("gRecaptchaResponse") or sol.get("token") or ""
                if tok: return tok
            if j.get("errorId", 0) != 0: return ""
        return ""
    return ""


def solve(sitekey, page_url, proxy_url="", user_agent=DEFAULT_UA,
          impersonate="chrome124") -> str:
    if not sitekey:
        return ""
    logger.info(f"solving captcha sitekey={sitekey[:16]}… page={page_url}")
    tok = _v3_solve(sitekey, page_url, proxy_url, user_agent, impersonate)
    if tok: return tok
    tok = _v2_solve(sitekey, page_url, proxy_url, user_agent, impersonate)
    if tok: return tok
    tok = _paid_solve(sitekey, page_url, proxy_url, impersonate)
    if tok: return tok
    logger.warning("all captcha paths failed")
    return ""
