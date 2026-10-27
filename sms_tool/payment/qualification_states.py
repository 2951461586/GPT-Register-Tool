"""Machine-readable 支付资格 (payment qualification) vocabulary.

Same split as ``sms_tool/promotion_states.py``: the Chinese badge text is display
copy owned by one composer, and everything that filters, sorts, branches or
persists keys off the stable ASCII states defined here. A label reword must not
change behaviour — the desktop column filter reads ``payment_qualification_state``,
never the label.

Dependency-free by design: ``store/``, ``desktop_read`` and the cross-language
contract tests import it alongside the batch module.
"""

from __future__ import annotations

from typing import Any

QUALIFICATION_ELIGIBLE = "eligible"
QUALIFICATION_INELIGIBLE = "ineligible"
QUALIFICATION_AUTH_INVALID = "auth_invalid"
QUALIFICATION_EGRESS_DENIED = "egress_denied"
QUALIFICATION_PROBE_FAILED = "probe_failed"
QUALIFICATION_UNKNOWN = "unknown"

#: Display names for the payment methods this project can probe. Unknown ids
#: fall back to the upper-cased id rather than to an empty label, so a new
#: catalogue entry is never rendered as "可用·".
METHOD_LABELS: dict[str, str] = {
    "paypal": "PayPal",
    "gcash": "GCash",
    "momo": "MoMo",
    "upi": "UPI",
    "gopay": "GoPay",
    "grabpay": "GrabPay",
    "qris": "QRIS",
    "bizum": "Bizum",
    "naver_pay": "Naver Pay",
    "ideal": "iDEAL",
    "pix": "PIX",
    "kakao": "Kakao Pay",
    "blik": "BLIK",
    "twint": "TWINT",
    "direct_card": "卡支付",
}

#: Error markers that mean "the access token is dead", not "the method is
#: unavailable". Matched case-insensitively against error/error_code.
_AUTH_INVALID_MARKERS = (
    "401",
    "unauthorized",
    "token_invalid",
    "access_token_invalid",
    "missing_access_token",
    "checkout_unauthorized",
    "stripe_init_unauthorized",
    "invalid_grant",
)

#: Markers that mean "the request country and the billing country disagree".
#:
#: This is a *deterministic* answer, not a transport failure: OpenAI refuses the
#: Checkout because the exit is not in the method's country (measured 2026-09-26:
#: a flapping endpoint returning VN produced ``400 Billing country must match
#: request country`` for an IN method). Classifying it as ``ineligible`` is what
#: turns the old "检测失败·HTTP 400" column into "无可用方式·地区不符".
_COUNTRY_MISMATCH_MARKERS = (
    "billing country must match",
    "country must match request country",
)

#: Stable machine decision for "the exit country and the billed country disagree"
#: (the server's ``400 Billing country must match request country``). Exported so
#: the batch/aggregate layer can label a row without re-implementing the match.
COUNTRY_MISMATCH_DECISION = "checkout_country_mismatch"


def is_country_mismatch(*texts: Any) -> bool:
    """True when any of the given strings carries the server's country refusal."""
    combined = " ".join(_text(value) for value in texts)
    return any(marker in combined for marker in _COUNTRY_MISMATCH_MARKERS)


#: OpenAI's risk engine refusal (``400 Our systems have detected unusual
#: activity``). It is an exit/device signal rather than a method verdict, so the
#: state stays ``probe_failed`` (retry on another egress) while the badge says
#: "风控拦截" instead of the generic "检测失败".
_RISK_MARKERS = ("unusual activity",)


def is_risk_blocked(*texts: Any) -> bool:
    """True when any of the given strings carries OpenAI's risk refusal.

    Public because the **aggregate** badge needs the same judgement as the
    per-method one: measured 2026-10-27, an account whose only failure was
    ``checkout HTTP 400 unusual activity`` rendered as 检测失败 because
    ``_aggregate_label`` only looked at the state, not at the error.
    """
    combined = " ".join(_text(value) for value in texts)
    return any(marker in combined for marker in _RISK_MARKERS)


#: The proxy refused the *target host* (not the method, not the token). The pool
#: answered ``chatgpt.com`` but the tunnel denied ``api.stripe.com``:
#: measured 2026-10-27, every proxylane exit returned
#: ``Tunnel connection failed: 503 Target host denied``. Kept distinct from
#: ``probe_failed`` because the operator's action is "change the exit", not
#: "refresh the token" or "pick another method".
_EGRESS_DENIED_MARKERS = (
    "target host denied",
    "tunnel connection failed",
    "unable to connect to proxy",
    "forbidden ip",
    "proxy authentication",
)

