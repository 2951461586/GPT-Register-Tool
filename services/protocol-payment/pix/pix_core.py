from __future__ import annotations

import json
import random
import re
import secrets
import time
import uuid
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, unquote, urljoin, urlparse, urlsplit, urlunsplit

import requests

try:
    from curl_cffi.requests import Session as CurlCffiSession
except Exception:  # pragma: no cover
    CurlCffiSession = None


CHATGPT_BASE_URL = "https://chatgpt.com"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

DEFAULT_STRIPE_PK = "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"

STRIPE_VERSION_FULL = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"

DEFAULT_STRIPE_RUNTIME_VERSION = "6f8494a281"

PAY_LONG_LINK_TIMEOUT = 30

COUNTRY_CURRENCY = {
    "AT": "EUR", "AU": "AUD", "BE": "EUR", "BR": "BRL", "CA": "CAD", "CH": "CHF", "CZ": "CZK",
    "DE": "EUR", "DK": "DKK", "ES": "EUR", "FI": "EUR", "FR": "EUR", "GB": "GBP", "HK": "HKD",
    "ID": "IDR", "IE": "EUR", "IN": "INR", "IT": "EUR", "JP": "JPY", "KR": "KRW", "MX": "MXN",
    "MY": "MYR", "NL": "EUR", "NO": "NOK", "NZ": "NZD", "PH": "PHP", "PL": "PLN", "PT": "EUR",
    "SE": "SEK", "SG": "SGD", "TH": "THB", "TW": "TWD", "US": "USD", "VN": "VND",
}

OPENAI_SUPPORTED_COUNTRY_CODES = {
    "AX", "AL", "DZ", "AS", "AD", "AO", "AI", "AQ", "AG", "AR",
    "AM", "AW", "AU", "AT", "AZ", "BS", "BH", "BD", "BB", "BE",
    "BZ", "BJ", "BM", "BT", "BO", "BQ", "BA", "BW", "BV", "BR",
    "IO", "BN", "BG", "BF", "BI", "CV", "KH", "CM", "CA", "KY",
    "CF", "TD", "CL", "CX", "CC", "CO", "KM", "CG", "CK", "CR",
    "CI", "HR", "CW", "CY", "CZ", "DK", "DJ", "DM", "DO", "EC",
    "SV", "GQ", "ER", "EE", "SZ", "FK", "FO", "FJ", "FI", "FR",
    "GF", "PF", "TF", "GA", "GM", "GE", "DE", "GH", "GI", "GR",
    "GL", "GD", "GP", "GU", "GT", "GG", "GN", "GW", "GY", "HT",
    "HM", "VA", "HN", "HU", "IS", "IN", "ID", "IQ", "IE", "IM",
    "IL", "IT", "JM", "JP", "JE", "JO", "KZ", "KE", "KI", "KW",
    "KG", "LA", "LV", "LB", "LS", "LR", "LI", "LT", "LU", "MG",
    "MW", "MY", "MV", "ML", "MT", "MH", "MQ", "MR", "MU", "YT",
    "MX", "FM", "MD", "MC", "MN", "ME", "MS", "MA", "MZ", "MM",
    "NA", "NR", "NP", "NL", "NC", "NZ", "NI", "NE", "NG", "NU",
    "NF", "MK", "MP", "NO", "OM", "PK", "PW", "PS", "PA", "PG",
    "PE", "PH", "PN", "PL", "PT", "PR", "QA", "RE", "RO", "RW",
    "BL", "SH", "KN", "LC", "MF", "PM", "VC", "WS", "SM", "ST",
    "SN", "RS", "SC", "SL", "SG", "SX", "SK", "SI", "SB", "SO",
    "ZA", "GS", "KR", "SS", "ES", "LK", "SR", "SJ", "SE", "CH",
    "TW", "TZ", "TH", "TL", "TG", "TK", "TO", "TT", "TN", "TR",
    "TM", "TC", "TV", "UG", "UA", "AE", "GB", "UM", "US", "UY",
    "UZ", "VU", "WF", "EH", "ZM",
}

COUNTRY_PHONE_PREFIX = {
    "AU": "+61", "CA": "+1", "DE": "+49", "GB": "+44", "IE": "+353", "JP": "+81",
    "NZ": "+64", "SG": "+65", "TH": "+66", "US": "+1",
    "AD": "+376", "AE": "+971", "AL": "+355", "AR": "+54", "AT": "+43", "BE": "+32",
    "BG": "+359", "BH": "+973", "BM": "+1", "BO": "+591", "BR": "+55", "CH": "+41",
    "CL": "+56", "CO": "+57", "CR": "+506", "CY": "+357", "CZ": "+420", "DK": "+45",
    "EE": "+372", "ES": "+34", "FI": "+358", "FR": "+33", "GI": "+350", "GR": "+30",
    "HK": "+852", "HU": "+36", "ID": "+62", "IL": "+972", "IN": "+91", "IS": "+354",
    "IT": "+39", "KR": "+82", "KZ": "+7", "LI": "+423", "LT": "+370", "LU": "+352",
    "LV": "+371", "MC": "+377", "MD": "+373", "ME": "+382", "MK": "+389", "MT": "+356",
    "MX": "+52", "MY": "+60", "NL": "+31", "NO": "+47", "PH": "+63", "PL": "+48",
    "PT": "+351", "QA": "+974", "RO": "+40", "RS": "+381", "SA": "+966", "SE": "+46",
    "SI": "+386", "SK": "+421", "SM": "+378", "TR": "+90", "TW": "+886", "UA": "+380",
    "UY": "+598", "ZA": "+27",
}

