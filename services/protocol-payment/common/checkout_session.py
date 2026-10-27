"""Checkout-session creation and approve warm-up shared by the QR extractors.

Stage 2 (batch 9) of
`docs/audits/plans/plan-2026-09-17-protocol-payment-extractor-consolidation.md`.

What is shared, and why it is safe
----------------------------------
``create_checkout`` was AST-identical in blik / ideal / twint once the provider
token is normalized away -- 109 / 109 / 102 lines, the 7-line gap being nothing
but blik's one-line ``dump_http`` call. ``warmup_approve_context`` was identical
in all three (22 lines), down to its unused ``checkout_page_url`` parameter.
Both move here as **one** body: nothing below branches on the provider.

What stays with the caller, and why it must
-------------------------------------------
Every seam below is provider *data* or a provider-owned side effect -- not
formatting -- so it arrives through :class:`CheckoutHooks` instead of an
``if provider == ...``:

* ``normalize_country`` / ``currency_for_country`` -- ideal and twint delegate to
  ``common/provider_profile.py`` with their own profile, but **blik does not**:
  it resolves against its own ``COUNTRY_CURRENCY`` table and falls back to
  ``default_payment_country()``. Importing the shared pair here would silently
  change blik's billing currency and every ``cs_id``'s price.
* ``default_stripe_pk`` -- a module constant each extractor evaluates from
  ``PP_STRIPE_PUBLISHABLE_KEY`` / ``STRIPE_PUBLISHABLE_KEY`` at import time.
  Baking one evaluation into ``common`` would move that read earlier and make a
  future per-extractor default impossible.
* ``coupon_fallback_flag`` / ``warmup_dump_flag`` -- the env-var *names*, not
  their values. ``ideal`` and ``blik`` share the ``IDEAL_`` prefix (blik runs the
  ideal runtime env), ``twint`` reads ``TWINT_*``; passing the name keeps each
  module's literal in its own file, where an operator reads it.
* ``log`` / ``dump_http`` / ``env_bool`` / ``env_int`` -- per-extractor sinks and
  readers: each module owns its log file, dump directory and redaction registry.

The hooks are built by the caller **per call**, not captured in a module-level
constant, because the pre-merge bodies resolved these names as globals on every
invocation. A constant would freeze the binding at import time and make a later
``monkeypatch.setattr(module, "log", ...)`` (or a swapped redaction registry)
silently ineffective -- a behaviour change no other gate would see.

Rule 10: pure stdlib + ``common`` only, never ``sms_tool``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from . import endpoints
from .extractor_helpers import is_user_already_paid_error
from .payment_predicates import checkout_response_has_promo, checkout_response_has_trial
from .timeouts import CHATGPT_TIMEOUT

__all__ = [
    "CheckoutHooks",
    "create_checkout",
    "warmup_approve_context",
]


@dataclass(frozen=True)
class CheckoutHooks:
    """Per-extractor seams for the two functions below.

    ``coupon_fallback_flag`` / ``warmup_dump_flag`` are env-var *names*: the
    probe that created this module exists precisely because a hardcoded prefix
    would make one extractor read another's switch.
    """

    normalize_country: Callable[[str], str]
    currency_for_country: Callable[[str], str]
    env_bool: Callable[..., bool]
    env_int: Callable[..., int]
    log: Callable[..., None]
    dump_http: Callable[..., Any]
    default_stripe_pk: str
    coupon_fallback_flag: str
    warmup_dump_flag: str


def create_checkout(chatgpt: Any, country: str, *, hooks: CheckoutHooks) -> dict[str, str]:
    """Create a custom Checkout session and return its billing fingerprint.

    Moved verbatim out of ``ideal_qr_extract.py`` / ``twint_extract.py`` /
    ``blik_qr_extract.py`` (stage 2, batch 9). The differential test in
    ``tests/governance/test_stage2_replay_selfcheck.py`` drives the frozen
    pre-move body and this one through the same replay transport and compares
    the served request fingerprints, the returned mapping and the raised error.
    """
    country = hooks.normalize_country(country)
    promo_mode = os.environ.get("PP_PROMO_MODE", "campaign").strip().lower() or "campaign"
    promo_id = os.environ.get("PP_PROMO_ID", "plus-1-month-free").strip()
    body: dict[str, Any] = {
        "entry_point": os.environ.get("PP_ENTRY_POINT", "all_plans_pricing_modal"),
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": hooks.currency_for_country(country)},
        "checkout_ui_mode": "custom",
    }
    if promo_mode in ("trial", "free_trial"):
        trial_days = hooks.env_int("PP_TRIAL_DAYS", 30)
        body["subscription_data"] = {"trial_period_days": trial_days}
    elif promo_mode in ("campaign", "query"):
        body["promo_campaign"] = {
            "promo_campaign_id": promo_id,
            "is_coupon_from_query_param": promo_mode == "query",
        }
    elif promo_mode == "coupon":
        body["coupon"] = promo_id
    elif promo_mode == "code":
        body["promotion_code"] = promo_id
    elif promo_mode != "off":
        hooks.log(f"未知 PP_PROMO_MODE={promo_mode!r}，已忽略", "[WARN] ")
    hooks.log(f"Checkout promo: mode={promo_mode}, id={promo_id}")

    headers = {
        "Referer": endpoints.CHATGPT_BASE + "/",
        "x-openai-target-path": "/backend-api/payments/checkout",
        "x-openai-target-route": "/backend-api/payments/checkout",
    }
    resp = chatgpt.post(
        endpoints.CHATGPT_CHECKOUT_API,
        json=body,
        headers=headers,
        timeout=CHATGPT_TIMEOUT,
    )
    hooks.dump_http(
        resp,
        "checkout",
        body,
        "POST",
        endpoints.CHATGPT_CHECKOUT_API,
        force=resp.status_code >= 400,
    )
    if resp.status_code >= 400:
        if is_user_already_paid_error(resp.text):
            raise RuntimeError("用户已支付: User is already paid")
        raise RuntimeError(f"checkout 创建失败 HTTP {resp.status_code}: {resp.text[:500]}")

    data = resp.json() or {}
    if (
        promo_mode == "coupon"
        and promo_id == "plus-1-month-free"
        and not checkout_response_has_promo(data)
        and hooks.env_bool(hooks.coupon_fallback_flag, True)
    ):
        hooks.log("coupon 响应未显示优惠，按 promo_campaign 字符串重试", "[PROMO] ")
        fallback_body = dict(body)
        fallback_body.pop("coupon", None)
        fallback_body["promo_campaign"] = promo_id
        resp = chatgpt.post(
            endpoints.CHATGPT_CHECKOUT_API,
            json=fallback_body,
            headers=headers,
            timeout=CHATGPT_TIMEOUT,
        )
        hooks.dump_http(
            resp,
            "checkout_promo_campaign",
            fallback_body,
            "POST",
            endpoints.CHATGPT_CHECKOUT_API,
            force=True,
        )
        if resp.status_code >= 400:
            if is_user_already_paid_error(resp.text):
                raise RuntimeError("用户已支付: User is already paid")
            raise RuntimeError(f"checkout promo_campaign 重试失败 HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json() or {}
        hooks.log(f"promo_campaign 重试后 promo={checkout_response_has_promo(data)}", "[PROMO] ")

    cs_id = data.get("checkout_session_id") or data.get("session_id") or data.get("id")
    if not cs_id or not str(cs_id).startswith("cs_"):
        raise RuntimeError(f"checkout 响应缺少 cs_id: {str(data)[:500]}")

    raw_pk = (
        data.get("stripe_publishable_key")
        or data.get("publishable_key")
        or data.get("publishableKey")
        or data.get("stripePublishableKey")
        or data.get("key")
        or ""
    )
    match = re.search(r"pk_live_[A-Za-z0-9]+", str(raw_pk))
    stripe_pk = match.group(0) if match else hooks.default_stripe_pk
    processor_entity = str(data.get("processor_entity") or data.get("processorEntity") or "")
    hooks.log(
        f"Checkout 创建成功: {cs_id} / {country} / {hooks.currency_for_country(country)} / "
        f"mode={promo_mode} / promo={checkout_response_has_promo(data)} / "
        f"trial={checkout_response_has_trial(data)}"
    )
    return {
        "cs_id": str(cs_id),
        "processor_entity": processor_entity,
        "stripe_pk": stripe_pk,
        "billing_country": country,
        "currency": hooks.currency_for_country(country),
    }


def warmup_approve_context(chatgpt: Any, checkout_page_url: str, *, hooks: CheckoutHooks) -> None:
    """Prime the approve context with one best-effort sentinel ping.

    ``checkout_page_url`` is part of the pre-merge signature and was **already
    unused there**; it stays so the three call sites and their wrappers keep the
    same shape (a signature change would be a silent behaviour change for any
    caller that passes it positionally).

    ``hooks.warmup_dump_flag`` is the dump switch's env-var *name*: ideal and blik
    read ``IDEAL_DUMP_WARMUP``, twint reads ``TWINT_DUMP_WARMUP``.
    """
    try:
        resp = chatgpt.post(
            endpoints.CHATGPT_SENTINEL_PING,
            json={},
            headers={
                "Referer": endpoints.CHATGPT_BASE + "/",
                "x-openai-target-path": "/backend-api/sentinel/ping",
                "x-openai-target-route": "/backend-api/sentinel/ping",
            },
            timeout=CHATGPT_TIMEOUT,
        )
        hooks.dump_http(
            resp,
            "sentinel_ping",
            {},
            "POST",
            endpoints.CHATGPT_SENTINEL_PING,
            force=hooks.env_bool(hooks.warmup_dump_flag, False),
        )
    except Exception as exc:
        hooks.log(f"approve sentinel 请求异常: {exc}", "[WARN] ")
