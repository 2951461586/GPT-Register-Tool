#!/usr/bin/env python3
"""
临时独立模块：用 ChatGPT accessToken 创建 pay.openai.com Plus promo checkout 长链。

目标：只抽核心逻辑，方便临时复用/查看；不依赖 ChatStart 包，也不访问 pay.openai.com。
注意：不要把 access token / session token 打印到日志或提交到仓库。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

CHECKOUT_ENDPOINT = "https://chatgpt.com/backend-api/payments/checkout"
DEFAULT_PLAN_NAME = "chatgptplusplan"
DEFAULT_PROMO_CAMPAIGN_ID = "plus-1-month-free"
DEFAULT_COUNTRY = "US"
DEFAULT_CURRENCY = "USD"
DEFAULT_LANGUAGE = "en-US"
DEFAULT_CANCEL_URL = "https://chatgpt.com/"
COUNTRY_ALIASES = {
    "UK": "GB",
    "GBR": "GB",
    "UNITED KINGDOM": "GB",
    "USA": "US",
    "UNITED STATES": "US",
    "JAPAN": "JP",
    "GERMANY": "DE",
    "AUSTRALIA": "AU",
    "CANADA": "CA",
    "INDIA": "IN",
    "SINGAPORE": "SG",
    "TURKEY": "TR",
    "NETHERLANDS": "NL",
    "FRANCE": "FR",
}
COUNTRY_CURRENCY = {
    "US": "USD", "GB": "GBP", "DE": "EUR", "JP": "JPY", "AU": "AUD",
    "CA": "CAD", "IN": "INR", "SG": "SGD", "TR": "TRY", "NL": "EUR", "FR": "EUR",
}
COUNTRY_LANGUAGE = {
    "US": "en-US", "GB": "en-GB", "DE": "de-DE", "JP": "ja-JP", "AU": "en-AU",
    "CA": "en-CA", "IN": "en-IN", "SG": "en-SG", "TR": "tr-TR", "NL": "nl-NL", "FR": "fr-FR",
}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
OPENAI_PAY_PREFIX = "https://pay.openai.com/c/pay/"
STRIPE_PAY_PREFIX = "https://checkout.stripe.com/c/pay/"
# PK 由 OpenAI 下发，checkout 响应通常带 publishable_key；下面的硬编码仅为兜底。
# 用环境变量 PP_STRIPE_PUBLISHABLE_KEY 统一覆盖两处副本（本文件与
# sms_tool/gen_pp_link.py），避免 OpenAI 轮换 PK 时改代码。
DEFAULT_STRIPE_PK = (os.environ.get("PP_STRIPE_PUBLISHABLE_KEY", "") or "").strip() or (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1"
    "XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"
)
STRIPE_VERSION_FULL = os.environ.get(
    "MOMO_STRIPE_API_VERSION",
    "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1",
).strip()


def stripe_client_betas() -> list[str]:
    raw = str(os.environ.get("MOMO_STRIPE_CLIENT_BETAS") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                values = [str(value).strip() for value in parsed if str(value).strip()]
                if values:
                    return values[:8]
        except (TypeError, ValueError):
            values = [value.strip() for value in raw.split(",") if value.strip()]
            if values:
                return values[:8]
    return ["custom_checkout_server_updates_1", "custom_checkout_manual_approval_1"]


class PaylinkError(RuntimeError):
    """paylink 创建失败。"""


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    pay_url: str
    checkout_session_id: str
    processor_entity: str
    checkout_ui_mode: str
    tag: str
    client_secret: str = ""
    promo_campaign_id: str = ""
    country: str = ""
    currency: str = ""
    amount_due: str = ""
    zero_promo: bool = False
    hosted_exact: bool = False
    publishable_key: str = ""
    trial_eligible: bool = False
    stripe_hosted_url: str = ""

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "pay_url": self.pay_url,
            "checkout_session_id": self.checkout_session_id,
            "processor_entity": self.processor_entity,
            "checkout_ui_mode": self.checkout_ui_mode,
            "tag": self.tag,
            "client_secret": self.client_secret,
            "promo_campaign_id": self.promo_campaign_id,
            "country": self.country,
            "currency": self.currency,
            "amount_due": self.amount_due,
            "zero_promo": self.zero_promo,
            "hosted_exact": self.hosted_exact,
            "publishable_key": self.publishable_key,
            "trial_eligible": self.trial_eligible,
            "stripe_hosted_url": self.stripe_hosted_url,
        }


def read_text(path: str | None) -> str:
    if not path or path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as file:
        return file.read()


def parse_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise PaylinkError("empty JSON input")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        try:
            value = json.loads("{" + raw.strip().strip(",") + "}")
        except json.JSONDecodeError as exc:
            raise PaylinkError(f"invalid JSON input: {exc}") from exc
    if not isinstance(value, dict):
        raise PaylinkError("JSON root must be an object")
    return value


def normalize_checkout_country(value: str) -> str:
    raw = str(value or "").strip().upper().replace("_", " ")
    return COUNTRY_ALIASES.get(raw, raw) if raw else DEFAULT_COUNTRY


def currency_for_country(country: str) -> str:
    return COUNTRY_CURRENCY.get(normalize_checkout_country(country), DEFAULT_CURRENCY)


def language_for_country(country: str) -> str:
    return COUNTRY_LANGUAGE.get(normalize_checkout_country(country), DEFAULT_LANGUAGE)


def _first_string(value: Any, keys: tuple[str, ...]) -> str:
    if not isinstance(value, dict):
        return ""
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    return ""


def _nested_dict(value: Any, key: str) -> dict[str, Any]:
    if isinstance(value, dict) and isinstance(value.get(key), dict):
        return value[key]
    return {}


def extract_access_token(raw: str) -> str:
    text = str(raw or "").strip()
    if JWT_RE.fullmatch(text):
        return text
    try:
        data = parse_json_object(text)
    except PaylinkError:
        match = re.search(r'"accessToken"\s*:\s*"(' + JWT_RE.pattern + r')"', text)
        if match:
            return match.group(1)
        match = re.search(r'"access_token"\s*:\s*"(' + JWT_RE.pattern + r')"', text)
        if match:
            return match.group(1)
        raise

    chat_session = _nested_dict(data, "chatgpt_session")
    session_data = _nested_dict(chat_session, "session_data")
    token = _first_string(data, ("accessToken", "access_token"))
    token = token or _first_string(chat_session, ("accessToken", "access_token"))
    token = token or _first_string(session_data, ("accessToken", "access_token"))
    if not JWT_RE.fullmatch(token):
        raise PaylinkError("missing or invalid accessToken")
    return token


def extract_session_token(raw: str) -> str:
    try:
        data = parse_json_object(raw)
    except PaylinkError:
        match = re.search(r'"sessionToken"\s*:\s*"([^"\s]+)"', str(raw or ""))
        return match.group(1).strip() if match else ""
    chat_session = _nested_dict(data, "chatgpt_session")
    session_data = _nested_dict(chat_session, "session_data")
    return (
        _first_string(data, ("sessionToken", "session_token"))
        or _first_string(chat_session, ("sessionToken", "session_token"))
        or _first_string(session_data, ("sessionToken", "session_token"))
    )


def extract_cookies(raw: str) -> str:
    try:
        data = parse_json_object(raw)
    except PaylinkError:
        return ""
    chat_session = _nested_dict(data, "chatgpt_session")
    return _first_string(data, ("cookies",)) or _first_string(chat_session, ("cookies",))


def build_checkout_payload(
    *,
    plan_name: str = DEFAULT_PLAN_NAME,
    country: str = DEFAULT_COUNTRY,
    currency: str = DEFAULT_CURRENCY,
    promo_campaign_id: str = DEFAULT_PROMO_CAMPAIGN_ID,
    cancel_url: str = DEFAULT_CANCEL_URL,
) -> dict[str, Any]:
    country = normalize_checkout_country(country)
    currency = str(currency or "").strip().upper() or currency_for_country(country)
    payload: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": plan_name,
        "billing_details": {"country": country, "currency": currency},
        "cancel_url": cancel_url,
        "checkout_ui_mode": "hosted",
    }
    if promo_campaign_id:
        payload["promo_campaign"] = {
            "promo_campaign_id": promo_campaign_id,
            "is_coupon_from_query_param": False,
        }
    return payload


def normalize_proxy_url(proxy: str | None) -> str:
    value = str(proxy or "").strip()
    if not value:
        return ""
    if "://" in value:
        return value
    parts = value.split(":", 3)
    if len(parts) != 4:
        raise PaylinkError("proxy must be URL or host:port:user:pass")
    host, port, user, password = parts
    user_q = urllib.parse.quote(user, safe="")
    pass_q = urllib.parse.quote(password, safe="")
    return f"http://{user_q}:{pass_q}@{host}:{port}"


def build_opener(proxy_url: str = "") -> urllib.request.OpenerDirector:
    if not proxy_url:
        return urllib.request.build_opener()
    handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    return urllib.request.build_opener(handler)


def build_headers(access_token: str, *, session_token: str = "", cookies: str = "", language: str = DEFAULT_LANGUAGE) -> dict[str, str]:
    token = str(access_token or "").strip()
    if not JWT_RE.fullmatch(token):
        raise PaylinkError("access_token must be a JWT string")
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "oai-device-id": str(uuid.uuid4()),
        "oai-language": str(language or DEFAULT_LANGUAGE),
    }
    cookie = str(cookies or "").strip()
    if not cookie and session_token:
        cookie = "__Secure-next-auth.session-token=" + session_token.strip()
    if cookie:
        headers["Cookie"] = cookie
    return headers


def extract_checkout_session_id(value: dict[str, Any] | str, checkout_url: str = "") -> str:
    if isinstance(value, dict):
        for key in ("checkout_session_id", "checkoutSessionId", "id"):
            item = str(value.get(key) or "").strip()
            if item.startswith("cs_"):
                return item
        url = checkout_url or str(value.get("url") or value.get("redirect_url") or "")
    else:
        url = str(value or checkout_url or "")
    match = re.search(r"/c/pay/([^/?#]+)", urllib.parse.urlparse(url).path)
    return match.group(1) if match else ""


def extract_client_secret_fragment(client_secret: str) -> str:
    secret = str(client_secret or "").strip()
    marker = "_secret_"
    if marker not in secret:
        return ""
    return secret.split(marker, 1)[1].strip()


def to_openai_pay_url(url: str) -> str:
    text = str(url or "").strip()
    if text.startswith(STRIPE_PAY_PREFIX):
        return OPENAI_PAY_PREFIX + text[len(STRIPE_PAY_PREFIX) :]
    try:
        parsed = urllib.parse.urlsplit(text)
    except Exception:
        return text
    if (parsed.netloc or "").lower() == "checkout.stripe.com":
        return urllib.parse.urlunsplit((parsed.scheme or "https", "pay.openai.com", parsed.path, parsed.query, parsed.fragment))
    return text


def is_browser_openable_hosted_url(url: str) -> bool:
    """True only for real Stripe hosted checkout pages with a full fragment.

    Reconstructing ``#fragment`` from custom ``client_secret`` is NOT enough and
    opens as Stripe "Something went wrong / page not found".  The fragment must
    come from Stripe ``payment_pages/{cs}/init`` -> ``stripe_hosted_url``.
    """
    text = str(url or "").strip()
    if not (text.startswith(OPENAI_PAY_PREFIX) or text.startswith(STRIPE_PAY_PREFIX)):
        return False
    parsed = urllib.parse.urlsplit(text)
    fragment = parsed.fragment or ""
    # Real hosted fragments are long page secrets, not the short custom secret tail.
    return len(fragment) >= 80


def build_hosted_pay_url(checkout_session_id: str, client_secret: str = "", explicit_url: str = "") -> str:
    """Prefer an explicit Stripe-hosted URL; never treat custom client_secret as final.

    Returns a *candidate* URL. Callers that need a browser-openable link must still
    pass it through Stripe init (see :func:`resolve_real_hosted_checkout`).
    """
    explicit = to_openai_pay_url(str(explicit_url or "").strip())
    if is_browser_openable_hosted_url(explicit):
        return explicit
    if explicit.startswith(OPENAI_PAY_PREFIX) or explicit.startswith(STRIPE_PAY_PREFIX):
        return explicit
    cs = str(checkout_session_id or "").strip()
    if not cs.startswith("cs_"):
        cs = extract_checkout_session_id(explicit) or extract_checkout_session_id(cs)
    if not cs.startswith("cs_"):
        return ""
    # Protocol-only base (not browser-safe by itself).
    return f"{OPENAI_PAY_PREFIX}{cs}"


def extract_promo_campaign_id(raw: dict[str, Any]) -> str:
    promo = raw.get("promo_campaign")
    if isinstance(promo, dict):
        value = str(promo.get("promo_campaign_id") or promo.get("id") or "").strip()
        if value:
            return value
    return str(raw.get("promo_campaign_id") or "").strip()


def extract_amount_due_cents(init_payload: dict[str, Any]) -> int | None:
    if not isinstance(init_payload, dict):
        return None
    total_summary = init_payload.get("total_summary")
    if isinstance(total_summary, dict) and total_summary.get("due") is not None:
        try:
            return int(total_summary.get("due"))
        except (TypeError, ValueError):
            pass
    invoice = init_payload.get("invoice")
    if isinstance(invoice, dict) and invoice.get("amount_due") is not None:
        try:
            return int(invoice.get("amount_due"))
        except (TypeError, ValueError):
            pass
    return None


def require_zero_enabled() -> bool:
    return str(os.environ.get("PP_REQUIRE_ZERO_PROMO", "1") or "1").strip().lower() in ("1", "true", "yes", "on")


class _StripeInitProxyError(RuntimeError):
    """Proxy-level failure (e.g. HTTP 407) during the Playwright Stripe init.

    Distinct from :class:`PaylinkError` so it is not treated as a final Stripe
    answer: it means the request never reached Stripe, so the caller should fall
    back to the ``requests`` transport (which authenticates the proxy correctly).
    """


def _playwright_proxy(proxy_url: str) -> dict[str, str]:
    """Build a Playwright proxy dict with credentials split from the server URL.

    Playwright/Chromium ignores a ``user:pass@`` embedded in ``server`` and sends
    no Proxy-Authorization header, so an authenticated proxy answers 407. The
    credentials must be passed as separate ``username``/``password`` keys.
    """
    parts = urllib.parse.urlsplit(proxy_url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    proxy: dict[str, str] = {"server": f"{parts.scheme or 'http'}://{host}"}
    if parts.username:
        proxy["username"] = urllib.parse.unquote(parts.username)
    if parts.password:
        proxy["password"] = urllib.parse.unquote(parts.password)
    return proxy


def stripe_init_payment_page(
    checkout_session_id: str,
    publishable_key: str,
    *,
    proxy: str = "",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Call Stripe payment_pages/{cs}/init and return JSON.

    On this Windows runtime, plain urllib/requests often cannot complete TLS to
    api.stripe.com, while Playwright's Chromium network stack can. Prefer
    Playwright APIRequestContext with Origin https://js.stripe.com.
    """
    cs = str(checkout_session_id or "").strip()
    pk = str(publishable_key or "").strip() or DEFAULT_STRIPE_PK
    if not cs.startswith("cs_"):
        raise PaylinkError(f"invalid checkout_session_id for stripe init: {cs!r}")

    form = {
        "browser_locale": "en-US",
        "browser_timezone": "America/Los_Angeles",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
        "elements_session_client[locale]": "en",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    for index, beta in enumerate(stripe_client_betas()):
        form[f"elements_session_client[client_betas][{index}]"] = beta
    url = f"https://api.stripe.com/v1/payment_pages/{cs}/init"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": DEFAULT_USER_AGENT,
    }
    proxy_url = normalize_proxy_url(proxy)
    errors: list[str] = []

    # 1) Playwright Chromium network (most reliable on this machine)
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as exc:
        errors.append(f"playwright_import:{exc}")
        sync_playwright = None  # type: ignore

    if sync_playwright is not None:
        try:
            with sync_playwright() as p:
                launch_kwargs: dict[str, Any] = {"headless": True}
                if proxy_url:
                    launch_kwargs["proxy"] = _playwright_proxy(proxy_url)
                browser = None
                for channel in ("chrome", "msedge", ""):
                    try:
                        if channel:
                            browser = p.chromium.launch(channel=channel, **launch_kwargs)
                        else:
                            browser = p.chromium.launch(**launch_kwargs)
                        break
                    except Exception as launch_exc:
                        errors.append(f"launch:{channel or 'bundled'}:{launch_exc}")
                if browser is None:
                    raise PaylinkError("playwright browser launch failed")
                try:
                    context = browser.new_context()
                    response = context.request.post(
                        url,
                        form=form,
                        headers=headers,
                        timeout=int(max(5.0, timeout) * 1000),
                    )
                    text = response.text()
                    if response.status == 407:
                        # Proxy auth failure — the request never reached Stripe.
                        # Fall back to requests, which authenticates the proxy.
                        raise _StripeInitProxyError(f"proxy_407:{text[:200]}")
                    if response.status >= 400:
                        raise PaylinkError(f"stripe init HTTP {response.status}: {text[:400]}")
                    payload = json.loads(text)
                    if not isinstance(payload, dict):
                        raise PaylinkError("stripe init returned non-object JSON")
                    return payload
                finally:
                    browser.close()
        except PaylinkError:
            raise
        except Exception as exc:
            errors.append(f"playwright:{type(exc).__name__}:{exc}")

    # 2) requests fallback
    try:
        import requests  # type: ignore
    except Exception as exc:
        errors.append(f"requests_import:{exc}")
        requests = None  # type: ignore
    if requests is not None:
        try:
            proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
            response = requests.post(url, data=form, headers=headers, timeout=timeout, proxies=proxies)
            if response.status_code >= 400:
                raise PaylinkError(f"stripe init HTTP {response.status_code}: {response.text[:400]}")
            payload = response.json()
            if not isinstance(payload, dict):
                raise PaylinkError("stripe init returned non-object JSON")
            return payload
        except PaylinkError:
            raise
        except Exception as exc:
            errors.append(f"requests:{type(exc).__name__}:{exc}")

    raise PaylinkError("stripe init failed: " + " | ".join(errors[:4]))