US_BILLING_NAMES = [("James", "Smith"), ("John", "Brown"), ("Michael", "Johnson"), ("Robert", "Miller"), ("David", "Davis"), ("William", "Wilson")]

US_BILLING_STREETS = [
    ("3110 Sunset Boulevard", "Los Angeles", "CA", "90026"),
    ("1200 Market Street", "San Francisco", "CA", "94102"),
    ("500 Main Street", "Austin", "TX", "78701"),
    ("88 Broadway", "New York", "NY", "10007"),
    ("1200 Peachtree St", "Atlanta", "GA", "30309"),
]

DE_BILLING_NAMES = [("Lukas", "Schneider"), ("Felix", "Muller"), ("Jonas", "Weber"), ("Leon", "Fischer"), ("Marie", "Wagner"), ("Laura", "Becker"), ("Maximilian", "Hoffmann"), ("Paul", "Schulz"), ("Emma", "Koch"), ("Hannah", "Bauer"), ("Sophie", "Richter"), ("Noah", "Klein")]

DE_BILLING_STREETS = [
    ("Friedrichstrasse 123", "Berlin", "BE", "10117"),
    ("Leopoldstrasse 50", "Munich", "BY", "80802"),
    ("Zeil 85", "Frankfurt am Main", "HE", "60313"),
    ("Konigsallee 60", "Dusseldorf", "NW", "40212"),
    ("Moenckebergstrasse 7", "Hamburg", "HH", "20095"),
    ("Hohenzollernring 72", "Cologne", "NW", "50672"),
    ("Kaiserstrasse 44", "Stuttgart", "BW", "70173"),
    ("Kaufingerstrasse 15", "Munich", "BY", "80331"),
    ("Georgstrasse 24", "Hanover", "NI", "30159"),
    ("Prager Strasse 9", "Dresden", "SN", "01069"),
    ("Schadowstrasse 36", "Dusseldorf", "NW", "40212"),
    ("Breite Strasse 18", "Bonn", "NW", "53111"),
]

GB_BILLING_NAMES = [("Oliver", "Smith"), ("George", "Taylor"), ("Harry", "Brown"), ("Noah", "Wilson"), ("Jack", "Davies"), ("Arthur", "Evans"), ("Olivia", "Johnson"), ("Amelia", "Roberts"), ("Isla", "Walker"), ("Ava", "Thompson"), ("Mia", "White"), ("Grace", "Hughes")]

GB_BILLING_STREETS = [
    ("221B Baker Street", "London", "England", "NW1 6XE"),
    ("10 Downing Street", "London", "England", "SW1A 2AA"),
    ("45 Deansgate", "Manchester", "England", "M3 2AY"),
    ("18 Park Row", "Leeds", "England", "LS1 5JA"),
    ("77 Queen Street", "Cardiff", "Wales", "CF10 2GR"),
    ("9 Princes Street", "Edinburgh", "Scotland", "EH2 2ER"),
    ("33 Broad Street", "Birmingham", "England", "B1 2HF"),
    ("14 Castle Street", "Liverpool", "England", "L2 0NE"),
    ("52 College Green", "Bristol", "England", "BS1 5SH"),
    ("6 Royal Avenue", "Belfast", "Northern Ireland", "BT1 1DA"),
]

AU_BILLING_NAMES = [("Jack", "Wilson"), ("Oliver", "Taylor"), ("Noah", "Brown"), ("Charlotte", "Smith"), ("Amelia", "Jones"), ("Isla", "Williams")]

AU_BILLING_STREETS = [
    ("120 Collins Street", "Melbourne", "Victoria", "3000"),
    ("88 George Street", "Sydney", "New South Wales", "2000"),
    ("45 Queen Street", "Brisbane", "Queensland", "4000"),
    ("22 King William Street", "Adelaide", "South Australia", "5000"),
    ("60 St Georges Terrace", "Perth", "Western Australia", "6000"),
    ("18 Elizabeth Street", "Hobart", "Tasmania", "7000"),
]

EXTRA_BILLING_NAMES = [("Alex", "Tan"), ("Daniel", "Lee"), ("Emma", "Wong"), ("Mia", "Chen"), ("Noah", "Martin"), ("Olivia", "Nguyen")]

