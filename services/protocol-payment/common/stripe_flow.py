"""Stripe confirm / redirect helpers shared by the protocol-payment extractors.

Stage 2 (the parameterized stripe_* pairs) of
`docs/audits/plans/plan-2026-09-17-protocol-payment-extractor-consolidation.md`.

What is shared, and why it is safe
----------------------------------
``processor_entity_for_country`` / ``stripe_checkout_long_url`` /
``to_openai_pay_url`` / ``stripe_confirm_return_url`` are **AST-identical** in
blik / ideal / twint once the provider token is normalised away (verified by
``runtime/tmp/_stripe_pair_diff.py``: 4 pairs, 3-way, zero divergence). They are
pure URL/entity builders — no module state — so a single authority is correct.

``resolve_confirm_payload`` is shared *parameterized*: the ideal and twint
bodies are identical except for three log strings and the QR-candidate note.
The provider name is injected, NOT branched on — there is no ``if provider ==
"ideal"`` here; the caller passes its label and the body is otherwise one path.
That keeps the consolidation out of the "config hell" the plan forbids (§6).

What is deliberately NOT shared
-------------------------------
``stripe_create_*_pm`` and ``add_inline_*_payment_method_data`` carry **real
provider semantics** (iDEAL has a ``bank`` selector, blik has its own inline
variant, the PM ``type`` differs) — the diff probe marks them DIFFERS. They stay
per-extractor. ``stripe_confirm_*`` is shared between ideal/twint but blik's
``stripe_confirm_ideal`` diverges from ideal's, so the confirm bodies stay
per-extractor for now.

Rule 10: pure stdlib, no ``sms_tool`` import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from . import endpoints
from .timeouts import DEFAULT_TIMEOUT

__all__ = [
    "processor_entity_for_country",
    "stripe_checkout_long_url",
    "to_openai_pay_url",
    "stripe_confirm_return_url",
    "resolve_confirm_payload",
    "StripePageHooks",
    "stripe_update_customer_data",
    "stripe_update_tax_region",
]


def processor_entity_for_country(
    country: str, processor_entity: str = "", *, normalize_country: Callable[[str], str]
) -> str:
    """``openai_llc`` for US exits, ``openai_ie`` elsewhere; explicit wins."""
    if processor_entity:
        return processor_entity
    return "openai_llc" if normalize_country(country) == "US" else "openai_ie"


def stripe_checkout_long_url(
    cs_id: str,
    country: str,
    processor_entity: str,
    *,
    normalize_country: Callable[[str], str],
) -> str:
    processor = processor_entity_for_country(country, processor_entity, normalize_country=normalize_country)
    success = endpoints.chatgpt_checkout_verify(cs_id, processor)
    return (
        f"{endpoints.STRIPE_CHECKOUT_BASE}/c/pay/{cs_id}"
        f"?returned_from_redirect=true&ui_mode=custom&return_url={quote(success, safe='')}"
    )


def to_openai_pay_url(stripe_hosted_url: str) -> str:
    url = str(stripe_hosted_url or "").strip()
    if not url:
        return ""
    if url.startswith(endpoints.STRIPE_CHECKOUT_BASE):
        return endpoints.OPENAI_PAY_BASE + url[len(endpoints.STRIPE_CHECKOUT_BASE) :]
    parsed = urlsplit(url)
    if parsed.netloc.lower() == "checkout.stripe.com":
        return urlunsplit((parsed.scheme or "https", "pay.openai.com", parsed.path, parsed.query, parsed.fragment))
    return url


def stripe_confirm_return_url(
    cs_id: str,
    checkout: dict[str, str],
    stripe_hosted_url: str,
    *,
    normalize_country: Callable[[str], str],
    default_country: str,
) -> str:
    country = normalize_country(checkout.get("billing_country") or default_country)
    processor = processor_entity_for_country(
        country, checkout.get("processor_entity") or "", normalize_country=normalize_country
    )
    success = endpoints.chatgpt_checkout_verify(cs_id, processor)
    hosted = to_openai_pay_url(stripe_hosted_url) or stripe_checkout_long_url(
        cs_id, country, processor, normalize_country=normalize_country
    )
    if "pay.openai.com/" in hosted or "checkout.stripe.com/" in hosted:
        parsed = urlsplit(hosted)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault("success_return_url", success)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    return hosted


def resolve_confirm_payload(
    stripe: Any,
    confirm_payload: dict[str, Any],
    checkout: dict[str, str],
    stripe_pk: str,
    ctx: dict[str, Any],
    pm_id: str,
    access_token: str,
    device_id: str,
    session_token: str,
    checkout_proxy: str,
    provider_proxy: str,
    approve_pool: list[str],
    *,
    provider_label: str,
    redirect_label: str,
    no_redirect_note: str,
    raise_if_setup_intent_blocked: Callable[..., None],
    extract_redirect_url: Callable[[Any], str],
    stripe_payload_intent_redirect_url: Callable[..., str],
    extract_qr_candidates: Callable[[Any], list[str]],
    find_submission_attempt: Callable[[Any], dict[str, Any]],
    approve_proxy_candidates: Callable[..., list[str]],
    approve_with_retry: Callable[..., str],
    poll_payment_page: Callable[..., tuple[str, list[str]]],
    log: Callable[..., None],
) -> tuple[str, list[str], str]:
    """Resolve the final redirect/QR after a Stripe confirm, approving if asked.

    The ideal and twint bodies were identical apart from three log strings; the
    provider name and those two log phrases are injected (``provider_label``,
    ``redirect_label``, ``no_redirect_note``), not branched on. All the
    side-effecting helpers stay the extractor's own and are passed in.
    """
    raise_if_setup_intent_blocked(confirm_payload, "stripe confirm", current_pm_id=pm_id)
    redirect_url = extract_redirect_url(confirm_payload)
    if not redirect_url:
        redirect_url = stripe_payload_intent_redirect_url(stripe, confirm_payload, stripe_pk, current_pm_id=pm_id)
    qr_urls = extract_qr_candidates(confirm_payload)
    submission = find_submission_attempt(confirm_payload)

    if redirect_url:
        log(f"confirm 提取到最终{redirect_label}: {redirect_url[:180]}")
    if qr_urls:
        log(f"confirm 提取到 QR 候选 {len(qr_urls)} 个")

    approve_proxy = ""
    if not redirect_url and submission.get("state") == "requires_approval":
        log("需要 ChatGPT approve...")
        approve_proxies = approve_proxy_candidates(checkout_proxy, provider_proxy, approve_pool)
        log(
            f"需要 approve：{provider_label} 0 元场景，优先使用历史成功/当前 Provider 代理，失败后切换下一个 Provider 代理。"
        )
        approve_proxy = approve_with_retry(
            access_token, device_id, checkout, approve_proxies, session_token, "provider"
        )
        log("跟随跳转提取最终链...")
        redirect_url, poll_qr = poll_payment_page(stripe, checkout, stripe_pk, ctx, current_pm_id=pm_id)
        qr_urls.extend(poll_qr)
    elif not redirect_url and not qr_urls:
        log(f"confirm 未返回真实 {provider_label} {no_redirect_note}，继续 poll payment_pages 做最终确认", "[WARN] ")
        redirect_url, poll_qr = poll_payment_page(stripe, checkout, stripe_pk, ctx, current_pm_id=pm_id)
        qr_urls.extend(poll_qr)

    return redirect_url, list(dict.fromkeys(qr_urls)), approve_proxy


@dataclass(frozen=True)
class StripePageHooks:
    """Per-extractor seams the two payment-page writers below need.

    Both writers POST to ``{STRIPE_PAYMENT_PAGES}/<cs_id>`` with a body built
    entirely from the caller's checkout context. The body is not provider
    specific *except* for the two saved-payment-method switches, so that value
    arrives as a callable: iDEAL and TWINT read different env vars
    (``IDEAL_SAVED_PAYMENT_VALUE`` / ``TWINT_SAVED_PAYMENT_VALUE``), and baking
    one in would silently change the other's request body.

    ``stripe_version`` is a parameter rather than a module constant for the same
    reason ``stripe_init`` takes it: it is part of the wire contract the
    extractor owns, and a shared literal would make a future version bump a
    cross-provider change.
    """

    dump_http: Callable[..., Any]
    log: Callable[..., None]
    saved_payment_value: Callable[[], str]
    checkout_not_active: Callable[[Any], bool]
    stripe_version: str


def stripe_update_customer_data(
    stripe: Any,
    cs_id: str,
    stripe_pk: str,
    ctx: dict[str, Any],
    billing: dict[str, str],
    *,
    hooks: StripePageHooks,
) -> bool:
    """Submit the customer_data half of a custom Checkout session.

    Moved verbatim out of ``ideal_qr_extract.py`` / ``twint_extract.py`` (stage 2
    of the consolidation plan). The two bodies were byte-identical; the
    differential test in ``tests/governance/test_stage2_replay_selfcheck.py``
    drives the frozen pre-move body and this one through the same replay
    transport and compares the served request fingerprints.
    """
    body: dict[str, Any] = {
        "key": stripe_pk,
        "_stripe_version": hooks.stripe_version,
        "expected_amount": str(ctx.get("checkout_amount") or 0),
        "elements_session_client[session_id]": ctx["elements_session_id"],
        "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[locale]": ctx["locale"],
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "customer_data[email]": billing["email"],
        "customer_data[name]": billing["name"],
        "customer_data[address][country]": billing["country"],
        "customer_data[address][line1]": billing["line1"],
        "customer_data[address][city]": billing["city"],
        "customer_data[address][postal_code]": billing["postal_code"],
    }
    if billing.get("line2"):
        body["customer_data[address][line2]"] = billing["line2"]
    if billing.get("state"):
        body["customer_data[address][state]"] = billing["state"]

    url = f"{endpoints.STRIPE_PAYMENT_PAGES}/{cs_id}"
    try:
        resp = stripe.post(url, data=body, timeout=DEFAULT_TIMEOUT)
        hooks.dump_http(resp, "customer_data_update_nl", body, "POST", url, force=resp.status_code >= 400)
        if resp.status_code < 400:
            hooks.log(f"荷兰 customer_data 已提交: {billing['name']} / {billing['city']} / {billing['postal_code']}")
            return True
        if hooks.checkout_not_active(resp.text):
            raise RuntimeError("checkout_not_active_session")
        hooks.log(f"荷兰 customer_data 提交失败 HTTP {resp.status_code}: {resp.text[:180]}", "[WARN] ")
    except Exception as exc:
        if hooks.checkout_not_active(exc):
            raise
        hooks.log(f"荷兰 customer_data 提交异常: {exc}", "[WARN] ")
    return False


def stripe_update_tax_region(
    stripe: Any,
    cs_id: str,
    stripe_pk: str,
    ctx: dict[str, Any],
    billing: dict[str, str],
    *,
    hooks: StripePageHooks,
) -> bool:
    """Submit the tax_region half of a custom Checkout session.

    Same provenance as :func:`stripe_update_customer_data`. The only
    provider-varying values in the body are the two ``saved_payment_method``
    switches, which is what ``hooks.saved_payment_value`` supplies.
    """
    body: dict[str, Any] = {
        "key": stripe_pk,
        "_stripe_version": hooks.stripe_version,
        "elements_session_client[session_id]": ctx["elements_session_id"],
        "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[locale]": ctx["locale"],
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_options_client[saved_payment_method][enable_save]": hooks.saved_payment_value(),
        "elements_options_client[saved_payment_method][enable_redisplay]": hooks.saved_payment_value(),
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "expressCheckout",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][2]": "address",
        "tax_region[country]": billing["country"],
        "tax_region[postal_code]": billing["postal_code"],
        "tax_region[line1]": billing["line1"],
        "tax_region[city]": billing["city"],
    }
    if billing.get("state"):
        body["tax_region[state]"] = billing["state"]

    url = f"{endpoints.STRIPE_PAYMENT_PAGES}/{cs_id}"
    try:
        resp = stripe.post(url, data=body, timeout=DEFAULT_TIMEOUT)
        hooks.dump_http(resp, "tax_region_update", body, "POST", url, force=resp.status_code >= 400)
        if resp.status_code < 400:
            hooks.log(f"tax_region 已提交: {billing['country']} / {billing['city']} {billing['postal_code']}")
            return True
        if hooks.checkout_not_active(resp.text):
            raise RuntimeError("checkout_not_active_session")
        hooks.log(f"tax_region 提交失败 HTTP {resp.status_code}: {resp.text[:180]}", "[WARN] ")
    except Exception as exc:
        if hooks.checkout_not_active(exc):
            raise
        hooks.log(f"tax_region 提交异常: {exc}", "[WARN] ")
    return False
