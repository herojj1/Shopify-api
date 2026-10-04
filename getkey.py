"""
Standalone sitekey finder.
Run: python3 getkey.py https://store.com http://user:pass@host:port
or:  python3 getkey.py https://store.com host:port:user:pass
"""
import re
import sys
import html as _html
from urllib.parse import urlparse

from curl_cffi.requests import Session


def _normalize_proxy(raw):
    p = raw.strip()
    if not p:
        raise ValueError("empty proxy")
    if "://" in p:
        return p
    parts = p.split(":")
    if len(parts) == 4:
        return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    return "http://" + p


def main():
    if len(sys.argv) < 3:
        print("usage: getkey.py <shop_url> <proxy>")
        return

    shop = sys.argv[1].rstrip("/")
    if not shop.startswith(("http://", "https://")):
        shop = "https://" + shop

    try:
        proxy = _normalize_proxy(sys.argv[2])
    except Exception as e:
        print(f"[!] invalid proxy: {e}")
        return

    s = Session(impersonate="chrome124", proxy=proxy, timeout=20)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })

    print(f"[*] shop={shop}")
    print(f"[*] proxy={proxy.split('@')[-1] if '@' in proxy else proxy}")

    try:
        r = s.get(f"{shop}/products.json?limit=250")
        products = r.json().get("products", [])
    except Exception as e:
        print(f"[!] products.json failed: {e}")
        return

    variant = None
    for p in products:
        for v in p.get("variants", []):
            if v.get("available"):
                variant = v["id"]; break
        if variant: break
    if not variant:
        print("[!] no product available"); return
    print(f"[*] variant={variant}")

    try:
        r = s.get(f"{shop}/cart/{variant}:1", allow_redirects=True)
        checkout_html = r.text
        checkout_url  = r.url
    except Exception as e:
        print(f"[!] checkout fetch failed: {e}")
        return

    print(f"[*] checkout={checkout_url}")
    print(f"[*] html_len={len(checkout_html)}")

    decoded = _html.unescape(checkout_html).replace("&quot;", '"')
    found   = set()

    print("[*] scanning checkout HTML...")
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
        for m in re.finditer(pat, decoded):
            k = m.group(1)
            if k.startswith(("6L", "6A")):
                found.add(k)

    if not found:
        print("[*] scanning JS bundles...")
        js_urls = set()
        for m in re.finditer(r'<script[^>]+src="([^"]+)"', checkout_html):
            u = m.group(1)
            if u.startswith("//"):   u = "https:" + u
            elif u.startswith("/"):  u = shop + u
            elif not u.startswith("http"): continue
            js_urls.add(u)

        print(f"[*] {len(js_urls)} bundles found")
        for u in sorted(js_urls):
            try:
                r = s.get(u, headers={"Referer": checkout_url})
                if r.status_code != 200: continue
                body = _html.unescape(r.text).replace("&quot;", '"')
                for m in re.finditer(r'\b(6L[A-Za-z0-9_\-]{38})\b', body):
                    found.add(m.group(1))
                for m in re.finditer(
                    r'recaptcha/(?:enterprise|api2?)\.js\?render=([A-Za-z0-9_\-]{20,})',
                    body
                ):
                    found.add(m.group(1))
            except Exception:
                pass

    if not found:
        print("[*] scanning serialized meta tags...")
        for m in re.finditer(
            r'<meta\s+name="(serialized-[^"]+)"\s+content="([^"]*)"',
            checkout_html
        ):
            content = _html.unescape(m.group(2))
            for mm in re.finditer(r'\b(6L[A-Za-z0-9_\-]{38})\b', content):
                found.add(mm.group(1))

    print()
    host = urlparse(shop).hostname or ""

    if found:
        print("=== SITEKEY(S) FOUND ===")
        for k in found:
            print(k)
        print()
        print("Paste into captcha_solver.py:")
        print("SHOP_SITEKEYS = {")
        for k in found:
            print(f'    "{host}": "{k}",')
        print("}")
    else:
        print("=== NO SITEKEY FOUND ===")
        with open("checkout_dump.html", "w") as f:
            f.write(checkout_html)
        print("dumped to checkout_dump.html")


if __name__ == "__main__":
    main()
