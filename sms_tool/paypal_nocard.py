"""PayPal 无卡协议支付模块 (plus-paypal no-card agreement payment)。

纯 HTTP 协议实现 PayPal 无卡签约支付 ChatGPT Plus，不启动浏览器。

流程：
  1. Stripe checkout → PayPal redirect URL (含 BA token)
  2. /agreements/approve → /checkoutweb/signup (获取 EC token + cookies)
  3. GraphQL: DeferredFeature / GriffinMetadata / CheckoutSessionData
  4. InitiateRiskBasedTwoFactorPhoneConfirmation (发送 SMS OTP)
  5. ConfirmRiskBasedTwoFactorPhoneConfirmation (确认 OTP)
  6. SignUpNewMemberMutation (无卡注册)
  7. /checkoutweb/drop → /webapps/hermes → billing.authorize

参考: DanOps-1/Gpt-Agreement-Payment plus-paypal 模块。
Source: https://github.com/DanOps-1/Gpt-Agreement-Payment
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import string
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    from curl_cffi.requests import Session as _CffiSession
    _HAS_CFFI = True
except ImportError:
    _CffiSession = None
    _HAS_CFFI = False

import requests

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────────

PP_ORIGIN = "https://www.paypal.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")

# ── 数据类型 ──────────────────────────────────────────────────────────────────


@dataclass
class Persona:
    first_name: str
    last_name: str
    email: str
    password: str
    line1: str
    city: str
    state: str
    postal_code: str
    country: str = "US"


@dataclass
class SignupResult:
    success: bool
    error: Optional[str] = None
    error_code: Optional[str] = None
    ec_token: Optional[str] = None
    ba_token: Optional[str] = None
    user_id: Optional[str] = None
    return_url: Optional[str] = None
    euat: Optional[str] = None
    persona: Optional[Persona] = None
    cookies: dict[str, str] = field(default_factory=dict)
    debug: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.persona is not None:
            d["persona"] = asdict(self.persona)
        return d


# ── 工具函数 ──────────────────────────────────────────────────────────────────

US_STATE_ABBR: dict[str, str] = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT",
    "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL",
    "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL",
    "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY",
    "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH",
    "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT",
    "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY",
}

_CC_TABLE = ("1", "33", "44", "49", "39", "34", "61", "81", "82", "852", "86",
             "91", "65", "62", "60", "63", "66", "84", "55", "52")


def _us_state_code(value: str) -> str:
    v = (value or "").strip()
    if len(v) == 2 and v.isalpha():
        return v.upper()
    return US_STATE_ABBR.get(v.upper(), v)


def _phone_split(e164: str) -> tuple[str, str]:
    raw = (e164 or "").strip()
    s = re.sub(r"\D", "", raw)
    if not raw.startswith("+") and len(s) == 10:
        return "1", s
    for cc in sorted(_CC_TABLE, key=len, reverse=True):
        if s.startswith(cc) and len(s) - len(cc) >= 7:
            return cc, s[len(cc):]
    raise ValueError(f"unparseable phone: {e164}")


def _rand_alnum(n: int) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _rand_password(n: int = 14) -> str:
    ll, lu = string.ascii_lowercase, string.ascii_uppercase
    d, sy = string.digits, "!@#$%^"
    chars = [random.choice(ll), random.choice(lu), random.choice(d), random.choice(sy)]
    pool = ll + lu + d + sy
    chars.extend(random.choice(pool) for _ in range(max(0, n - len(chars))))
    random.shuffle(chars)
    return "".join(chars)


def _rand_name() -> str:
    first = random.choice(["James", "John", "Robert", "Michael", "William", "David"])
    last = random.choice(["Smith", "Johnson", "Brown", "Williams", "Miller", "Davis"])
    return f"{first} {last}"


def _card_type(number: str) -> str:
    n = (number or "").strip().replace(" ", "")
    if n.startswith("4"):
        return "VISA"
    if n[:2].isdigit() and 51 <= int(n[:2]) <= 55:
        return "MASTERCARD"
    return "VISA"


# ── 配置加载 ──────────────────────────────────────────────────────────────────


def _load_config() -> dict[str, Any]:
    try:
        with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _next_from_pool(index_file: str, pool: list) -> int:
    """从轮询池中取下一个索引 (原子递增)。"""
    idx = 0
    try:
        path = os.path.join(PROJECT_ROOT, index_file)
        if os.path.exists(path):
            with open(path, "r") as f:
                idx = int(f.read().strip() or "0")
    except Exception:
        idx = 0
    if not pool:
        return 0
    next_idx = (idx + 1) % len(pool)
    try:
        os.makedirs(os.path.dirname(os.path.join(PROJECT_ROOT, index_file)), exist_ok=True)
        with open(os.path.join(PROJECT_ROOT, index_file), "w") as f:
            f.write(str(next_idx))
    except Exception:
        pass
    return idx


def get_next_card(cfg: dict[str, Any]) -> dict[str, str]:
    """从卡号池轮询取下一张卡。"""
    cards = (cfg.get("paypal_auto") or {}).get("cards") or []
    if not cards:
        raise RuntimeError("config.json 中 paypal_auto.cards 为空")
    idx_file = ((cfg.get("paypal_nocard") or {}).get("card_index_file")
                or "runtime/nocard_card_index.txt")
    idx = _next_from_pool(idx_file, cards)
    return cards[idx]


def get_next_phone(cfg: dict[str, Any]) -> dict[str, str]:
    """从手机号池轮询取下一个手机号。"""
    pool = (cfg.get("paypal_nocard") or {}).get("phone_pool") or []
    if not pool:
        raise RuntimeError("config.json 中 paypal_nocard.phone_pool 为空")
    idx_file = ((cfg.get("paypal_nocard") or {}).get("phone_index_file")
                or "runtime/nocard_phone_index.txt")
    idx = _next_from_pool(idx_file, pool)
    return pool[idx]


# ── HTTP Session ──────────────────────────────────────────────────────────────


def _make_session(proxy: Optional[str] = None) -> Any:
    if _HAS_CFFI:
        s = _CffiSession(impersonate=_paypal_impersonate())
        s.trust_env = False
        if proxy:
            p = proxy
            if p.startswith("socks5://"):
                p = "socks5h://" + p[len("socks5://"):]
            s.proxies = {"http": p, "https": p}
        else:
            s.proxies = {"http": "", "https": ""}
        return s
    s = requests.Session()
    s.trust_env = False
    s.headers["User-Agent"] = USER_AGENT
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def _paypal_impersonate() -> str:
    try:
        cfg = _load_config()
        value = str((cfg.get("paypal_nocard") or {}).get("impersonate") or "").strip()
        return value or "chrome136"
    except Exception:
        return "chrome136"


def _session_cookies(s: Any) -> dict[str, str]:
    try:
        return dict(s.cookies.get_dict())
    except Exception:
        try:
            return {c.name: c.value for c in s.cookies}
        except Exception:
            return {}


# ── BA Token 提取 ─────────────────────────────────────────────────────────────

_BA_RE = re.compile(r"BA-[A-Za-z0-9_.-]+")
_EC_RE = re.compile(r"(EC-[A-Z0-9]{17,})")


def extract_ba_token(paypal_redirect_url: str) -> Optional[str]:
    """从 Stripe 返回的 PayPal redirect URL 中提取 BA token。"""
    m = _BA_RE.search(paypal_redirect_url or "")
    return m.group(0) if m else None


def _mask_ba_token(value: str) -> str:
    def _mask(match: re.Match[str]) -> str:
        token = match.group(0)
        if len(token) <= 12:
            return "BA-***"
        return f"{token[:6]}...{token[-4:]}"

    return _BA_RE.sub(_mask, str(value or ""))


def _extract_paypal_approve_url(text: str) -> str:
    body = (
        str(text or "")
        .replace("\\u0026", "&")
        .replace("\\/", "/")
        .replace("&amp;", "&")
    )
    match = re.search(r"https?://(?:www\.)?paypal\.com/agreements/approve\?[^\s<>\"']+", body)
    if match:
        return match.group(0)
    match = re.search(r"ba_token=(BA-[A-Za-z0-9_.-]+)", body)
    if match:
        return f"{PP_ORIGIN}/agreements/approve?ba_token={urllib.parse.quote(match.group(1), safe='')}"
    return ""


def _follow_stripe_redirect(
    stripe_url: str,
    proxy: Optional[str] = None,
    timeout: int = 15,
    log=None,
) -> str:
    """Resolve Stripe pm-redirects to PayPal /agreements/approve when possible."""
    s = _make_session(proxy)
    current = (stripe_url or "").strip()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Referer": "https://checkout.stripe.com/",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }

    def _log(message: str):
        if log:
            log(message)
        else:
            logger.info(message)

    for step in range(1, 9):
        if extract_ba_token(current):
            _log(f"redirect step={step}: BA token already present in {_mask_ba_token(current[:140])}")
            return current
        if not current:
            break
        try:
            r = s.get(current, headers=headers, timeout=timeout, allow_redirects=False)
        except Exception as exc:
            _log(f"redirect step={step}: request failed {type(exc).__name__}: {exc}")
            break

        status = getattr(r, "status_code", "?")
        loc = (r.headers or {}).get("location") or (r.headers or {}).get("Location") or ""
        if loc:
            next_url = urllib.parse.urljoin(current, loc.replace("\\u0026", "&").replace("&amp;", "&"))
            _log(f"redirect step={step}: status={status} location={_mask_ba_token(next_url[:160])}")
            current = next_url
            continue

        try:
            body = (getattr(r, "text", "") or "")[:12000]
        except Exception:
            body = ""
        approve_url = _extract_paypal_approve_url(body)
        if approve_url:
            _log(f"redirect step={step}: status={status} body={_mask_ba_token(approve_url[:160])}")
            return approve_url

        final_url = str(getattr(r, "url", "") or current)
        _log(f"redirect step={step}: status={status} no Location/BA final={_mask_ba_token(final_url[:160])}")
        return final_url
    return current


def extract_ec_token(text: str) -> Optional[str]:
    m = _EC_RE.search(text or "")
    return m.group(1) if m else None


# ── SMS OTP 轮询 ──────────────────────────────────────────────────────────────

_OTP_RE = re.compile(r"(?:\b|:|：)(\d{4,8})(?:\b|$)")


def _sms_gateway_text(sms_api_url: str, proxy: Optional[str] = None) -> str:
    s = _make_session(proxy)
    try:
        r = s.get(sms_api_url, timeout=10)
        return (r.text or "").strip()
    except Exception:
        return ""


def wait_for_sms_otp(
    sms_api_url: str,
    *,
    after_ts: float,
    timeout: int = 180,
    poll_interval: float = 4.0,
    proxy: Optional[str] = None,
    baseline_text: str = "",
) -> str:
    """轮询 SMS 网关直到收到 OTP。

    支持两种 API 格式:
    - a.62-us.com: `yes|<digits>|...` / `no|...`
    - mail-api.yuecheng.shop: 直接返回短信文本
    """
    s = _make_session(proxy)
    deadline = time.time() + timeout
    last_text = ""
    while time.time() < deadline:
        try:
            r = s.get(sms_api_url, timeout=10)
            text = (r.text or "").strip()
        except Exception as e:
            logger.warning("sms poll error: %s", e)
            time.sleep(poll_interval)
            continue

        if text != last_text:
            logger.info("sms gateway: %s", text[:200])
            last_text = text

        if baseline_text and text == baseline_text:
            time.sleep(poll_interval)
            continue

        # 格式1: `no|<msg>|...` 或 `yes|<digits>|...`
        parts = text.split("|", 2)
        if len(parts) >= 2 and parts[0].lower() != "no":
            payload = parts[1]
            m = _OTP_RE.search(payload)
            if m:
                return m.group(1)
            digits = re.sub(r"\D", "", payload)
            if 4 <= len(digits) <= 8:
                return digits

        # 格式2: 直接从文本中提取数字
        m = _OTP_RE.search(text)
        if m:
            return m.group(1)

        time.sleep(poll_interval)
    raise TimeoutError(f"SMS OTP 未在 {timeout}s 内收到 (last: {last_text!r})")


# ── PayPal GraphQL ────────────────────────────────────────────────────────────

Q_DEFERRED = """query DeferredFeature($channel: String!, $countryCodeAsString: String!, $isBaslAsString: String!, $isForcedGuest: String!, $token: String!, $integrationType: String!) {
  otpLoginContext(token: $token, integrationType: $integrationType) {
    __typename
    context
  }
  elmoExperiment(
    app: "checkoutuinodeweb"
    filters: [{key: "Country", value: $countryCodeAsString}, {key: "Channel", value: $channel}, {key: "IsBasl", value: $isBaslAsString}, {key: "IsGuestOnly", value: $isForcedGuest}]
    res: "weasley:deferredFeature:memberAsDefault"
  ) {
    __typename
    treatments {
      __typename
      experimentId
      experimentName
      factors { __typename key value }
      treatmentId
      treatmentName
    }
  }
}
"""

Q_GRIFFIN_METADATA = """query GriffinMetadataQuery($countryCode: CountryCodes!, $languageCode: CheckoutContentLanguageCode!, $shippingCountryCode: CountryCodes!) {
  localeMetadata {
    address(countryCode: $countryCode, languageCode: $languageCode) {
      layout { maxLength minLength isRequired name regex __typename }
      strings { cityLabel line1Label line2Label optionalLabel postcodeLabel stateLabel stateList { displayText value __typename } __typename }
      __typename
    }
    shippingAddress: address(countryCode: $shippingCountryCode, languageCode: $languageCode) {
      layout { maxLength minLength isRequired name regex __typename }
      strings { cityLabel line1Label line2Label optionalLabel postcodeLabel stateLabel stateList { displayText value __typename } __typename }
      __typename
    }
    currencyCode(countryCode: $countryCode)
    phone(countryCode: $countryCode) { masks { mobile __typename } patterns { default __typename } __typename }
    __typename
  }
}
"""

Q_CHECKOUT_SESSION = """query CheckoutSessionDataQuery($token: String!) {
  checkoutSession(token: $token) {
    allowedCardIssuers
    cart {
      cancelUrl { href __typename }
      intent
      billingAddress { city country line1 line2 postalCode state formattedFullAddress __typename }
      shippingAddress { city country firstName isStoreAddress lastName line1 line2 postalCode state formattedFullAddress __typename }
      __typename
    }
    checkoutSessionType
    merchant { country merchantId name __typename }
    __typename
  }
}
"""

Q_INIT_OTP = """mutation InitiateRiskBasedTwoFactorPhoneConfirmationMutation($phoneNumber: String!, $locale: LocaleInput!, $phoneCountry: CountryCodes!, $token: String!) {
  initiateRiskBasedTwoFactorPhoneConfirmation(
    locale: $locale
    phoneCountry: $phoneCountry
    phoneNumber: $phoneNumber
    token: $token
  ) {
    authId
    challengeId
    state
    __typename
  }
}
"""

Q_CONFIRM_OTP = """mutation ConfirmRiskBasedTwoFactorPhoneConfirmationMutation($pin: String!, $authId: String!, $challengeId: String!, $token: String!) {
  confirmRiskBasedTwoFactorPhoneConfirmation(
    pin: $pin
    authId: $authId
    challengeId: $challengeId
    token: $token
  ) {
    authId
    challengeId
    state
    __typename
  }
}
"""

Q_SIGNUP = """mutation SignUpNewMemberMutation($bank: BankAccountInput, $billingAddress: AddressInput, $card: CardInput, $contentIdentifier: String, $country: CountryCodes, $countrySpecificFirstName: String, $countrySpecificLastName: String, $crsData: CommonReportingStandardsInput, $currencyConversionType: CheckoutCurrencyConversionType, $dateOfBirth: DateOfBirth, $email: String!, $firstName: String!, $gender: Gender, $identityDocument: IdentityDocumentInput, $lastName: String!, $middleName: String, $marketingOptOut: Boolean, $nationality: CountryCodes, $occupation: Occupation, $password: String, $phone: PhoneInput!, $placeOfBirth: CountryCodes, $secondaryIdentityDocument: IdentityDocumentInput, $selectedInstallmentOption: InstallmentsInput, $shareAddressWithDonatee: Boolean, $shippingAddress: AddressInput, $supportedThreeDsExperiences: [ThreeDSPaymentExperience], $token: String!, $residentialAddress: AddressInput, $isSignupIncentiveOptIn: Boolean, $isSignupIncentiveOptInStretch: Boolean, $legalAgreements: LegalAgreementsInput, $collectedConsents: [CollectedConsent]) {
  onboardAccount: signUpNewMember(
    bank: $bank
    billingAddress: $billingAddress
    card: $card
    contentIdentifier: $contentIdentifier
    countrySpecificFirstName: $countrySpecificFirstName
    countrySpecificLastName: $countrySpecificLastName
    country: $country
    crsData: $crsData
    currencyConversionType: $currencyConversionType
    dateOfBirth: $dateOfBirth
    email: $email
    firstName: $firstName
    gender: $gender
    identityDocument: $identityDocument
    lastName: $lastName
    middleName: $middleName
    marketingOptOut: $marketingOptOut
    nationality: $nationality
    occupation: $occupation
    password: $password
    phone: $phone
    placeOfBirth: $placeOfBirth
    secondaryIdentityDocument: $secondaryIdentityDocument
    selectedInstallmentOption: $selectedInstallmentOption
    shareAddressWithDonatee: $shareAddressWithDonatee
    shippingAddress: $shippingAddress
    token: $token
    residentialAddress: $residentialAddress
    isSignupIncentiveOptIn: $isSignupIncentiveOptIn
    isSignupIncentiveOptInStretch: $isSignupIncentiveOptInStretch
    legalAgreements: $legalAgreements
    collectedConsents: $collectedConsents
  ) {
    ...buyer
    flags { is3DSecureRequired __typename }
    ...fundingOptions
    paymentContingencies { ...threeDomainSecure __typename }
    __typename
  }
}