#: Error markers that mean "we never got an answer", so nothing was learned
#: about the account. Distinguished from ``unknown`` (an answer arrived but was
#: inconclusive) because only this class is worth retrying on a new exit.
_TRANSPORT_MARKERS = (
    "transport",
    "timeout",
    "timed out",
    "curl: (",
    "connection",
    "proxy",
    "tls",
)


def method_label(method: str) -> str:
    key = str(method or "").strip().lower().replace("-", "_")
    return METHOD_LABELS.get(key, key.upper())


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def qualification_state(result: Any) -> str:
    """Map one capability-probe result onto the stable qualification state."""
    if not isinstance(result, dict) or not result:
        return QUALIFICATION_UNKNOWN
    error_text = f"{_text(result.get('error'))} {_text(result.get('error_code'))} {_text(result.get('error_stage'))}"
    eligible = result.get("eligible")
    conclusive = bool(result.get("conclusive"))
    if eligible is True and conclusive:
        return QUALIFICATION_ELIGIBLE
    if eligible is False and conclusive:
        return QUALIFICATION_INELIGIBLE
    # A country-selection skip carries its verdict in ``decision`` (no probe ran,
    # so there is no ``eligible`` flag to read).
    if _text(result.get("decision")) == "method_country_mismatch":
        return QUALIFICATION_INELIGIBLE
    if is_country_mismatch(result.get("error"), result.get("error_code"), result.get("decision")):
        return QUALIFICATION_INELIGIBLE
    if any(marker in error_text for marker in _AUTH_INVALID_MARKERS):
        return QUALIFICATION_AUTH_INVALID
    # Checked before the generic transport class: an ACL denial text contains
    # "connection" (``Tunnel connection failed``), so the order decides whether
    # the badge says 出口被拒 or the generic 检测失败.
    if any(marker in error_text for marker in _EGRESS_DENIED_MARKERS):
        return QUALIFICATION_EGRESS_DENIED
    status = _text(result.get("status"))
    if status == "failed" or any(marker in error_text for marker in _TRANSPORT_MARKERS):
        return QUALIFICATION_PROBE_FAILED
    # No error and not conclusive means the probe answered but the answer did
    # not decide eligibility (e.g. amount unknown).
    return QUALIFICATION_UNKNOWN


def qualification_label(result: Any, method: str = "") -> str:
    """Compose the 支付资格 badge text from the same result as the state."""
    state = qualification_state(result)
    source = result if isinstance(result, dict) else {}
    name = method_label(method or source.get("payment_method") or "")
    if state == QUALIFICATION_ELIGIBLE:
        label = f"可用·{name}" if name else "可用"
        kind = _text(source.get("checkout_kind") or source.get("probe_checkout_kind"))
        if kind == "oaics":
            label += "（自定义）"
        elif kind == "stripe":
            label += "（Stripe）"
        return label
    if state == QUALIFICATION_INELIGIBLE:
        decision = _text((result or {}).get("decision"))
        error_text = f"{_text((result or {}).get('error'))} {_text((result or {}).get('error_code'))}"
        if decision == "nonzero_offer":
            return "无0元优惠"
        if decision == "checkout_currency_mismatch":
            return "币种不符"
        if decision == "method_country_mismatch" or is_country_mismatch(error_text, decision):
            return "无可用方式·地区不符"
        return "无可用方式"
    if state == QUALIFICATION_AUTH_INVALID:
        return "AT失效"
    if state == QUALIFICATION_EGRESS_DENIED:
        return "出口被拒"
    if state == QUALIFICATION_PROBE_FAILED:
        error_text = f"{_text((result or {}).get('error'))} {_text((result or {}).get('error_code'))}"
        if is_risk_blocked(error_text):
            return "风控拦截"
        return "检测失败"
    return "未确定"


def qualification_state_group(state: str) -> str:
    """Coarse filter bucket used by the desktop column filter."""
    value = str(state or "").strip().lower()
    if value == QUALIFICATION_ELIGIBLE:
        return "qualified"
    if value in {QUALIFICATION_INELIGIBLE, QUALIFICATION_AUTH_INVALID}:
        return "blocked"
    # ``egress_denied`` lands here on purpose: nothing was learned about the
    # account, so it belongs with "unknown", not with the account-level blocks.
    return "unknown"


__all__ = [
    "COUNTRY_MISMATCH_DECISION",
    "METHOD_LABELS",
    "QUALIFICATION_AUTH_INVALID",
    "QUALIFICATION_EGRESS_DENIED",
    "QUALIFICATION_ELIGIBLE",
    "QUALIFICATION_INELIGIBLE",
    "QUALIFICATION_PROBE_FAILED",
    "QUALIFICATION_UNKNOWN",
    "is_country_mismatch",
    "is_risk_blocked",
    "method_label",
    "qualification_label",
    "qualification_state",
    "qualification_state_group",
]