EXTRA_BILLING_STREETS = {
    "TH": [("999 Rama I Road", "Bangkok", "Bangkok", "10330"), ("88 Sukhumvit Road", "Bangkok", "Bangkok", "10110"), ("45 Nimman Road", "Chiang Mai", "Chiang Mai", "50200")],
    "JP": [("1-1 Marunouchi", "Chiyoda-ku", "Tokyo", "100-0005"), ("2-2-1 Yaesu", "Chuo-ku", "Tokyo", "104-0028"), ("3-1 Umeda", "Osaka", "Osaka", "530-0001")],
    "SG": [("10 Anson Road", "Singapore", "Singapore", "079903"), ("1 Raffles Place", "Singapore", "Singapore", "048616"), ("80 Robinson Road", "Singapore", "Singapore", "068898")],
    "NZ": [("22 Queen Street", "Auckland", "Auckland", "1010"), ("50 Lambton Quay", "Wellington", "Wellington", "6011"), ("120 Hereford Street", "Christchurch", "Canterbury", "8011")],
    "CA": [("100 King Street West", "Toronto", "ON", "M5X 1A9"), ("555 West Hastings Street", "Vancouver", "BC", "V6B 4N6"), ("1250 Rene-Levesque Blvd", "Montreal", "QC", "H3B 4W8")],
    "IE": [("1 Grand Canal Square", "Dublin", "Dublin", "D02 P820"), ("10 South Mall", "Cork", "Cork", "T12 RD43"), ("5 Eyre Square", "Galway", "Galway", "H91 FPK2")],
    "BR": [
        ("Av. Paulista 1578", "Sao Paulo", "SP", "01310-200"),
        ("Rua da Consolacao 2302", "Sao Paulo", "SP", "01301-100"),
        ("Av. Rio Branco 156", "Rio de Janeiro", "RJ", "20040-901"),
        ("Rua Voluntarios da Patria 45", "Rio de Janeiro", "RJ", "22270-010"),
        ("Av. Afonso Pena 1500", "Belo Horizonte", "MG", "30130-921"),
        ("Rua dos Andradas 1234", "Porto Alegre", "RS", "90020-007"),
    ],
}

BILLING_PROFILE_CITY_BY_COUNTRY = {
    "AT": ["Vienna", "Graz", "Linz"], "BE": ["Brussels", "Antwerp", "Ghent"], "BR": ["Sao Paulo", "Rio de Janeiro", "Brasilia"],
    "CH": ["Zurich", "Geneva", "Basel"], "DK": ["Copenhagen", "Aarhus", "Odense"], "ES": ["Madrid", "Barcelona", "Valencia"],
    "FI": ["Helsinki", "Espoo", "Tampere"], "FR": ["Paris", "Lyon", "Marseille"], "ID": ["Jakarta", "Surabaya", "Bandung"],
    "IT": ["Rome", "Milan", "Turin"], "KR": ["Seoul", "Busan", "Incheon"], "MX": ["Mexico City", "Guadalajara", "Monterrey"],
    "NL": ["Amsterdam", "Rotterdam", "Utrecht"], "NO": ["Oslo", "Bergen", "Trondheim"], "PL": ["Warsaw", "Krakow", "Gdansk"],
    "PT": ["Lisbon", "Porto", "Coimbra"], "SE": ["Stockholm", "Gothenburg", "Malmo"], "TW": ["Taipei", "Taichung", "Kaohsiung"],
}

POSTAL_PATTERN_BY_COUNTRY = {
    "AD": "AD###", "AR": "C####", "AU": "####", "AT": "####", "BE": "####", "BR": "#####-###",
    "CA": "A#A #A#", "CH": "####", "CL": "#######", "CZ": "### ##", "DE": "#####", "DK": "####",
    "ES": "#####", "FI": "#####", "FR": "#####", "GB": "AA# #AA", "IE": "A## A###", "ID": "#####",
    "IN": "######", "IT": "#####", "JP": "###-####", "KR": "#####", "MX": "#####", "NL": "#### AA",
    "NO": "####", "NZ": "####", "PL": "##-###", "PT": "####-###", "SE": "### ##", "SG": "######",
    "TH": "#####", "US": "#####",
}

BILLING_STREET_POOL = ["Market Street", "Central Avenue", "Station Road", "Main Street", "High Street", "King Street"]

BILLING_PROFILE_BY_COUNTRY = {
    country: {
        "currency": COUNTRY_CURRENCY.get(country, "USD"),
        "phone_prefix": COUNTRY_PHONE_PREFIX.get(country, "+1"),
        "city_pool": BILLING_PROFILE_CITY_BY_COUNTRY.get(country, ["Capital City", "Central District", "Market Town"]),
        "postal_pattern": POSTAL_PATTERN_BY_COUNTRY.get(country, "#####"),
        "street_pool": BILLING_STREET_POOL,
    }
    for country in OPENAI_SUPPORTED_COUNTRY_CODES
}