def resolve_real_hosted_checkout(
    checkout_session_id: str,
    publishable_key: str,
    *,
    proxy: str = "",
    timeout: float = 30.0,
) -> tuple[str, int | None, dict[str, Any]]:
    init_payload = stripe_init_payment_page(
        checkout_session_id,
        publishable_key,
        proxy=proxy,
        timeout=timeout,
    )
    hosted = str(init_payload.get("stripe_hosted_url") or init_payload.get("url") or "").strip()
    hosted = to_openai_pay_url(hosted)
    if not is_browser_openable_hosted_url(hosted):
        raise PaylinkError(
            "stripe init did not return a browser-openable hosted URL "
            f"(got {hosted[:120]!r})"
        )
    amount = extract_amount_due_cents(init_payload)
    return hosted, amount, init_payload


def create_checkout_from_access_token(
    access_token: str,
    *,
    proxy: str = "",
    session_token: str = "",
    cookies: str = "",
    timeout: float = 60.0,
    payload: dict[str, Any] | None = None,
    language: str = DEFAULT_LANGUAGE,
    retries: int | None = None,
    resolve_hosted: bool | None = None,
) -> CheckoutResult:
    proxy_url = normalize_proxy_url(proxy)
    body = json.dumps(payload or build_checkout_payload(), separators=(",", ":"))
    attempt_limit = max(1, int(retries if retries is not None else os.environ.get("PP_CHECKOUT_RETRIES", "3") or "3"))
    last_error: Exception | None = None
    raw: dict[str, Any] | None = None
    for attempt in range(1, attempt_limit + 1):
        request = urllib.request.Request(
            CHECKOUT_ENDPOINT,
            data=body.encode("utf-8"),
            method="POST",
            headers=build_headers(access_token, session_token=session_token, cookies=cookies, language=language),
        )
        try:
            with build_opener(proxy_url).open(request, timeout=timeout) as response:
                response_body = response.read().decode("utf-8", "replace")
            raw = json.loads(response_body)
            if not isinstance(raw, dict):
                raise PaylinkError("checkout returned unexpected JSON shape")
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            if int(getattr(exc, "code", 0) or 0) in (400, 401, 403, 404, 409, 422):
                raise PaylinkError(f"checkout HTTP {exc.code}: {detail}") from exc
            last_error = PaylinkError(f"checkout HTTP {exc.code}: {detail}")
        except urllib.error.URLError as exc:
            last_error = PaylinkError(f"checkout network error: {exc.reason}")
        except json.JSONDecodeError as exc:
            last_error = PaylinkError(f"checkout returned non-JSON: {exc}")
        except PaylinkError as exc:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            last_error = PaylinkError(f"checkout unexpected error: {exc}")
        if attempt < attempt_limit:
            time.sleep(min(1.2 * attempt, 4.0))
    if raw is None:
        raise PaylinkError(str(last_error) if last_error else "checkout failed")

    do_resolve = resolve_hosted
    if do_resolve is None:
        do_resolve = str(os.environ.get("PP_RESOLVE_HOSTED_URL", "1") or "1").strip().lower() in ("1", "true", "yes", "on")
    return finalize_checkout_result(raw, proxy=proxy_url, resolve_hosted=do_resolve, timeout=timeout)


