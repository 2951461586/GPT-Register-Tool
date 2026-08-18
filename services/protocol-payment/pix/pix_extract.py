from __future__ import annotations

import os
import random
import re
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

import pix_core as core

LogCb = Callable[[str], None] | None

PIX_BOOTSTRAP_COUNTRY = "BR"
PIX_PROMOTION_COUNTRY = str(os.environ.get("PIX_PROMOTION_COUNTRY", "VN") or "VN").strip().upper() or "VN"
PIX_PROVIDER_COUNTRY = "BR"
PIX_MAX_AMOUNT_CENTS = max(0, int(os.environ.get("PIX_MAX_AMOUNT_CENTS", str(core.OPLL_FREE_TRIAL_MAX_MINOR_UNITS)) or core.OPLL_FREE_TRIAL_MAX_MINOR_UNITS))
PIX_REQUIRE_ZERO = str(os.environ.get("PIX_REQUIRE_ZERO", "0")).strip().lower() in {"1", "true", "on", "yes"}
PIX_REBUILD_ATTEMPTS = max(1, int(os.environ.get("PIX_REBUILD_ATTEMPTS", "5") or "5"))
PIX_POLL_TIMEOUT_SECONDS = max(10, int(os.environ.get("PIX_POLL_TIMEOUT_SECONDS", "45") or "45"))
PIX_FOLLOW_REDIRECT = str(os.environ.get("PIX_FOLLOW_REDIRECT", "1")).strip().lower() not in {"0", "false", "off", "no"}


def _log(log_cb: LogCb, message: str) -> None:
    text = str(message or "")
    print(text)
    if log_cb:
        try:
            log_cb(text)
        except Exception:
            pass


def proxy_for_region(proxy: str, region: str) -> str:
    proxy = str(proxy or "").strip()
    region = str(region or "").strip().upper()
    if proxy and region and re.search(r"(?i)region-[A-Za-z]{2}", proxy):
        return re.sub(r"(?i)region-[A-Za-z]{2}", f"region-{region}", proxy)
    return proxy


def enforce_pix_amount(amount: Any, stage: str) -> int:
    amount_int = core.opll_amount_to_int(amount)
    if amount_int is None:
        raise RuntimeError(f"pix amount policy failed {stage}: amount={amount or 'missing'}")
    if PIX_REQUIRE_ZERO and amount_int != 0:
        raise RuntimeError(f"pix amount policy failed {stage}: amount={amount_int}, require_zero=1")
    if amount_int > PIX_MAX_AMOUNT_CENTS:
        raise RuntimeError(
            f"pix amount policy failed {stage}: amount={amount_int}, allowed<={PIX_MAX_AMOUNT_CENTS}"
        )
    return amount_int