LOCALE_MAP = {
    "de": ("de-DE", "de"), "en": ("en-US", "en"), "en-US": ("en-US", "en"), "es": ("es-ES", "es"),
    "fr": ("fr-FR", "fr"), "id": ("id-ID", "id"), "it": ("it-IT", "it"), "ja": ("ja-JP", "ja"),
    "ko": ("ko-KR", "ko"), "pt-BR": ("pt-BR", "pt-BR"), "zh-CN": ("zh-CN", "zh-CN"), "zh-TW": ("zh-TW", "zh-TW"),
}

def random_proxy_sid(length: int = 10) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(random.choice(alphabet) for _ in range(length))

def randomize_proxy_sid(proxy_url: str) -> str:
    text = str(proxy_url or "").strip()
    if not text:
        return ""
    sid = random_proxy_sid()
    parsed = urlsplit(text)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.lower() == "sid" for key, _value in query_pairs):
        query = urlencode([(key, sid if key.lower() == "sid" else value) for key, value in query_pairs])
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))

    netloc = parsed.netloc
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        new_userinfo = re.sub(r"(?i)(sid[-_=])([^-:@;&/?]+)", lambda m: f"{m.group(1)}{sid}", userinfo, count=1)
        if new_userinfo != userinfo:
            return urlunsplit((parsed.scheme, f"{new_userinfo}@{host}", parsed.path, parsed.query, parsed.fragment))

    new_text = re.sub(r"(?i)(sid[-_=])([^-:@;&/?]+)", lambda m: f"{m.group(1)}{sid}", text, count=1)
    return new_text

def find_access_token(value) -> str:
    if isinstance(value, dict):
        for key in ("accessToken", "access_token", "token"):
            token = str(value.get(key) or "").strip()
            if token:
                return token
        for item in value.values():
            token = find_access_token(item)
            if token:
                return token
    if isinstance(value, list):
        for item in value:
            token = find_access_token(item)
            if token:
                return token
    return ""

def extract_access_token_from_session_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    if raw.startswith("Bearer "):
        return raw.split(None, 1)[1].strip()
    try:
        return find_access_token(json.loads(raw))
    except Exception:
        pass
    match = re.search(r'"(?:accessToken|access_token|token)"\s*:\s*"([^"]+)"', raw)
    if match:
        return match.group(1).strip()
    return raw if raw.count(".") >= 2 and len(raw) > 80 else ""

def normalize_opll_country(country: str) -> str:
    country = str(country or "").strip().upper()
    return country if country in OPENAI_SUPPORTED_COUNTRY_CODES else "US"

def locale_parts(locale: str = "en") -> tuple[str, str]:
    return LOCALE_MAP.get(str(locale or "").strip(), LOCALE_MAP["en"])

def opll_extract_processor_entity(data) -> str:
    if not isinstance(data, dict):
        return ""
    direct = data.get("processor_entity") or data.get("processorEntity")
    if direct:
        return str(direct).strip()
    for key in ("checkout_session", "session", "checkout", "data"):
        nested = data.get(key)
        if isinstance(nested, dict):
            found = opll_extract_processor_entity(nested)
            if found:
                return found
    return ""

def opll_extract_stripe_publishable_key(data) -> str:
    if isinstance(data, str):
        match = re.search(r"pk_live_[A-Za-z0-9]+", data)
        return match.group(0) if match else ""
    if isinstance(data, dict):
        for key in ("stripe_publishable_key", "publishable_key", "publishableKey", "stripePublishableKey", "key"):
            found = opll_extract_stripe_publishable_key(data.get(key))
            if found:
                return found
        for item in data.values():
            found = opll_extract_stripe_publishable_key(item)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = opll_extract_stripe_publishable_key(item)
            if found:
                return found
    return ""

def opll_processor_entity_for_country(country: str, processor_entity: str = "") -> str:
    entity = str(processor_entity or "").strip()
    if entity:
        return entity
    return "openai_llc" if str(country or "").upper() == "US" else "openai_ie"

def opll_chatgpt_success_return_url(cs_id: str, country: str, processor_entity: str = "") -> str:
    entity = opll_processor_entity_for_country(country, processor_entity)
    return f"https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}&processor_entity={entity}&plan_type=plus"

def opll_to_openai_pay_url(stripe_hosted_url: str) -> str:
    url = str(stripe_hosted_url or "").strip()
    if not url:
        return ""
    if url.startswith("https://checkout.stripe.com"):
        return "https://pay.openai.com" + url[len("https://checkout.stripe.com"):]
    parsed = urlsplit(url)
    if parsed.netloc.lower() == "checkout.stripe.com":
        return urlunsplit((parsed.scheme or "https", "pay.openai.com", parsed.path, parsed.query, parsed.fragment))
    return url

def opll_stripe_checkout_long_url(cs_id: str, country: str, processor_entity: str = "") -> str:
    return (
        f"https://checkout.stripe.com/c/pay/{cs_id}"
        f"?returned_from_redirect=true&ui_mode=custom&return_url="
        f"{quote(opll_chatgpt_success_return_url(cs_id, country, processor_entity), safe='')}"
    )