fragment buyer on CheckoutSession {
  buyer { auth { accessToken __typename } userId __typename }
  __typename
}

fragment fundingOptions on CheckoutSession {
  fundingOptions {
    allPlans { fundingSources { fundingInstrument { id __typename } amount { currencyCode currencyValue __typename } __typename } __typename }
    fundingInstrument { id lastDigits name nameDescription type __typename }
    __typename
  }
  __typename
}

fragment threeDomainSecure on PaymentContingencies {
  threeDomainSecure(experiences: $supportedThreeDsExperiences) {
    status
    redirectUrl { href __typename }
    method
    parameter
    experience
    __typename
  }
  __typename
}
"""

Q_AUTHORIZE = (
    "mutation authorize($billingAgreementId: String!, $addressId: String, "
    "$fundingPreference: billingFundingPreferenceInput, "
    "$legalAgreements: billingLegalAgreementsInput) { "
    "billing { authorize( billingAgreementId: $billingAgreementId "
    "addressId: $addressId fundingPreference: $fundingPreference "
    "legalAgreements: $legalAgreements ) { billingAgreementToken "
    "paymentAction returnURL { href __typename } buyer { userId __typename } "
    "__typename } __typename } }"
)


# ── GraphQL 请求 ──────────────────────────────────────────────────────────────


def _gql(
    s: Any,
    op_name: str,
    variables: dict[str, Any],
    query: str,
    *,
    signup_url: str,
    timeout: int = 30,
    extra_body: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"operationName": op_name, "variables": variables, "query": query}
    if extra_body:
        body.update(extra_body)
    token = str(variables.get("token") or "")
    country = (
        variables.get("country")
        or variables.get("countryCodeAsString")
        or (variables.get("locale") or {}).get("country")
        or "US"
    )
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": PP_ORIGIN,
        "Referer": signup_url,
        "X-Requested-With": "fetch",
        "X-App-Name": "checkoutuinodeweb_weasley",
        "PayPal-Client-Context": token,
        "PayPal-Client-Metadata-Id": token,
        "X-Country": str(country),
        "X-Locale": "en_US",
        "Sec-CH-UA": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    r = s.post(f"{PP_ORIGIN}/graphql", json=body, headers=headers, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"graphql {op_name} HTTP {r.status_code}: {r.text[:300]}")
    try:
        data = r.json()
    except Exception:
        text = r.text or ""
        if "authchallenge" in text[:1200].lower() or "captcha" in text[:1200].lower():
            raise RuntimeError(f"graphql {op_name}: PayPal returned captcha/challenge page")
        raise RuntimeError(f"graphql {op_name} JSON parse failed: {text[:200]}")
    if isinstance(data, dict) and data.get("errors"):
        msg = (data["errors"][0] or {}).get("message", "")
        logger.warning("graphql %s errors: %s", op_name, msg)
    return data


# ── FraudNet warmup ───────────────────────────────────────────────────────────

def _paypal_fraudnet_warmup(
    s: Any,
    *,
    ec_token: str,
    signup_url: str,
    ba_token: str,
    timeout: int = 20,
) -> None:
    """精简版 FraudNet warmup (c.paypal.com p1/p2/w)。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": PP_ORIGIN,
        "Referer": "https://www.paypal.com/",
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    now_ms = int(time.time() * 1000)
    app_id = "CHECKOUTUINODEWEB_ONBOARDING_LITE"

    # p1
    p1 = {
        "appName": "Netscape",
        "appVersion": "5.0 (Windows NT 10.0; Win64; x64)",
        "cookieEnabled": True,
        "language": "en-US",
        "onLine": True,
        "platform": "Win32",
        "userAgent": USER_AGENT,
        "screen": {"colorDepth": 24, "height": 900, "width": 1440},
        "time": now_ms,
        "tz": 28800000,
        "tzName": "Asia/Shanghai",
    }
    try:
        s.post(
            "https://c.paypal.com/v1/r/d/b/p1",
            json={"appId": app_id, "correlationId": ec_token, "payload": p1},
            headers=headers,
            timeout=min(timeout, 15),
        )
    except Exception as e:
        logger.debug("fraudnet p1 soft-failed: %s", e)

    # p2
    try:
        s.post(
            "https://c.paypal.com/v1/r/d/b/p2",
            json={"appId": app_id, "correlationId": ec_token, "payload": {
                "URL": signup_url, "tnt": "PP",
                "data": {"fts": now_ms},
                "sc": {"httpCookie": "", "sc-lst": ""},
                "pvc": 0,
                "pt2": {"pp2": "5.00", "cd2": "1.00", "cp": 1},
            }},
            headers=headers,
            timeout=min(timeout, 15),
        )
    except Exception as e:
        logger.debug("fraudnet p2 soft-failed: %s", e)

    # w
    try:
        s.post(
            "https://c.paypal.com/v1/r/d/b/w",
            json={"appId": app_id, "correlationId": ec_token, "payload": {
                "pkc": {"uvpa": 2, "cma": 1, "cc": 3, "ht": 3, "pkp": 3},
                "slt": random.randint(25, 450),
            }},
            headers=headers,
            timeout=min(timeout, 15),
        )
    except Exception as e:
        logger.debug("fraudnet w soft-failed: %s", e)