def create_checkout_from_session_json(
    session_json: str,
    *,
    proxy: str = "",
    timeout: float = 60.0,
    payload: dict[str, Any] | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> CheckoutResult:
    return create_checkout_from_access_token(
        extract_access_token(session_json),
        proxy=proxy,
        session_token=extract_session_token(session_json),
        cookies=extract_cookies(session_json),
        timeout=timeout,
        payload=payload,
        language=language,
    )


def finalize_checkout_result(
    raw: dict[str, Any],
    *,
    proxy: str = "",
    resolve_hosted: bool = True,
    timeout: float = 30.0,
) -> CheckoutResult:
    if str(raw.get("error") or "").strip():
        raise PaylinkError(f"checkout error: {raw.get('error')}")
    if raw.get("detail") and not raw.get("checkout_session_id") and not raw.get("url"):
        raise PaylinkError(f"checkout error: {raw.get('detail')}")

    client_secret = str(raw.get("client_secret") or raw.get("clientSecret") or "").strip()
    explicit_url = str(
        raw.get("url")
        or raw.get("redirect_url")
        or raw.get("checkout_url")
        or raw.get("hosted_checkout_url")
        or raw.get("stripe_hosted_url")
        or ""
    ).strip()
    checkout_session_id = extract_checkout_session_id(raw, explicit_url)
    if not checkout_session_id and client_secret.startswith("cs_"):
        checkout_session_id = client_secret.split("_secret_", 1)[0]
    if not checkout_session_id.startswith("cs_"):
        raise PaylinkError(
            "checkout response missing checkout_session_id; "
            f"keys={sorted(raw.keys())[:20]}"
        )

    publishable_key = str(raw.get("publishable_key") or raw.get("stripe_publishable_key") or DEFAULT_STRIPE_PK).strip()
    promo_campaign_id = extract_promo_campaign_id(raw)
    trial_eligible = bool(raw.get("one_click_trial_eligible"))
    billing = raw.get("billing_details") if isinstance(raw.get("billing_details"), dict) else {}
    country = str(billing.get("country") or raw.get("country") or "")
    currency = str(billing.get("currency") or raw.get("currency") or "")

    pay_url = ""
    amount_due_cents: int | None = None
    stripe_hosted_url = ""
    if resolve_hosted:
        pay_url, amount_due_cents, _init = resolve_real_hosted_checkout(
            checkout_session_id,
            publishable_key,
            proxy=proxy,
            timeout=timeout,
        )
        stripe_hosted_url = pay_url
    else:
        pay_url = build_hosted_pay_url(checkout_session_id, client_secret=client_secret, explicit_url=explicit_url)

    if not pay_url:
        raise PaylinkError("failed to produce hosted checkout URL")

    hosted_exact = is_browser_openable_hosted_url(pay_url)
    zero_promo = amount_due_cents == 0
    if amount_due_cents is None:
        # Without Stripe amount we cannot claim a 0-promo browser link.
        zero_promo = False

    if require_zero_enabled():
        if amount_due_cents is None:
            raise PaylinkError(
                "无法确认 0 元优惠金额（Stripe init 未返回 amount_due）。"
                f" trial_eligible={trial_eligible}, promo_campaign_id={promo_campaign_id or 'missing'}"
            )
        if amount_due_cents != 0:
            dollars = amount_due_cents / 100.0
            raise PaylinkError(
                f"当前 checkout 不是 0 元优惠链接：Stripe amount_due={amount_due_cents} "
                f"(${dollars:.2f}). trial_eligible={trial_eligible}, "
                f"promo_campaign_id={promo_campaign_id or 'missing'}。"
                "该 AT 可能已用过免费试用/无资格，请换未开通过 Plus 的新号。"
            )
        if not hosted_exact:
            raise PaylinkError("未拿到可浏览器打开的完整 hosted 长链（缺少有效 #fragment）")

    return CheckoutResult(
        pay_url=pay_url,
        checkout_session_id=checkout_session_id,
        processor_entity=str(raw.get("processor_entity") or ""),
        checkout_ui_mode=str(raw.get("checkout_ui_mode") or ""),
        tag=str(raw.get("tag") or ""),
        client_secret=client_secret,
        promo_campaign_id=promo_campaign_id,
        country=country,
        currency=currency,
        amount_due=str(amount_due_cents if amount_due_cents is not None else ""),
        zero_promo=zero_promo,
        hosted_exact=hosted_exact,
        publishable_key=publishable_key,
        trial_eligible=trial_eligible,
        stripe_hosted_url=stripe_hosted_url,
    )


def parse_checkout_response(response_body: str) -> CheckoutResult:
    """Parse checkout JSON and resolve a real hosted long link when possible."""
    try:
        raw = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise PaylinkError(f"checkout returned non-JSON: {response_body[:500]}") from exc
    if not isinstance(raw, dict):
        raise PaylinkError("checkout returned unexpected JSON shape")
    return finalize_checkout_result(raw, resolve_hosted=True)


def emit_result(result: CheckoutResult, fmt: str) -> None:
    if fmt == "plain":
        print(result.pay_url)
    elif fmt == "env":
        print(f"PAY_URL={result.pay_url}")
        print(f"CHECKOUT_SESSION_ID={result.checkout_session_id}")
    elif fmt == "json":
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        raise PaylinkError(f"unsupported output format: {fmt}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create pay.openai.com Plus checkout URL from accessToken/session JSON.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--access-token", help="Raw ChatGPT accessToken JWT.")
    source.add_argument("--access-token-file", help="File containing raw JWT, JSON, or accessToken snippet.")
    source.add_argument("--session-json", "-i", help="/api/auth/session JSON path. Use '-' or omit for stdin.")
    parser.add_argument("--proxy", default="", help="Proxy URL or host:port:user:pass.")
    parser.add_argument("--session-token", default="", help="Optional __Secure-next-auth.session-token value.")
    parser.add_argument("--cookies", default="", help="Optional Cookie header. Overrides --session-token cookie.")
    parser.add_argument("--format", choices=("plain", "env", "json"), default="plain")
    parser.add_argument("--country", default=DEFAULT_COUNTRY, help="Checkout billing country, e.g. US, GB/UK, JP.")
    parser.add_argument("--currency", default="", help="Checkout currency; empty derives the country currency.")
    parser.add_argument("--language", default="", help="oai-language header; empty derives the country locale.")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--retries", type=int, default=1)
    return parser.parse_args(argv)


def load_source_text(args: argparse.Namespace) -> str:
    if args.access_token:
        return args.access_token
    if args.access_token_file:
        return read_text(args.access_token_file)
    return read_text(args.session_json)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    last_error: Exception | None = None
    for attempt in range(1, max(1, args.retries) + 1):
        try:
            source_text = load_source_text(args)
            country = normalize_checkout_country(args.country)
            currency = str(args.currency or "").strip().upper() or currency_for_country(country)
            result = create_checkout_from_access_token(
                extract_access_token(source_text),
                proxy=args.proxy,
                session_token=args.session_token or extract_session_token(source_text),
                cookies=args.cookies or extract_cookies(source_text),
                timeout=args.timeout,
                payload=build_checkout_payload(country=country, currency=currency),
                language=args.language or language_for_country(country),
            )
            emit_result(result, args.format)
            return 0
        except PaylinkError as exc:
            last_error = exc
            if attempt >= max(1, args.retries):
                break
            print(f"attempt {attempt} failed: {exc}", file=sys.stderr)
            time.sleep(min(1.5 * attempt, 6.0))
    print(f"error: {last_error}", file=sys.stderr)
    return 1


__all__ = [
    "CHECKOUT_ENDPOINT",
    "CheckoutResult",
    "PaylinkError",
    "build_checkout_payload",
    "build_headers",
    "build_hosted_pay_url",
    "currency_for_country",
    "create_checkout_from_access_token",
    "create_checkout_from_session_json",
    "extract_access_token",
    "extract_amount_due_cents",
    "extract_checkout_session_id",
    "language_for_country",
    "normalize_checkout_country",
    "extract_client_secret_fragment",
    "extract_promo_campaign_id",
    "finalize_checkout_result",
    "is_browser_openable_hosted_url",
    "parse_checkout_response",
    "resolve_real_hosted_checkout",
    "stripe_init_payment_page",
    "to_openai_pay_url",
]


if __name__ == "__main__":
    raise SystemExit(main())