def opll_stripe_confirm_return_url(cs_id: str, checkout: dict, stripe_hosted_url: str) -> str:
    hosted_url = opll_to_openai_pay_url(stripe_hosted_url) or opll_stripe_checkout_long_url(
        cs_id,
        checkout["billing_country"],
        checkout.get("processor_entity", ""),
    )
    if "pay.openai.com/" in hosted_url or "checkout.stripe.com/" in hosted_url:
        parsed = urlsplit(hosted_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault(
            "success_return_url",
            opll_chatgpt_success_return_url(
                cs_id,
                checkout["billing_country"],
                checkout.get("processor_entity", ""),
            ),
        )
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    return hosted_url

def opll_new_http_session() -> requests.Session:
    if CurlCffiSession is not None:
        session = CurlCffiSession(impersonate="chrome136")  # type: ignore[assignment]
    else:
        session = requests.Session()
    if hasattr(session, "trust_env"):
        session.trust_env = False
    return session

def opll_build_chatgpt_session(access_token: str, proxy_url: str = "") -> requests.Session:
    token = extract_access_token_from_session_text(access_token) or str(access_token or "").strip()
    if not token:
        raise RuntimeError("当前账号没有 Access Token，请先注册并获取 Session 信息")
    device_id = str(uuid.uuid4())
    session = opll_new_http_session()
    session.headers.update({
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Authorization": f"Bearer {token}",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "Content-Type": "application/json",
        "oai-device-id": device_id,
        "oai-language": "en-US",
        "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "Cookie": f"oai-did={device_id}",
    })
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    return session

OPLL_CHECKOUT_TRANSIENT_STATUSES = {403, 408, 425, 429, 500, 502, 503, 504}

OPLL_CHECKOUT_TRANSIENT_RETRY_MAX = 4

OPLL_CHECKOUT_TRANSIENT_RETRY_DELAY = 2.0

def opll_update_checkout_promotion(access_token: str, checkout: dict, proxy_url: str = "") -> dict:
    cs_id = str((checkout or {}).get("cs_id") or "").strip()
    if not cs_id:
        raise RuntimeError("checkout/update missing cs_id")
    processor_entity = str((checkout or {}).get("processor_entity") or "").strip() or opll_processor_entity_for_country(str((checkout or {}).get("billing_country") or "GB"))
    json_body = {
        "checkout_session_id": cs_id,
        "processor_entity": processor_entity,
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
    }
    referer = f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}"
    headers = {
        "Referer": referer,
        "x-openai-target-path": "/backend-api/payments/checkout/update",
        "x-openai-target-route": "/backend-api/payments/checkout/update",
    }
    response = None
    for attempt in range(OPLL_CHECKOUT_TRANSIENT_RETRY_MAX):
        session = opll_build_chatgpt_session(access_token, proxy_url)
        try:
            response = session.post(
                "https://chatgpt.com/backend-api/payments/checkout/update",
                json=json_body,
                headers=headers,
                timeout=PAY_LONG_LINK_TIMEOUT,
            )
        except Exception as exc:
            if attempt < OPLL_CHECKOUT_TRANSIENT_RETRY_MAX - 1:
                time.sleep(OPLL_CHECKOUT_TRANSIENT_RETRY_DELAY + random.random())
                continue
            raise RuntimeError(f"checkout/update failed: {exc}") from exc
        if response.status_code < 400:
            break
        if response.status_code in OPLL_CHECKOUT_TRANSIENT_STATUSES and attempt < OPLL_CHECKOUT_TRANSIENT_RETRY_MAX - 1:
            time.sleep(OPLL_CHECKOUT_TRANSIENT_RETRY_DELAY + random.random())
            continue
        raise RuntimeError(f"checkout/update failed: HTTP {response.status_code} {response.text[:500]}")
    payload = response.json() or {}
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(f"checkout/update rejected: {str(payload)[:500]}")
    return payload if isinstance(payload, dict) else {"response": payload}

def opll_stripe_key_for_checkout(checkout: dict | None = None) -> str:
    return str((checkout or {}).get("stripe_publishable_key") or "").strip() or DEFAULT_STRIPE_PK

def opll_build_stripe_session(proxy_url: str = "") -> requests.Session:
    session = opll_new_http_session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    return session

def opll_stripe_context(init_payload: dict, payment_locale: str = "en", ctx: dict | None = None) -> dict:
    _browser_locale, elements_locale = locale_parts(payment_locale)
    base = ctx or {}
    return {
        "stripe_js_id": str(base.get("stripe_js_id") or uuid.uuid4()),
        "elements_session_id": str(base.get("elements_session_id") or f"elements_session_{uuid.uuid4().hex[:11]}"),
        "elements_session_config_id": str(init_payload.get("config_id") or base.get("elements_session_config_id") or uuid.uuid4()),
        "config_id": str(init_payload.get("config_id") or ""),
        "init_checksum": str(init_payload.get("init_checksum") or ""),
        "checkout_amount": str(opll_expected_amount(init_payload)),
        "currency": str(init_payload.get("currency") or "").lower(),
        "locale": elements_locale,
        "runtime_version": str(base.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION),
    }