# ── 内容标识符 ────────────────────────────────────────────────────────────────

def _extract_content_identifier(html: str, locale_country: str, locale_lang: str) -> str:
    for pat in (
        r'"contentIdentifier"\s*:\s*"([^"]*signupTerms[^"]*)"',
        r'([A-Z]{2}:[a-z]{2}:[0-9a-f]{16,64}:compliance\.signupTerms)',
    ):
        m = re.search(pat, html or "", re.I)
        if m:
            return m.group(1).replace("\\/", "/")
    if locale_country.upper() == "US" and locale_lang.lower() == "en":
        return "US:en:f411614ea3eaac38abc54763fcfca00e:compliance.signupTerms"
    return f"{locale_country}:{locale_lang}:compliance.signupTerms"


# ── 构建 URL ──────────────────────────────────────────────────────────────────


def _build_signup_url(
    *,
    ba_token: str,
    ec_token: str,
    locale_country: str = "US",
    locale_lang: str = "en",
) -> str:
    params = [
        ("ul", "1"),
        ("country.x", locale_country),
        ("locale.x", f"{locale_lang}_{locale_country}"),
        ("modxo_redirect_reason", "guest_user"),
        ("ba_token", ba_token),
        ("token", ec_token),
        ("rcache", "1"),
        ("cookieBannerVariant", "hidden"),
    ]
    return f"{PP_ORIGIN}/checkoutweb/signup?{urllib.parse.urlencode(params)}"