def is_static_stripe_asset(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    try:
        parsed = urlsplit(text)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if host in {"js.stripe.com", "m.stripe.network", "q.stripe.com", "files.stripe.com", "stripe-camo.global.ssl.fastly.net"}:
        return True
    if path.endswith((".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".map", ".woff", ".woff2")):
        return True
    if "fingerprinted/img" in path or "/icon-pm-" in path:
        return True
    return False


def is_pix_payment_url(value: str) -> bool:
    text = str(value or "").strip()
    if not text.startswith("https://") or is_static_stripe_asset(text):
        return False
    try:
        parsed = urlsplit(text)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if host in {"payments.stripe.com", "hooks.stripe.com", "qr.stripe.com"}:
        return True
    if host.endswith(".stripe.com") and any(x in path for x in ("/pix/", "/redirect/", "authenticate", "instructions")):
        return True
    return False


def is_pix_instructions_url(value: str) -> bool:
    if is_static_stripe_asset(value):
        return False
    try:
        parsed = urlsplit(str(value or "").strip())
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if (parsed.scheme or "").lower() != "https":
        return False
    if host == "payments.stripe.com" and "/pix/" in path:
        return True
    if host in {"hooks.stripe.com", "qr.stripe.com"} and ("pix" in path or "redirect" in path):
        return True
    return False


def extract_pix_details(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    next_action = payload.get("next_action")
    if isinstance(next_action, dict):
        action_type = str(next_action.get("type") or "").strip()
        if action_type == "pix_display_qr_code":
            display = next_action.get("pix_display_qr_code") or {}
            if isinstance(display, dict):
                try:
                    expires_at = int(display.get("expires_at") or 0)
                except Exception:
                    expires_at = 0
                details = {
                    "pix_hosted_instructions_url": str(display.get("hosted_instructions_url") or "").strip(),
                    "pix_qr_code": str(display.get("data") or display.get("qr_code") or "").strip(),
                    "pix_qr_image_url_png": str(display.get("image_url_png") or "").strip(),
                    "pix_qr_image_url_svg": str(display.get("image_url_svg") or "").strip(),
                    "pix_expires_at": expires_at,
                    "pix_redirect_url": "",
                    "source": "pix_display_qr_code",
                }
                if any(details[k] for k in (
                    "pix_hosted_instructions_url",
                    "pix_qr_code",
                    "pix_qr_image_url_png",
                    "pix_qr_image_url_svg",
                )):
                    return details
        if action_type == "redirect_to_url":
            redirect = next_action.get("redirect_to_url") or {}
            if isinstance(redirect, dict):
                url = str(redirect.get("url") or "").strip()
                if url:
                    return {
                        "pix_hosted_instructions_url": "",
                        "pix_qr_code": "",
                        "pix_qr_image_url_png": "",
                        "pix_qr_image_url_svg": "",
                        "pix_expires_at": 0,
                        "pix_redirect_url": url,
                        "source": "redirect_to_url",
                    }
    for key in ("setup_intent", "payment_intent", "payment_method_object", "payment_page"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = extract_pix_details(nested)
            if found:
                return found
    redirect_url = core.opll_extract_redirect_to_url(payload) if hasattr(core, "opll_extract_redirect_to_url") else ""
    if not redirect_url and isinstance(payload, dict):
        for url in core.opll_collect_urls(payload):
            if is_pix_instructions_url(url) or is_pix_payment_url(url):
                redirect_url = url
                break
    if redirect_url and not is_static_stripe_asset(redirect_url):
        return {
            "pix_hosted_instructions_url": redirect_url if is_pix_instructions_url(redirect_url) else "",
            "pix_qr_code": "",
            "pix_qr_image_url_png": "",
            "pix_qr_image_url_svg": "",
            "pix_expires_at": 0,
            "pix_redirect_url": redirect_url,
            "source": "redirect_scan",
        }
    return {}


def pix_details_has_link(details: dict[str, Any] | None) -> bool:
    if not isinstance(details, dict):
        return False
    return bool(
        str(details.get("pix_hosted_instructions_url") or "").strip()
        or str(details.get("pix_qr_code") or "").strip()
        or str(details.get("pix_qr_image_url_png") or "").strip()
        or str(details.get("pix_qr_image_url_svg") or "").strip()
        or str(details.get("pix_redirect_url") or "").strip()
    )


def _find_payment_method_types(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        raw = payload.get("payment_method_types")
        if isinstance(raw, list):
            return [str(item).lower() for item in raw if item]
        for value in payload.values():
            found = _find_payment_method_types(value)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_payment_method_types(item)
            if found:
                return found
    return []


def ensure_pix_offered(init_payload: dict[str, Any], stage: str) -> list[str]:
    methods = _find_payment_method_types(init_payload)
    if methods and "pix" not in methods:
        raise RuntimeError(f"pix line unavailable at {stage}: methods={methods}")
    return methods


def stripe_init_pix(stripe: requests.Session, cs_id: str, stripe_pk: str) -> dict[str, Any]:
    """PIX 专用 init：固定 pt-BR + America/Sao_Paulo。"""
    body = {
        "browser_locale": "pt-BR",
        "browser_timezone": "America/Sao_Paulo",
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
        "elements_session_client[locale]": "pt-BR",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": stripe_pk or core.DEFAULT_STRIPE_PK,
        "_stripe_version": core.STRIPE_VERSION_FULL,
    }
    response = stripe.post(
        f"https://api.stripe.com/v1/payment_pages/{cs_id}/init",
        data=body,
        timeout=core.PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"stripe init failed: HTTP {response.status_code} {response.text[:500]}")
    return response.json() or {}


def create_pix_checkout(access_token: str, proxy_url: str = "", with_promo: bool = False) -> dict[str, Any]:
    """PIX 专用 checkout：默认不带初始 promo（与 opll2 一致）。"""
    country = PIX_BOOTSTRAP_COUNTRY
    currency = "BRL"
    json_body: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "checkout_ui_mode": "custom",
    }
    if with_promo:
        json_body["promo_campaign"] = {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        }
    headers = {
        "Referer": "https://chatgpt.com/",
        "x-openai-target-path": "/backend-api/payments/checkout",
        "x-openai-target-route": "/backend-api/payments/checkout",
    }
    response = None
    for attempt in range(core.OPLL_CHECKOUT_TRANSIENT_RETRY_MAX):
        session = core.opll_build_chatgpt_session(access_token, proxy_url)
        response = session.post(
            "https://chatgpt.com/backend-api/payments/checkout",
            json=json_body,
            headers=headers,
            timeout=core.PAY_LONG_LINK_TIMEOUT,
        )
        if response.status_code < 400:
            break
        if response.status_code in core.OPLL_CHECKOUT_TRANSIENT_STATUSES and attempt < core.OPLL_CHECKOUT_TRANSIENT_RETRY_MAX - 1:
            time.sleep(core.OPLL_CHECKOUT_TRANSIENT_RETRY_DELAY + random.random())
            continue
        raise RuntimeError(f"checkout create failed: HTTP {response.status_code} {response.text[:500]}")
    data = (response.json() if response is not None else {}) or {}
    cs_id = data.get("checkout_session_id") or data.get("session_id") or data.get("id")
    if not cs_id or not str(cs_id).startswith("cs_"):
        raise RuntimeError(f"checkout response missing cs_id: {str(data)[:500]}")
    processor = core.opll_extract_processor_entity(data) or core.opll_processor_entity_for_country(country)
    return {
        "cs_id": str(cs_id),
        "processor_entity": processor,
        "stripe_publishable_key": core.opll_extract_stripe_publishable_key(data),
        "billing_country": country,
        "currency": currency,
    }


def generate_valid_cpf() -> str:
    """生成合法巴西 CPF（含校验位），供 PIX billing_details.tax_id 使用。"""
    nums = [random.randint(0, 9) for _ in range(9)]
    # 避免全相同数字
    if len(set(nums)) == 1:
        nums[0] = (nums[0] + 1) % 10
    s1 = sum(n * w for n, w in zip(nums, range(10, 1, -1)))
    d1 = 11 - (s1 % 11)
    d1 = 0 if d1 >= 10 else d1
    nums.append(d1)
    s2 = sum(n * w for n, w in zip(nums, range(11, 1, -1)))
    d2 = 11 - (s2 % 11)
    d2 = 0 if d2 >= 10 else d2
    nums.append(d2)
    return "".join(str(n) for n in nums)


def stripe_create_pix_method(
    stripe: requests.Session,
    cs_id: str,
    ctx: dict[str, Any],
    billing: dict[str, str],
    stripe_pk: str,
) -> str:
    runtime_version = str(ctx.get("runtime_version") or core.DEFAULT_STRIPE_RUNTIME_VERSION)
    tax_id = str(billing.get("tax_id") or "").strip() or generate_valid_cpf()
    body = {
        "billing_details[name]": billing.get("name") or "Joao Silva",
        "billing_details[email]": billing.get("email") or "buyer@example.com",
        "billing_details[phone]": billing.get("phone") or "",
        "billing_details[address][country]": billing.get("country") or "BR",
        "billing_details[address][line1]": billing.get("line1") or "Avenida Paulista 1000",
        "billing_details[address][city]": billing.get("city") or "Sao Paulo",
        "billing_details[address][postal_code]": billing.get("postal_code") or "01310-100",
        "billing_details[address][state]": billing.get("state") or "SP",
        "billing_details[tax_id]": tax_id,
        "type": "pix",
        "payment_user_agent": f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(25000, 55000)),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": stripe_pk or core.DEFAULT_STRIPE_PK,
        "_stripe_version": core.STRIPE_VERSION_FULL,
    }
    response = stripe.post("https://api.stripe.com/v1/payment_methods", data=body, timeout=core.PAY_LONG_LINK_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(core.opll_stripe_error_summary("stripe payment_methods(pix) failed", response))
    pm_id = str((response.json() or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        raise RuntimeError(f"stripe payment_methods(pix) bad response: {response.text[:300]}")
    return pm_id


def update_pix_checkout_taxes(
    access_token: str,
    checkout: dict[str, Any],
    billing: dict[str, str],
    proxy_url: str,
) -> None:
    cs_id = str(checkout.get("cs_id") or "").strip()
    processor = str(checkout.get("processor_entity") or "").strip() or core.opll_processor_entity_for_country("BR")
    body = {
        "checkout_session_id": cs_id,
        "checkout_email": billing.get("email") or "buyer@example.com",
        "billing_country": PIX_PROVIDER_COUNTRY,
        "billing_name": billing.get("name") or "Joao Silva",
        "currency": "BRL",
        "tax_id": None,
        "processor_entity": processor,
        "billing_address": {
            "line1": billing.get("line1") or "Avenida Paulista 1000",
            "city": billing.get("city") or "Sao Paulo",
            "country": PIX_PROVIDER_COUNTRY,
            "postal_code": billing.get("postal_code") or "01310-100",
        },
    }
    if billing.get("state"):
        body["billing_address"]["state"] = billing["state"]
    headers = {
        "Referer": f"https://chatgpt.com/checkout/{processor}/{cs_id}",
        "x-openai-target-path": "/backend-api/payments/checkout/taxes",
        "x-openai-target-route": "/backend-api/payments/checkout/taxes",
    }
    last_error = ""
    for attempt in range(core.OPLL_CHECKOUT_TRANSIENT_RETRY_MAX):
        session = core.opll_build_chatgpt_session(access_token, proxy_url)
        response = session.post(
            "https://chatgpt.com/backend-api/payments/checkout/taxes",
            json=body,
            headers=headers,
            timeout=core.PAY_LONG_LINK_TIMEOUT,
        )
        if response.status_code < 400:
            return
        last_error = f"HTTP {response.status_code} {response.text[:300]}"
        if response.status_code in core.OPLL_CHECKOUT_TRANSIENT_STATUSES and attempt < core.OPLL_CHECKOUT_TRANSIENT_RETRY_MAX - 1:
            time.sleep(core.OPLL_CHECKOUT_TRANSIENT_RETRY_DELAY + random.random())
            continue
        break
    raise RuntimeError(f"checkout/taxes failed: {last_error}")


def stripe_update_tax_region(
    stripe: requests.Session,
    cs_id: str,
    stripe_pk: str,
    billing: dict[str, str],
) -> None:
    body = {
        "eid": "NA",
        "tax_region[country]": PIX_PROVIDER_COUNTRY,
        "tax_region[postal_code]": billing.get("postal_code") or "01310-100",
        "tax_region[line1]": billing.get("line1") or "Avenida Paulista 1000",
        "tax_region[city]": billing.get("city") or "Sao Paulo",
        "key": stripe_pk or core.DEFAULT_STRIPE_PK,
    }
    if billing.get("state"):
        body["tax_region[state]"] = billing["state"]
    response = stripe.post(f"https://api.stripe.com/v1/payment_pages/{cs_id}", data=body, timeout=core.PAY_LONG_LINK_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f"stripe tax_region failed: HTTP {response.status_code} {response.text[:300]}")


def follow_redirect(stripe: requests.Session, start_url: str, max_hops: int = 5) -> str:
    current = str(start_url or "").strip()
    preferred = ("payments.stripe.com", "hooks.stripe.com", "qr.stripe.com", "stripe.com")
    for _ in range(max(1, max_hops)):
        if not current:
            return ""
        host = (urlsplit(current).netloc or "").lower()
        if any(host == h or host.endswith(f".{h}") for h in preferred) and is_pix_payment_url(current):
            if is_pix_instructions_url(current) or "pix" in current.lower():
                return current
        try:
            response = stripe.get(current, allow_redirects=False, timeout=core.PAY_LONG_LINK_TIMEOUT)
        except Exception:
            return current
        if response.status_code not in (301, 302, 303, 307, 308):
            return current
        location = str(response.headers.get("Location") or "").strip()
        if not location:
            return current
        from urllib.parse import urljoin

        current = urljoin(current, location)
    return current


def poll_pix_payment_page(
    stripe: requests.Session,
    cs_id: str,
    stripe_pk: str,
    ctx: dict[str, Any],
    timeout_seconds: int = 45,
) -> dict[str, Any]:
    deadline = time.time() + max(1, int(timeout_seconds or 45))
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": str(ctx.get("elements_session_id") or f"elements_session_{uuid.uuid4().hex[:11]}"),
        "elements_session_client[stripe_js_id]": str(ctx.get("stripe_js_id") or uuid.uuid4()),
        "elements_session_client[locale]": str(ctx.get("locale") or "pt-BR"),
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": stripe_pk,
        "_stripe_version": core.STRIPE_VERSION_FULL,
    }
    last_err = ""
    while time.time() < deadline:
        response = stripe.get(
            f"https://api.stripe.com/v1/payment_pages/{cs_id}",
            params=params,
            timeout=core.PAY_LONG_LINK_TIMEOUT,
        )
        if response.status_code == 200:
            payload = response.json() or {}
            details = extract_pix_details(payload)
            if pix_details_has_link(details):
                return details
            submission = core.opll_find_submission_attempt(payload)
            if submission.get("state") == "requires_approval":
                raise core.OpllStripeRequiresApproval("payment page requires ChatGPT approval")
            if submission.get("state") == "failed":
                raise RuntimeError(f"stripe submission failed: {core.opll_stripe_payload_diagnostics(payload, ctx)}")
            last_err = core.opll_stripe_payload_diagnostics(payload, ctx)
        else:
            last_err = f"HTTP {response.status_code} {response.text[:120]}"
        time.sleep(1)
    raise RuntimeError(f"pix payment link resolution timeout: {last_err}")


def enrich_with_redirect(stripe: requests.Session, details: dict[str, Any], log_cb: LogCb = None) -> dict[str, Any]:
    out = dict(details or {})
    hosted = str(out.get("pix_hosted_instructions_url") or "").strip()
    redirect = str(out.get("pix_redirect_url") or "").strip()
    if hosted:
        return out
    if not redirect:
        return out
    final_url = redirect
    if PIX_FOLLOW_REDIRECT:
        final_url = follow_redirect(stripe, redirect) or redirect
        if final_url != redirect:
            _log(log_cb, f"[PIX] follow redirect → {final_url[:180]}")
            out["pix_redirect_url"] = final_url
    if final_url and not str(out.get("pix_hosted_instructions_url") or "").strip():
        out["pix_hosted_instructions_url"] = final_url
    return out


def resolve_pix_after_confirm(
    access_token: str,
    stripe: requests.Session,
    confirm_payload: dict[str, Any],
    checkout: dict[str, Any],
    stripe_pk: str,
    ctx: dict[str, Any],
    provider_proxy: str,
    log_cb: LogCb = None,
) -> dict[str, Any]:
    cs_id = checkout["cs_id"]
    details = extract_pix_details(confirm_payload)
    if pix_details_has_link(details):
        return enrich_with_redirect(stripe, details, log_cb)
    submission = core.opll_find_submission_attempt(confirm_payload)
    state = str(submission.get("state") or "").strip()
    if state == "failed":
        raise RuntimeError(f"PIX confirm submission failed: {core.opll_stripe_payload_diagnostics(confirm_payload, ctx)}")
    if state == "requires_approval":
        _log(log_cb, "[PIX] requires_approval → ChatGPT approve")
        core.opll_chatgpt_approve_with_retry(access_token, cs_id, checkout, provider_proxy)
    try:
        polled = poll_pix_payment_page(stripe, cs_id, stripe_pk, ctx, timeout_seconds=PIX_POLL_TIMEOUT_SECONDS)
        return enrich_with_redirect(stripe, polled, log_cb)
    except core.OpllStripeRequiresApproval:
        _log(log_cb, "[PIX] poll still requires_approval → approve again")
        core.opll_chatgpt_approve_with_retry(access_token, cs_id, checkout, provider_proxy)
        polled = poll_pix_payment_page(stripe, cs_id, stripe_pk, ctx, timeout_seconds=PIX_POLL_TIMEOUT_SECONDS)
        return enrich_with_redirect(stripe, polled, log_cb)


def run_pix_provider_attempt(
    access_token: str,
    provider_proxy: str,
    promotion_proxy: str = "",
    log_cb: LogCb = None,
) -> dict[str, Any]:
    promotion_proxy = promotion_proxy or proxy_for_region(provider_proxy, PIX_PROMOTION_COUNTRY)
    provider_proxy = proxy_for_region(provider_proxy, PIX_PROVIDER_COUNTRY) if provider_proxy else provider_proxy
    _log(
        log_cb,
        f"[PIX] proxy chain: BR provider={provider_proxy or 'direct'}; "
        f"{PIX_PROMOTION_COUNTRY} promotion={promotion_proxy or 'direct'}",
    )

    billing = core.opll_billing_for_country(PIX_PROVIDER_COUNTRY)
    billing["tax_id"] = generate_valid_cpf()
    if not billing.get("state"):
        billing["state"] = "SP"
    _log(log_cb, f"[PIX] BR checkout create (no initial promo)… cpf={billing['tax_id'][:3]}***")
    # 对齐 opll2：PIX 首次 checkout 不带 promo_campaign，优惠改由后续 VN update 注入
    checkout = create_pix_checkout(access_token, provider_proxy, with_promo=False)
    if not checkout.get("processor_entity"):
        checkout["processor_entity"] = core.opll_processor_entity_for_country(PIX_BOOTSTRAP_COUNTRY)
    stripe_pk = core.opll_stripe_key_for_checkout(checkout)
    stripe = core.opll_build_stripe_session(provider_proxy)

    _log(log_cb, "[PIX] BR bootstrap Stripe init…")
    init_payload = stripe_init_pix(stripe, checkout["cs_id"], stripe_pk)
    bootstrap_amount, _ = core.opll_stripe_amount_info(init_payload)
    bootstrap_methods = _find_payment_method_types(init_payload)
    _log(
        log_cb,
        f"[PIX] bootstrap amount={bootstrap_amount} methods={bootstrap_methods or ['(empty)']} "
        f"(PIX 可能在税务同步后才出现)",
    )

    # promotion 优先 BR（checkout 同地域，连接稳定）；失败则回退 VN。
    # 之前 VN 优先，但实测 VN kookeey 节点存在间歇性 SSL_connect 中断，
    # 会造成 checkout/update 反复失败，故改为 BR 优先。
    promo_candidates = []
    for item in (provider_proxy, promotion_proxy):
        item = str(item or "").strip()
        if item and item not in promo_candidates:
            promo_candidates.append(item)
    if not promo_candidates:
        promo_candidates = [""]

    promo_used = promo_candidates[0]
    last_promo_error = ""
    for idx, candidate in enumerate(promo_candidates, start=1):
        label = "BR/provider" if idx == 1 else "VN/promo fallback"
        _log(log_cb, f"[PIX] checkout/update promotion via {label} proxy…")
        try:
            core.opll_update_checkout_promotion(access_token, checkout, candidate)
            promo_used = candidate
            last_promo_error = ""
            break
        except Exception as exc:
            last_promo_error = str(exc)
            _log(log_cb, f"[PIX] promotion failed on {label}: {core.opll_short_error(last_promo_error, 180)}")
            if idx >= len(promo_candidates):
                # 金额已是 0 时允许跳过 update，继续税区同步
                if core.opll_amount_to_int(bootstrap_amount) == 0:
                    _log(log_cb, "[PIX] bootstrap amount already 0, skip checkout/update and continue taxes")
                    promo_used = provider_proxy
                    break
                raise
    if last_promo_error and core.opll_amount_to_int(bootstrap_amount) != 0 and promo_used == promo_candidates[-1]:
        raise RuntimeError(f"checkout/update failed: {last_promo_error}")

    _log(log_cb, "[PIX] BR taxes + tax_region…")
    tax_proxy = provider_proxy or promo_used
    try:
        update_pix_checkout_taxes(access_token, checkout, billing, tax_proxy)
    except Exception as exc:
        if tax_proxy != promo_used and promo_used:
            _log(log_cb, f"[PIX] taxes on provider failed, retry promo proxy: {core.opll_short_error(str(exc), 120)}")
            update_pix_checkout_taxes(access_token, checkout, billing, promo_used)
            tax_proxy = promo_used
        else:
            raise
    tax_stripe = core.opll_build_stripe_session(tax_proxy)
    stripe_update_tax_region(tax_stripe, checkout["cs_id"], stripe_pk, billing)

    _log(log_cb, "[PIX] BR final Stripe init + amount check…")
    init_payload = stripe_init_pix(stripe, checkout["cs_id"], stripe_pk)
    stripe_amount, stripe_amount_source = core.opll_stripe_amount_info(init_payload)
    amount_int = enforce_pix_amount(stripe_amount, "after BR tax sync")
    methods = _find_payment_method_types(init_payload)
    _log(log_cb, f"[PIX] final amount={amount_int} source={stripe_amount_source} methods={methods or ['(empty)']}")
    if methods and "pix" not in methods:
        raise RuntimeError(f"pix line unavailable at final init: methods={methods}")

    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    ctx = core.opll_stripe_context(init_payload, payment_locale="pt-BR")
    ctx["checkout_amount"] = str(amount_int)
    if not ctx.get("currency"):
        ctx["currency"] = "brl"

    _log(log_cb, f"[PIX] create PM + confirm ({billing['name']} / {billing['city']})…")
    pm_id = stripe_create_pix_method(stripe, checkout["cs_id"], ctx, billing, stripe_pk)
    confirm_payload = core.opll_stripe_confirm(
        stripe,
        checkout["cs_id"],
        pm_id,
        stripe_pk,
        init_payload,
        ctx,
        checkout,
        stripe_hosted_url,
        pm_type="pix",
    )
    details = resolve_pix_after_confirm(
        access_token,
        stripe,
        confirm_payload if isinstance(confirm_payload, dict) else {},
        checkout,
        stripe_pk,
        ctx,
        provider_proxy,
        log_cb=log_cb,
    )
    if not pix_details_has_link(details):
        raise RuntimeError("pix payment link missing after confirm/approve")

    hosted = str(details.get("pix_hosted_instructions_url") or "").strip()
    redirect = str(details.get("pix_redirect_url") or "").strip()
    qr_code = str(details.get("pix_qr_code") or "").strip()
    image_png = str(details.get("pix_qr_image_url_png") or "").strip()
    image_svg = str(details.get("pix_qr_image_url_svg") or "").strip()
    # 拒绝静态资源误判
    if hosted and (is_static_stripe_asset(hosted) or not is_pix_link_url(hosted)):
        hosted = ""
    if redirect and (is_static_stripe_asset(redirect) or not is_pix_payment_url(redirect)):
        redirect = ""
    if image_png and is_static_stripe_asset(image_png) and "qr" not in image_png.lower():
        image_png = ""
    payment_url = hosted or redirect
    if not payment_url and not qr_code and not image_png and not image_svg:
        raise RuntimeError(f"pix payment link empty/invalid source={details.get('source')}")

    long_url = payment_url or (image_png if image_png and not is_static_stripe_asset(image_png) else "") or qr_code
    if not is_pix_link_url(long_url) and not (qr_code and qr_code.startswith("000201")):
        raise RuntimeError(f"pix payment link rejected as non-payment URL: {long_url[:160]}")
    _log(log_cb, f"[PIX] success source={details.get('source')} long_url={long_url[:180]}")
    return {
        **checkout,
        "payment_method_country": PIX_PROVIDER_COUNTRY,
        "payment_method_id": pm_id,
        "stripe_hosted_url": stripe_hosted_url,
        "stripe_redirect_url": redirect if redirect and redirect != payment_url else "",
        "provider_redirect_url": payment_url,
        "pix_hosted_instructions_url": hosted or payment_url,
        "pix_qr_code": qr_code,
        "pix_qr_image_url_png": image_png,
        "pix_qr_image_url_svg": image_svg,
        "pix_expires_at": int(details.get("pix_expires_at") or 0),
        "long_url": long_url,
        "stripe_amount": str(amount_int),
        "stripe_amount_source": stripe_amount_source,
        "confirm_amount": str(amount_int),
        "fallback": False,
    }


def generate_opll_pix_long_link(
    access_token: str,
    country: str = "BR",
    currency: str = "BRL",
    proxy_url: str = "",
    promotion_proxy_url: str = "",
    log_cb: LogCb = None,
    proxy_pair_provider=None,
) -> dict[str, Any]:
    """对外入口：与其它 generate_opll_*_long_link 同签名扩展。"""
    del country, currency  # PIX 固定 BR/BRL
    failures: list[str] = []
    for attempt in range(1, PIX_REBUILD_ATTEMPTS + 1):
        provider_proxy = proxy_url
        promotion_proxy = promotion_proxy_url or proxy_for_region(proxy_url, PIX_PROMOTION_COUNTRY)
        if proxy_pair_provider:
            try:
                pair = proxy_pair_provider(attempt)
                if pair:
                    provider_proxy = str((pair[0] if len(pair) >= 1 else "") or provider_proxy)
                    promotion_proxy = str((pair[1] if len(pair) >= 2 else "") or promotion_proxy or provider_proxy)
            except Exception as exc:
                _log(log_cb, f"[PIX] proxy_pair_provider error: {exc}")
        provider_proxy = proxy_for_region(provider_proxy, PIX_PROVIDER_COUNTRY) if provider_proxy else provider_proxy
        promotion_proxy = proxy_for_region(promotion_proxy, PIX_PROMOTION_COUNTRY) if promotion_proxy else promotion_proxy
        # 每轮刷新 sticky sid，降低 403 命中
        if provider_proxy:
            provider_proxy = core.randomize_proxy_sid(provider_proxy) or provider_proxy
        if promotion_proxy:
            promotion_proxy = core.randomize_proxy_sid(promotion_proxy) or promotion_proxy
        _log(log_cb, f"[PIX] attempt {attempt}/{PIX_REBUILD_ATTEMPTS}")
        try:
            return run_pix_provider_attempt(
                access_token,
                provider_proxy=provider_proxy,
                promotion_proxy=promotion_proxy,
                log_cb=log_cb,
            )
        except Exception as exc:
            msg = core.opll_short_error(str(exc), 260)
            failures.append(f"#{attempt}:{msg}")
            _log(log_cb, f"[PIX] attempt {attempt} failed: {msg}")
            lower = msg.lower()
            rebuildable = any(
                marker in lower
                for marker in (
                    "amount policy",
                    "pix line unavailable",
                    "payment link",
                    "resolution timeout",
                    "confirm",
                    "approve",
                    "checkout",
                    "tax",
                    "tls",
                    "ssl",
                    "timed out",
                    "timeout",
                    "failed to perform",
                    "curl",
                    "403",
                    "429",
                    "502",
                    "503",
                )
            )
            if not rebuildable or attempt >= PIX_REBUILD_ATTEMPTS:
                raise
            time.sleep(0.8 + random.random() * 0.5)
    raise RuntimeError("PIX 提取失败: " + "; ".join(failures[-5:]))


def is_pix_link_url(value: str) -> bool:
    text = str(value or "").strip()
    if not text or is_static_stripe_asset(text):
        return False
    if is_pix_instructions_url(text):
        return True
    if is_pix_payment_url(text):
        return True
    # EMV PIX payload (starts with 000201)
    if text.startswith("000201") and len(text) > 20:
        return True
    return False