def opll_expected_amount(init_payload: dict) -> str:
    return opll_stripe_amount_info(init_payload)[0]

def opll_stripe_amount_info(init_payload) -> tuple[str, str]:
    if not isinstance(init_payload, dict):
        return "0", "missing_payload"
    total_summary = init_payload.get("total_summary") if isinstance(init_payload, dict) else None
    if isinstance(total_summary, dict) and total_summary.get("due") is not None:
        return str(total_summary.get("due")), "total_summary.due"
    invoice = init_payload.get("invoice") if isinstance(init_payload, dict) else None
    if isinstance(invoice, dict) and invoice.get("amount_due") is not None:
        return str(invoice.get("amount_due")), "invoice.amount_due"
    line_items = init_payload.get("line_items") if isinstance(init_payload, dict) else None
    if isinstance(line_items, list):
        total = 0
        found = False
        for item in line_items:
            if isinstance(item, dict) and item.get("amount") is not None:
                try:
                    total += int(item.get("amount") or 0)
                    found = True
                except Exception:
                    pass
        if found:
            return str(total), "line_items.amount"
    return "0", "fallback_zero"

def opll_amount_to_int(value) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except Exception:
        return None

def opll_random_postal_code(pattern: str) -> str:
    result = []
    for char in str(pattern or "#####"):
        if char == "#":
            result.append(str(random.randint(0, 9)))
        elif char == "A":
            result.append(chr(random.randint(ord("A"), ord("Z"))))
        else:
            result.append(char)
    return "".join(result)

def opll_billing_for_country(country: str) -> dict:
    country = normalize_opll_country(country)
    if country == "DE":
        first, last = random.choice(DE_BILLING_NAMES)
        line1, city, state, postal = random.choice(DE_BILLING_STREETS)
    elif country == "GB":
        first, last = random.choice(GB_BILLING_NAMES)
        line1, city, state, postal = random.choice(GB_BILLING_STREETS)
    elif country == "AU":
        first, last = random.choice(AU_BILLING_NAMES)
        line1, city, state, postal = random.choice(AU_BILLING_STREETS)
    elif country == "US":
        first, last = random.choice(US_BILLING_NAMES)
        line1, city, state, postal = random.choice(US_BILLING_STREETS)
    elif country in EXTRA_BILLING_STREETS:
        first, last = random.choice(EXTRA_BILLING_NAMES)
        line1, city, state, postal = random.choice(EXTRA_BILLING_STREETS[country])
    elif country in OPENAI_SUPPORTED_COUNTRY_CODES:
        profile = BILLING_PROFILE_BY_COUNTRY[country]
        first, last = random.choice(EXTRA_BILLING_NAMES)
        line1 = f"{random.randint(10, 999)} {random.choice(profile['street_pool'])}"
        city = random.choice(profile["city_pool"])
        state = country
        postal = opll_random_postal_code(str(profile.get("postal_pattern") or "#####"))
    else:
        raise RuntimeError(f"不支持的账单资料地区: {country}")
    suffix = random.randint(1000, 9999)
    phone_prefix = str(BILLING_PROFILE_BY_COUNTRY.get(country, {}).get("phone_prefix") or COUNTRY_PHONE_PREFIX.get(country, "+1"))
    return {
        "name": f"{first} {last}",
        "email": f"{first.lower()}.{last.lower()}{suffix}@example.com",
        "phone": f"{phone_prefix}{random.randint(100000000, 999999999)}",
        "country": country,
        "line1": line1,
        "city": city,
        "state": state,
        "postal_code": postal,
    }

def opll_short_error(detail: str, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", str(detail or "")).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."

def opll_stripe_error_summary(prefix: str, response) -> str:
    try:
        payload = response.json() or {}
    except Exception:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        error = {}
    extra_fields = error.get("extra_fields") if isinstance(error.get("extra_fields"), dict) else {}
    parts = []
    for label, value in (
        ("code", error.get("code")),
        ("decline_code", error.get("decline_code")),
        ("type", error.get("type")),
        ("message", error.get("message")),
        ("payment_method_type", extra_fields.get("payment_method_type")),
        ("confirm_error_reason", extra_fields.get("confirm_error_reason")),
        ("confirm_error_code", extra_fields.get("confirm_error_code")),
        ("confirm_error_message", extra_fields.get("confirm_error_message")),
    ):
        if value is not None and value != "":
            parts.append(f"{label}={opll_short_error(str(value), 180)}")
    if parts:
        return f"{prefix}: " + ", ".join(parts)
    return f"{prefix}: {opll_short_error(response.text, 500)}"

def opll_is_external_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)

def opll_is_paypal_url(value: str) -> bool:
    host = (urlsplit(value).netloc or "").lower()
    return host == "paypal.com" or host.endswith(".paypal.com") or host == "paypalobjects.com" or host.endswith(".paypalobjects.com")

def opll_is_paypal_ba_approve_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    if not (host == "paypal.com" or host.endswith(".paypal.com")):
        return False
    path = parsed.path.rstrip("/").lower()
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return path == "/agreements/approve" and bool(str(query.get("ba_token") or "").strip())