def _build_onboard_url(
    *,
    ba_token: str,
    locale_country: str = "US",
    locale_lang: str = "en",
) -> str:
    params = [
        ("ul", "1"),
        ("country.x", locale_country),
        ("locale.x", f"{locale_lang}_{locale_country}"),
        ("modxo_redirect_reason", "guest_user"),
        ("ulOnboardRedirect", "true"),
        ("ba_token", ba_token),
    ]
    return f"{PP_ORIGIN}/agreements/approve?{urllib.parse.urlencode(params)}"


# ── Bootstrap (获取 EC token + cookies) ───────────────────────────────────────


def _bootstrap(
    s: Any,
    ba_token: str,
    *,
    locale_country: str = "US",
    locale_lang: str = "en",
    timeout: int = 30,
) -> tuple[str, str, str]:
    """GET /agreements/approve，获取 EC token 和 signup URL。

    Returns (ec_token, signup_url, signup_html)
    """
    locale = f"{locale_lang}_{locale_country}"
    url = (
        f"{PP_ORIGIN}/agreements/approve?"
        f"ba_token={urllib.parse.quote(ba_token)}"
        f"&country.x={urllib.parse.quote(locale_country)}"
        f"&locale.x={urllib.parse.quote(locale)}"
    )
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": f"{locale_lang}-{locale_country},{locale_lang};q=0.9,en;q=0.8",
        "Referer": "https://chatgpt.com/",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }

    r1 = s.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    html1 = r1.text or ""
    if r1.status_code != 200:
        if "datadome" in html1[:1500].lower() or "geo.ddc.paypal.com" in html1[:3000].lower():
            raise RuntimeError("PayPal DataDome 拦截 (需要代理或换 IP)")
        raise RuntimeError(f"/agreements/approve 失败: {r1.status_code}")

    ec_token = extract_ec_token(html1)
    if not ec_token:
        raise RuntimeError("/agreements/approve 响应中未找到 EC token")

    # 构建 onboard URL 并跳转到 /checkoutweb/signup
    onboard_url = _build_onboard_url(
        ba_token=ba_token,
        locale_country=locale_country,
        locale_lang=locale_lang,
    )
    r2 = s.get(
        onboard_url,
        headers={**headers, "Referer": url, "Sec-Fetch-Site": "same-origin"},
        timeout=timeout,
        allow_redirects=False,
    )
    loc = (r2.headers or {}).get("location") or (r2.headers or {}).get("Location") or ""
    if loc:
        loc_abs = urllib.parse.urljoin(str(r2.url or onboard_url), loc.replace("&amp;", "&"))
        m_ec = extract_ec_token(loc_abs)
        if m_ec:
            ec_token = m_ec

    signup_url = _build_signup_url(
        ba_token=ba_token,
        ec_token=ec_token,
        locale_country=locale_country,
        locale_lang=locale_lang,
    )

    # Prime signup page
    signup_html = ""
    try:
        r3 = s.get(
            signup_url,
            headers={**headers, "Referer": onboard_url, "Sec-Fetch-Site": "same-origin"},
            timeout=timeout,
            allow_redirects=False,
        )
        signup_html = r3.text or ""
        m_ec2 = extract_ec_token(signup_url) or extract_ec_token(signup_html)
        if m_ec2:
            ec_token = m_ec2
    except Exception as e:
        logger.warning("signup prime soft-failed: %s", e)

    return ec_token, signup_url, signup_html


