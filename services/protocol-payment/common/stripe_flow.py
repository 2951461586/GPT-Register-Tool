"""Stripe confirm / redirect helpers shared by the protocol-payment extractors.

Stage 2 (the parameterized stripe_* pairs) of
`docs/audits/plan-2026-09-17-protocol-payment-extractor-consolidation.md`.

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

from typing import Any, Callable
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from . import endpoints

__all__ = [
    "processor_entity_for_country",
    "stripe_checkout_long_url",
    "to_openai_pay_url",
    "stripe_confirm_return_url",
    "resolve_confirm_payload",
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
        return endpoints.OPENAI_PAY_BASE + url[len(endpoints.STRIPE_CHECKOUT_BASE):]
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
        log(f"需要 approve：{provider_label} 0 元场景，优先使用历史成功/当前 Provider 代理，失败后切换下一个 Provider 代理。")
        approve_proxy = approve_with_retry(access_token, device_id, checkout, approve_proxies, session_token, "provider")
        log("跟随跳转提取最终链...")
        redirect_url, poll_qr = poll_payment_page(stripe, checkout, stripe_pk, ctx, current_pm_id=pm_id)
        qr_urls.extend(poll_qr)
    elif not redirect_url and not qr_urls:
        log(f"confirm 未返回真实 {provider_label} {no_redirect_note}，继续 poll payment_pages 做最终确认", "[WARN] ")
        redirect_url, poll_qr = poll_payment_page(stripe, checkout, stripe_pk, ctx, current_pm_id=pm_id)
        qr_urls.extend(poll_qr)

    return redirect_url, list(dict.fromkeys(qr_urls)), approve_proxy