def opll_is_ignored_resource_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    ignored_hosts = {"stripe-camo.global.ssl.fastly.net", "files.stripe.com", "q.stripe.com", "js.stripe.com", "m.stripe.network"}
    ignored_suffixes = (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".ico", ".css", ".js", ".woff", ".woff2")
    if host in ignored_hosts or any(host.endswith(f".{item}") for item in ignored_hosts):
        return True
    return path.endswith(ignored_suffixes)

def opll_collect_urls(payload, urls: list[str] | None = None) -> list[str]:
    found = urls if urls is not None else []
    if isinstance(payload, str):
        for match in re.findall(r"https?://[^\s\"'<>]+", payload):
            found.append(match.rstrip("),.;]"))
    elif isinstance(payload, dict):
        for key, value in payload.items():
            if key in ("url", "return_url", "redirect_url", "redirect_to_url") and isinstance(value, str) and opll_is_external_url(value):
                found.append(value)
            else:
                opll_collect_urls(value, found)
    elif isinstance(payload, list):
        for item in payload:
            opll_collect_urls(item, found)
    return found

def opll_extract_redirect_to_url(payload) -> str:
    if not isinstance(payload, dict):
        urls = opll_collect_urls(payload)
        return next(
            (item for item in urls if opll_is_paypal_ba_approve_url(item)),
            next((item for item in urls if opll_is_paypal_url(item) and not opll_is_ignored_resource_url(item)), ""),
        )
    next_action = payload.get("next_action")
    if isinstance(next_action, dict) and next_action.get("type") == "redirect_to_url":
        redirect_to_url = next_action.get("redirect_to_url") or {}
        if isinstance(redirect_to_url, dict):
            url = str(redirect_to_url.get("url") or "").strip()
            if url:
                return url
    for key in ("setup_intent", "payment_intent"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = opll_extract_redirect_to_url(nested)
            if found:
                return found
    urls = opll_collect_urls(payload)
    return next(
        (item for item in urls if opll_is_paypal_ba_approve_url(item)),
        next((item for item in urls if opll_is_paypal_url(item) and not opll_is_ignored_resource_url(item)), ""),
    )

def opll_first_non_empty(values: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = str(values.get(key) or "").strip()
        if value:
            return value
    return ""

def opll_submission_attempt_failure_fields(submission) -> dict[str, str]:
    wanted = {"error", "code", "message", "reason", "failure_reason", "decline_code", "failure_code", "failure_message"}
    found: dict[str, str] = {}

    def walk(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key or "").strip()
                if normalized in wanted and normalized not in found:
                    if isinstance(item, (str, int, float, bool)):
                        text = str(item).strip()
                    elif isinstance(item, dict):
                        text = str(item.get("message") or item.get("code") or item.get("reason") or item.get("type") or "").strip()
                    else:
                        text = ""
                    if text:
                        found[normalized] = text[:240]
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    if isinstance(submission, dict):
        walk(submission)
    return found

def opll_find_submission_attempt(payload) -> dict:
    if isinstance(payload, dict):
        item = payload.get("submission_attempt")
        if isinstance(item, dict):
            return item
        for value in payload.values():
            found = opll_find_submission_attempt(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = opll_find_submission_attempt(value)
            if found:
                return found
    return {}

def opll_stripe_payload_diagnostics(payload, ctx: dict) -> str:
    if not isinstance(payload, dict):
        return f"payload_type={type(payload).__name__}"
    keys = ",".join(sorted(payload.keys())[:12])
    urls = opll_collect_urls(payload)
    paypal_count = sum(1 for item in urls if opll_is_paypal_url(item))
    ba_count = sum(1 for item in urls if opll_is_paypal_ba_approve_url(item))
    ignored_count = sum(1 for item in urls if opll_is_ignored_resource_url(item))
    submission = opll_find_submission_attempt(payload)
    submission_state = str(submission.get("state") or "") if isinstance(submission, dict) else ""
    submission_fields = opll_submission_attempt_failure_fields(submission)
    submission_reason = opll_first_non_empty(submission_fields, "reason", "failure_reason", "decline_code", "failure_code", "code")
    submission_code = opll_first_non_empty(submission_fields, "code", "decline_code", "failure_code")
    submission_message = opll_first_non_empty(submission_fields, "message", "failure_message", "error")
    return (
        f"submission_state={submission_state or '未知'}, submission_reason={submission_reason or '无'}, "
        f"submission_code={submission_code or '无'}, submission_message={submission_message or '无'}, "
        f"submission_attempt={bool(submission)}, paypal_urls={paypal_count}, ba_approve_urls={ba_count}, "
        f"urls={len(urls)}, ignored_resource_urls={ignored_count}, keys=[{keys}], "
        f"ctx_session={ctx.get('elements_session_id') or ''}"
    )

class OpllStripeRequiresApproval(Exception):
    pass

class OpllChatgptApproveBlocked(Exception):
    pass

OPLL_APPROVE_BURST_RESULTS = {"blocked", "exception"}

OPLL_APPROVE_BLOCKED_RETRY_MAX = 5

OPLL_APPROVE_BLOCKED_RETRY_BASE_DELAY = 2.5

OPLL_APPROVE_BLOCKED_ERROR_BACKOFF = 5.0

def opll_chatgpt_approve(chatgpt: requests.Session, cs_id: str, checkout: dict) -> None:
    entity = opll_processor_entity_for_country(checkout["billing_country"], checkout.get("processor_entity", ""))
    try:
        chatgpt.post(
            "https://chatgpt.com/backend-api/sentinel/ping",
            json={},
            headers={
                "Referer": "https://chatgpt.com/",
                "x-openai-target-path": "/backend-api/sentinel/ping",
                "x-openai-target-route": "/backend-api/sentinel/ping",
            },
            timeout=PAY_LONG_LINK_TIMEOUT,
        )
    except Exception:
        pass
    response = chatgpt.post(
        "https://chatgpt.com/backend-api/payments/checkout/approve",
        json={"checkout_session_id": cs_id, "processor_entity": entity},
        headers={"Referer": f"https://chatgpt.com/checkout/{entity}/{cs_id}", "x-openai-target-path": "/backend-api/payments/checkout/approve", "x-openai-target-route": "/backend-api/payments/checkout/approve"},
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"chatgpt approve failed: HTTP {response.status_code} {response.text[:500]}")
    try:
        result = (response.json() or {}).get("result")
    except Exception:
        result = ""
    normalized_result = str(result or "").strip().lower()
    if normalized_result in OPLL_APPROVE_BURST_RESULTS:
        body = opll_short_error(str(response.text or ""), 300)
        raise OpllChatgptApproveBlocked(f"chatgpt approve result={normalized_result!r} http={response.status_code} body={body}")
    if result != "approved":
        raise RuntimeError(f"chatgpt approve unexpected result: {result!r}")

def opll_chatgpt_approve_with_retry(access_token: str, cs_id: str, checkout: dict, proxy_url: str = "") -> requests.Session:
    last_error = ""
    last_was_blocked = False
    for _attempt in range(OPLL_APPROVE_BLOCKED_RETRY_MAX):
        try:
            chatgpt = opll_build_chatgpt_session(access_token, proxy_url)  # 每次新 session（新 oai-device-id）
            opll_chatgpt_approve(chatgpt, cs_id, checkout)
            return chatgpt
        except OpllChatgptApproveBlocked as exc:
            # blocked 是概率性风控，可重试（参考 fufu.best 升级重试）；抖动延迟后继续，不再 break
            last_error = str(exc)
            last_was_blocked = True
            time.sleep(OPLL_APPROVE_BLOCKED_RETRY_BASE_DELAY + random.random() * 0.5)
        except Exception as exc:
            last_error = str(exc)
            last_was_blocked = False
            # 403/网络异常多为速率拦截：退避更久，避免猛敲继续触发 403
            time.sleep(OPLL_APPROVE_BLOCKED_ERROR_BACKOFF + random.random())
    hint = "（概率性风控 blocked；可调大 OPLL_APPROVE_BLOCKED_RETRY_MAX，或换干净住宅代理提高基础通过率）" if last_was_blocked else ""
    raise RuntimeError(f"ChatGPT approve 连续 {OPLL_APPROVE_BLOCKED_RETRY_MAX} 次未通过{hint}: {last_error}")

def opll_stripe_confirm(stripe: requests.Session, cs_id: str, pm_id: str, stripe_pk: str, init_payload: dict, ctx: dict, checkout: dict, stripe_hosted_url: str, pm_type: str = "paypal") -> dict:
    return_url = opll_stripe_confirm_return_url(cs_id, checkout, stripe_hosted_url)
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    response = stripe.post(
        f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm",
        data={
            "guid": uuid.uuid4().hex,
            "muid": uuid.uuid4().hex,
            "sid": uuid.uuid4().hex,
            "payment_method": pm_id,
            "init_checksum": str(init_payload.get("init_checksum") or ctx.get("init_checksum") or ""),
            "version": runtime_version,
            "expected_amount": str(ctx.get("checkout_amount") or opll_expected_amount(init_payload)),
            "expected_payment_method_type": pm_type,
            "return_url": return_url,
            "elements_session_client[session_id]": ctx["elements_session_id"],
            "elements_session_client[locale]": str(ctx.get("locale") or "en"),
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
            "client_attribution_metadata[checkout_session_id]": cs_id,
            "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
            "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
            "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
            "client_attribution_metadata[merchant_integration_version]": "custom",
            "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
            "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
            "consent[terms_of_service]": "accepted",
            "key": stripe_pk,
            "_stripe_version": STRIPE_VERSION_FULL,
        },
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(opll_stripe_error_summary("stripe confirm failed", response))
    return response.json() or {}

OPLL_FREE_TRIAL_MAX_MINOR_UNITS = 50