# ── 签名变量构建 ──────────────────────────────────────────────────────────────


def _signup_variables(
    *,
    persona: Persona,
    ec_token: str,
    phone_e164: str,
    locale_country: str,
    locale_lang: str,
    content_identifier: str,
    card: dict[str, str],
    address: dict[str, str],
) -> dict[str, Any]:
    cc, num = _phone_split(phone_e164)
    first_name = persona.first_name or "James"
    last_name = persona.last_name or "Smith"
    addr_country = (address.get("country") or persona.country or locale_country).upper()
    state = _us_state_code(address.get("state") or persona.state)

    addr: dict[str, Any] = {
        "line1": address.get("line1") or persona.line1,
        "city": address.get("city") or persona.city,
        "postalCode": address.get("postal_code") or address.get("postalCode") or persona.postal_code,
        "accountQuality": {"autoCompleteType": "MANUAL", "isUserModified": False},
        "country": addr_country,
        "familyName": last_name,
        "givenName": first_name,
    }
    if addr_country == "US" and state:
        addr["state"] = state

    card_number = re.sub(r"\s+", "", str(card.get("number") or ""))
    exp_month = str(card.get("exp_month") or "12")
    exp_year = str(card.get("exp_year") or "2030")
    cvv = str(card.get("cvv") or card.get("cvc") or "")

    variables: dict[str, Any] = {
        "country": locale_country,
        "email": persona.email,
        "firstName": first_name,
        "lastName": last_name,
        "phone": {"countryCode": cc, "number": num, "type": "MOBILE"},
        "supportedThreeDsExperiences": ["IFRAME"],
        "token": ec_token,
        "billingAddress": addr,
        "shippingAddress": {
            "line1": "", "city": "", "state": "", "postalCode": "",
            "accountQuality": {"autoCompleteType": "MANUAL", "isUserModified": False},
            "country": addr_country,
            "familyName": last_name, "givenName": first_name,
        },
        "contentIdentifier": content_identifier,
        "marketingOptOut": False,
        "password": persona.password,
        "crsData": None,
        "legalAgreements": {},
        "card": {
            "cardNumber": card_number,
            "expirationDate": f"{exp_month}/{exp_year}",
            "securityCode": cvv,
            "type": _card_type(card_number),
        },
    }
    return variables


# ── 主入口: signup_no_card ────────────────────────────────────────────────────


def signup_no_card(
    ba_token: str,
    *,
    phone_e164: str,
    sms_api_url: str,
    card: dict[str, str],
    address: dict[str, str],
    proxy: Optional[str] = None,
    locale_country: str = "US",
    locale_lang: str = "en",
    otp_timeout: int = 180,
    request_timeout: int = 30,
) -> SignupResult:
    """执行 PayPal 无卡协议签约。

    Args:
        ba_token: Stripe → PayPal redirect URL 中的 BA-... token
        phone_e164: SMS 接收手机号 (e.g. "+14482162932")
        sms_api_url: SMS 拉码 API URL
        card: 卡号信息 {"number", "exp_month", "exp_year", "cvv"}
        address: 账单地址 {"line1", "city", "state", "postal_code"}
        proxy: 代理 URL
        locale_country: PayPal locale country
        locale_lang: PayPal locale lang
        otp_timeout: OTP 等待超时秒数
        request_timeout: HTTP 请求超时秒数

    Returns:
        SignupResult
    """
    s = _make_session(proxy)

    # 生成随机身份
    persona = Persona(
        first_name=random.choice(["James", "John", "Robert", "Michael", "William", "David"]),
        last_name=random.choice(["Smith", "Johnson", "Brown", "Williams", "Miller", "Davis"]),
        email=f"{_rand_alnum(16)}@gmail.com",
        password=_rand_password(),
        line1=address.get("line1", ""),
        city=address.get("city", ""),
        state=address.get("state", ""),
        postal_code=address.get("postal_code", ""),
        country="US",
    )
    logger.info("signup persona=%s %s <%s>", persona.first_name, persona.last_name, persona.email)

    # 1) Bootstrap → EC token + cookies + signup URL
    ec_token, signup_url, signup_html = _bootstrap(
        s, ba_token,
        locale_country=locale_country,
        locale_lang=locale_lang,
        timeout=request_timeout,
    )
    logger.info("ec_token=%s signup_url=%s", ec_token, signup_url[:120])

    # 2) GraphQL warmup: DeferredFeature
    try:
        _gql(s, "DeferredFeature", {
            "channel": "WEB",
            "countryCodeAsString": locale_country,
            "integrationType": "XoSignupAuth",
            "isBaslAsString": "false",
            "isForcedGuest": "false",
            "token": ec_token,
        }, Q_DEFERRED, signup_url=signup_url, timeout=request_timeout)
    except Exception as e:
        logger.warning("DeferredFeature soft-failed: %s", e)

    # 2b) GriffinMetadata + CheckoutSessionData
    try:
        _gql(s, "GriffinMetadataQuery", {
            "countryCode": locale_country,
            "languageCode": locale_lang,
            "shippingCountryCode": locale_country,
        }, Q_GRIFFIN_METADATA, signup_url=signup_url, timeout=request_timeout)
    except Exception as e:
        logger.warning("GriffinMetadata soft-failed: %s", e)

    try:
        _gql(s, "CheckoutSessionDataQuery", {"token": ec_token},
             Q_CHECKOUT_SESSION, signup_url=signup_url, timeout=request_timeout)
    except Exception as e:
        logger.warning("CheckoutSessionData soft-failed: %s", e)

    # FraudNet warmup
    try:
        _paypal_fraudnet_warmup(s, ec_token=ec_token, signup_url=signup_url,
                                ba_token=ba_token, timeout=request_timeout)
    except Exception as e:
        logger.debug("fraudnet warmup soft-failed: %s", e)

    # 3) Send SMS OTP
    content_identifier = _extract_content_identifier(signup_html, locale_country, locale_lang)
    sms_baseline = _sms_gateway_text(sms_api_url, proxy=proxy)
    sms_t0 = time.time()
    cc, num = _phone_split(phone_e164)
    phone_country = {"1": "US", "33": "FR", "44": "GB"}.get(cc, locale_country)

    init_resp = _gql(s, "InitiateRiskBasedTwoFactorPhoneConfirmationMutation", {
        "locale": {"country": locale_country, "lang": locale_lang},
        "phoneCountry": phone_country,
        "phoneNumber": num,
        "token": ec_token,
    }, Q_INIT_OTP, signup_url=signup_url, timeout=request_timeout)

    init_data = ((init_resp.get("data") or {})
                 .get("initiateRiskBasedTwoFactorPhoneConfirmation") or {})
    auth_id = init_data.get("authId")
    challenge_id = init_data.get("challengeId")
    if not auth_id or not challenge_id:
        return SignupResult(
            success=False, error="OTP init failed", error_code="OTP_INIT",
            ec_token=ec_token, ba_token=ba_token, persona=persona,
        )
    logger.info("otp init authId=%s challengeId=%s", auth_id, challenge_id)

    # 4) Poll SMS
    try:
        pin = wait_for_sms_otp(
            sms_api_url,
            after_ts=sms_t0,
            timeout=otp_timeout,
            proxy=proxy,
            baseline_text=sms_baseline,
        )
    except TimeoutError as e:
        return SignupResult(
            success=False, error=str(e), error_code="OTP_TIMEOUT",
            ec_token=ec_token, ba_token=ba_token, persona=persona,
        )
    logger.info("otp received: %s", pin)

    # 5) Confirm OTP
    conf_resp = _gql(s, "ConfirmRiskBasedTwoFactorPhoneConfirmationMutation", {
        "authId": auth_id,
        "challengeId": challenge_id,
        "pin": pin,
        "token": ec_token,
    }, Q_CONFIRM_OTP, signup_url=signup_url, timeout=request_timeout)

    conf_state = (((conf_resp.get("data") or {})
                   .get("confirmRiskBasedTwoFactorPhoneConfirmation") or {})
                  .get("state"))
    if conf_state != "CONFIRMED":
        return SignupResult(
            success=False, error=f"OTP confirm rejected: state={conf_state}",
            error_code="OTP_CONFIRM",
            ec_token=ec_token, ba_token=ba_token, persona=persona,
        )

    # 6) SignUpNewMember (no card → with card for agreement)
    variables = _signup_variables(
        persona=persona,
        ec_token=ec_token,
        phone_e164=phone_e164,
        locale_country=locale_country,
        locale_lang=locale_lang,
        content_identifier=content_identifier,
        card=card,
        address=address,
    )
    logger.info("signup card=%s ****%s", _card_type(card.get("number", "")),
                re.sub(r"\D", "", card.get("number", ""))[-4:])

    signup_resp = _gql(s, "SignUpNewMemberMutation", variables, Q_SIGNUP,
                       signup_url=signup_url, timeout=request_timeout)

    # 解析 signup 结果
    signup_errors = signup_resp.get("errors") or []
    onboard = (signup_resp.get("data") or {}).get("onboardAccount") or {}
    buyer = onboard.get("buyer") or {}
    euat = ((buyer.get("auth") or {}).get("accessToken") or "")
    user_id = buyer.get("userId") or ""

    if signup_errors and not euat:
        first_err = signup_errors[0] or {}
        return SignupResult(
            success=False,
            error=first_err.get("message", "signup failed"),
            error_code="SIGNUP_FAILED",
            ec_token=ec_token, ba_token=ba_token, persona=persona,
            debug={"signup_errors": signup_errors},
        )
    if not euat:
        return SignupResult(
            success=False, error="signup returned no accessToken",
            error_code="NO_EUAT",
            ec_token=ec_token, ba_token=ba_token, persona=persona,
        )

    # 7) authorize
    headers_html = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,*/*;q=0.8",
        "Referer": signup_url,
        "X-PayPal-Internal-EUAT": euat,
    }
    hermes_params = [
        ("ul", "1"),
        ("country.x", locale_country),
        ("locale.x", f"{locale_lang}_{locale_country}"),
        ("modxo_redirect_reason", "guest_user"),
        ("ba_token", ba_token),
        ("token", ec_token),
        ("rcache", "1"),
        ("cookieBannerVariant", "hidden"),
        ("fromSignupLite", "true"),
    ]
    hermes_url = f"{PP_ORIGIN}/webapps/hermes?{urllib.parse.urlencode(hermes_params)}"
    try:
        s.get(PP_ORIGIN + "/checkoutweb/drop", headers=headers_html, timeout=request_timeout)
        s.get(hermes_url, headers=headers_html, timeout=request_timeout)
    except Exception as e:
        logger.warning("drop/hermes soft-failed: %s", e)

    auth_resp = s.post(
        f"{PP_ORIGIN}/graphql/",
        json=[{
            "operationName": "authorize",
            "variables": {
                "billingAgreementId": ec_token,
                "fundingPreference": {"balancePreference": "OPT_OUT"},
                "legalAgreements": {},
            },
            "query": Q_AUTHORIZE,
        }],
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": PP_ORIGIN,
            "Referer": hermes_url,
            "X-Requested-With": "fetch",
            "X-App-Name": "checkoutuinodeweb",
            "X-PayPal-Internal-EUAT": euat,
        },
        timeout=request_timeout,
    )

    try:
        auth_data = auth_resp.json()[0]
    except Exception as e:
        return SignupResult(
            success=False, error=f"authorize parse: {e}: {auth_resp.text[:200]}",
            error_code="AUTHORIZE_PARSE",
            ec_token=ec_token, ba_token=ba_token, user_id=user_id,
            euat=euat, persona=persona,
        )

    authorize = ((auth_data.get("data") or {}).get("billing") or {}).get("authorize") or {}
    return_url = (authorize.get("returnURL") or {}).get("href")

    return SignupResult(
        success=bool(return_url),
        error=None if return_url else "authorize returned no URL",
        error_code=None if return_url else "AUTHORIZE_EMPTY",
        ec_token=ec_token,
        ba_token=authorize.get("billingAgreementToken") or ba_token,
        user_id=(authorize.get("buyer") or {}).get("userId") or user_id,
        return_url=return_url,
        euat=euat,
        persona=persona,
        cookies=_session_cookies(s),
    )


# ── 一键支付编排 ──────────────────────────────────────────────────────────────


def one_click_pay(
    access_token: str,
    *,
    card: dict[str, str],
    phone: dict[str, str],
    proxy: Optional[str] = None,
    cfg: Optional[dict[str, Any]] = None,
    paypal_url: Optional[str] = None,
    paypal_status: Optional[str] = None,
    paypal_updated_at: Optional[int] = None,
) -> dict[str, Any]:
    """一键支付: 生成 fresh PayPal 链接 → 解析 BA → 无卡协议签约。

    Args:
        access_token: ChatGPT access_token
        card: 卡号信息 {"number", "exp_month", "exp_year", "cvv"}
        phone: 手机号信息 {"phone", "sms_api_url"}
        proxy: 代理 URL
        cfg: 配置 (可选, 默认从 config.json 加载)
        paypal_url: SQLite/session 中已有的 PayPal redirect URL；刚重新生成的 link_ready URL 会优先使用
        paypal_status: SQLite/session 中的 paypal_status
        paypal_updated_at: SQLite/session 中的 paypal_updated_at

    Returns:
        {"ok": bool, "error": str, "return_url": str, ...}
    """
    if cfg is None:
        cfg = _load_config()

    nocard_cfg = cfg.get("paypal_nocard") or {}
    locale_country = nocard_cfg.get("locale_country") or "US"
    locale_lang = nocard_cfg.get("locale_lang") or "en"
    otp_timeout = int(nocard_cfg.get("otp_timeout") or 180)
    reuse_saved_url = bool(nocard_cfg.get("reuse_saved_url", False))
    reuse_saved_ready_url = bool(nocard_cfg.get("reuse_saved_ready_url", True))
    try:
        saved_url_max_age_seconds = int(nocard_cfg.get("saved_url_max_age_seconds", 1800))
    except Exception:
        saved_url_max_age_seconds = 1800
    fallback_to_saved_url = bool(nocard_cfg.get("fallback_to_saved_url", False))

    phone_e164 = phone.get("phone") or ""
    sms_api_url = phone.get("sms_api_url") or ""
    if not phone_e164 or not sms_api_url:
        return {"ok": False, "error": "手机号或 SMS API URL 为空"}

    # Step 1: 获取 PayPal redirect URL。默认 fresh 生成；旧 URL 只作显式复用/回退候选。
    saved_paypal_url = str(paypal_url or "").strip()
    saved_status = str(paypal_status or "").strip().lower()
    saved_updated_at = int(paypal_updated_at or 0)
    saved_age = int(time.time()) - saved_updated_at if saved_updated_at > 0 else None
    saved_ready_fresh = (
        bool(saved_paypal_url)
        and reuse_saved_ready_url
        and saved_status == "link_ready"
        and (
            saved_url_max_age_seconds <= 0
            or (saved_age is not None and 0 <= saved_age <= saved_url_max_age_seconds)
        )
    )
    paypal_url = ""
    used_existing = False
    link_result: dict[str, Any] = {}
    if saved_paypal_url and (reuse_saved_url or saved_ready_fresh):
        paypal_url = saved_paypal_url
        used_existing = True
        if saved_ready_fresh and not reuse_saved_url:
            print(
                f"[one-click-pay] 使用刚重新生成的 PayPal 链接: status=link_ready age={saved_age}s "
                f"url={_mask_ba_token(paypal_url[:120])}...",
                flush=True,
            )
        else:
            print(f"[one-click-pay] 配置为复用已有 PayPal 链接: {_mask_ba_token(paypal_url[:120])}...", flush=True)
    else:
        from . import gen_pp_link
        if saved_paypal_url:
            print("[one-click-pay] 已有 PayPal 链接仅作候选；优先重新生成 fresh 链接", flush=True)
        print(f"[one-click-pay] 生成 fresh PayPal 链接... proxy={proxy or 'DIRECT'}", flush=True)
        link_result = gen_pp_link.generate_pp_link(access_token, proxy=proxy)
        if not link_result.get("ok"):
            if saved_paypal_url and fallback_to_saved_url:
                paypal_url = saved_paypal_url
                used_existing = True
                print(
                    f"[one-click-pay] fresh 链接生成失败，按配置回退旧 PayPal 链接: {link_result.get('error')}",
                    flush=True,
                )
            else:
                return {"ok": False, "error": f"生成 PayPal 链接失败: {link_result.get('error')}"}
        else:
            paypal_url = str(link_result.get("url") or "").strip()
        if not paypal_url:
            if saved_paypal_url and fallback_to_saved_url:
                paypal_url = saved_paypal_url
                used_existing = True
                print("[one-click-pay] fresh PayPal redirect URL 为空，按配置回退旧链接", flush=True)
            else:
                return {"ok": False, "error": "PayPal redirect URL 为空"}

    # Step 2: 提取 BA token (先直接提取, 不行则 follow redirect)
    ba_token = extract_ba_token(paypal_url)
    if not ba_token:
        print(f"[one-click-pay] URL 中无 BA token, 跟踪重定向...", flush=True)
        resolved = _follow_stripe_redirect(
            paypal_url,
            proxy=proxy,
            log=lambda message: print(f"[one-click-pay] {message}", flush=True),
        )
        ba_token = extract_ba_token(resolved)
        if ba_token:
            paypal_url = resolved
    if not ba_token and used_existing and not reuse_saved_url:
        from . import gen_pp_link
        print("[one-click-pay] 已保存链接未解析到 BA，重新生成 fresh PayPal 链接...", flush=True)
        link_result = gen_pp_link.generate_pp_link(access_token, proxy=proxy)
        used_existing = False
        if not link_result.get("ok"):
            return {"ok": False, "error": f"已保存链接无 BA，重新生成 PayPal 链接失败: {link_result.get('error')}"}
        paypal_url = str(link_result.get("url") or "").strip()
        if not paypal_url:
            return {"ok": False, "error": "已保存链接无 BA，fresh PayPal redirect URL 为空"}
        ba_token = extract_ba_token(paypal_url)
        if not ba_token:
            print("[one-click-pay] fresh URL 中无 BA token, 跟踪重定向...", flush=True)
            resolved = _follow_stripe_redirect(
                paypal_url,
                proxy=proxy,
                log=lambda message: print(f"[one-click-pay] {message}", flush=True),
            )
            ba_token = extract_ba_token(resolved)
            if ba_token:
                paypal_url = resolved
    if not ba_token:
        return {"ok": False, "error": f"无法从 PayPal URL 提取 BA token: {_mask_ba_token(paypal_url[:120])}"}
    print(f"[one-click-pay] BA token: {_mask_ba_token(ba_token)}", flush=True)

    # Step 3: 执行无卡签约
    card_num = re.sub(r"\D", "", card.get("number", ""))
    card_display = f"{_card_type(card.get('number', ''))} ****{card_num[-4:]}"
    phone_display = phone_e164[-4:].rjust(len(phone_e164), "*")
    print(f"[one-click-pay] 执行 PayPal 无卡签约: card={card_display} phone=****{phone_display}", flush=True)

    try:
        result = signup_no_card(
            ba_token,
            phone_e164=phone_e164,
            sms_api_url=sms_api_url,
            card=card,
            address=_default_address(),
            proxy=proxy,
            locale_country=locale_country,
            locale_lang=locale_lang,
            otp_timeout=otp_timeout,
        )
    except Exception as exc:
        error = str(exc) or type(exc).__name__
        error_l = error.lower()
        error_code = "PAYPAL_DATADOME_BLOCKED" if "datadome" in error_l else "signup_exception"
        print(f"[one-click-pay] 签约异常: {error}", flush=True)
        return {
            "ok": False,
            "error": error,
            "error_code": error_code,
            "paypal_url": paypal_url,
            "ba_token": ba_token,
        }

    if result.success:
        print(f"[one-click-pay] 签约成功! return_url={result.return_url}", flush=True)
        ret: dict[str, Any] = {
            "ok": True,
            "return_url": result.return_url,
            "ba_token": result.ba_token,
            "user_id": result.user_id,
            "ec_token": result.ec_token,
            "paypal_url": paypal_url,
        }
        if not used_existing and link_result:
            ret["link_info"] = {k: v for k, v in link_result.items() if k != "url"}
        return ret
    else:
        print(f"[one-click-pay] 签约失败: {result.error} (code={result.error_code})", flush=True)
        return {
            "ok": False,
            "error": result.error,
            "error_code": result.error_code,
            "paypal_url": paypal_url,
            "debug": result.debug,
        }


def _default_address() -> dict[str, str]:
    """返回默认 US 账单地址。"""
    return {
        "line1": "123 Main St",
        "city": "New York",
        "state": "NY",
        "postal_code": "10001",
        "country": "US",
    }


# ── 批量一键支付 ──────────────────────────────────────────────────────────────


def one_click_pay_batch(args) -> None:
    """批量一键支付入口 (CLI 调用)。"""
    from .storage import mark_paypal_status, list_paypal_accounts

    cfg = _load_config()
    nocard_cfg = cfg.get("paypal_nocard") or {}
    if not nocard_cfg.get("enabled", True):
        print("[one-click-pay] paypal_nocard 未启用")
        return

    proxy = args.proxy
    emails: list[str] = []

    if getattr(args, "email", None):
        emails = [args.email.strip()]
    elif getattr(args, "email_file", None):
        with open(args.email_file, "r", encoding="utf-8") as f:
            emails = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    else:
        # 从 SQLite 获取所有待支付账号
        accounts = list_paypal_accounts()
        emails = [a.get("email") or a.get("identifier") or "" for a in accounts
                  if a.get("paypal_status") != "completed"]
        emails = [e for e in emails if e]

    if not emails:
        print("[one-click-pay] 没有待支付的账号")
        return

    print(f"[one-click-pay] 共 {len(emails)} 个账号待支付", flush=True)
    success_count = 0
    fail_count = 0

    for i, email in enumerate(emails, 1):
        print(f"\n[one-click-pay] === {i}/{len(emails)}: {email} ===", flush=True)

        # 从轮询池取资源
        card = get_next_card(cfg)
        phone = get_next_phone(cfg)

        # 获取 access_token (从 session JSON 或 SQLite)
        access_token = _get_access_token(email)
        if not access_token:
            print(f"[one-click-pay] 跳过: 无法获取 access_token", flush=True)
            fail_count += 1
            continue

        # 从 SQLite 获取旧 PayPal URL/状态，刚通过“重新生成支付链接”写入的 link_ready URL 会被优先使用。
        account_rows = list_paypal_accounts(email)
        account_row = account_rows[0] if account_rows else {}
        existing_url = str(account_row.get("paypal_url") or "").strip()

        result = one_click_pay(
            access_token,
            card=card,
            phone=phone,
            proxy=proxy,
            cfg=cfg,
            paypal_url=existing_url or None,
            paypal_status=account_row.get("paypal_status"),
            paypal_updated_at=account_row.get("paypal_updated_at") or account_row.get("updated_at"),
        )

        if result.get("ok"):
            mark_paypal_status(email, "completed")
            success_count += 1
            print(f"[one-click-pay] {email} 支付成功!", flush=True)
        else:
            fail_count += 1
            print(f"[one-click-pay] {email} 支付失败: {result.get('error')}", flush=True)

    print(f"\n[one-click-pay] 完成: 成功={success_count} 失败={fail_count} 总计={len(emails)}")


def _get_access_token(email: str) -> Optional[str]:
    """从 session JSON 或 SQLite 获取 access_token。"""
    from .storage import list_paypal_accounts
    import glob

    # 从 session JSON 文件查找
    sessions_dir = os.path.join(PROJECT_ROOT, "sessions")
    pattern = os.path.join(sessions_dir, f"session_{email}_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            at = data.get("access_token") or ""
            if at:
                return at
        except Exception:
            continue

    # 从 SQLite 查找
    try:
        accounts = list_paypal_accounts(email)
        for acc in accounts:
            at = acc.get("access_token") or ""
            if at:
                return at
    except Exception:
        pass

    return None
