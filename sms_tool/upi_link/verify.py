"""交付前核验：一条 hosted_instructions_url 到底是真链还是废链。

**为什么必须核验**
``setup_intent.next_action.upi_handle_redirect_or_display_qr_code.hosted_instructions_url``
在 setup_intent **被拒**（``requires_payment_method``）时**照样会返回**。只看「有没有
URL」就把链接交出去，用户打开看到的会是 ₹1999 付款页，而不是 ₹0 的 UPI Autopay 委托。

**判据（两个都要满足）**

1. ``intent_state ∈ {requires_action, processing}`` —— 委托真的签出来了；
2. UPI URI 里 ``fam != 1999.00`` —— ``fam=1.00`` 是「本次扣 ₹0/₹1、授权上限 ₹1999」，
   ``fam=1999.00`` 是 ₹1999 付款链。

注意 ``am=1999.00`` 是**授权上限**（``amrule=MAX``），不是本次扣款额，不能拿它判零元。

``hosted_instructions_url`` 是公网可直连的，但直连被拦时用当前出口再读一次；读取是
幂等的，所以失败会重试。仍然失败返回 ``unreachable`` / ``http_5xx`` 一类，属于
**「没结论」而不是「链是假的」**，见 :data:`INCONCLUSIVE`。

Ported 2026-09-30 from the reference ``upi-zero-link/upi_zero_link/verify.py``.
"""

from __future__ import annotations

import re
import time
from typing import Any

from ._extract import _upi_decode_base64url_json, _upi_is_instructions_url

try:  # pragma: no cover - direct script execution
    from ..paypal_extract import _new_session
except ImportError:  # pragma: no cover - direct script execution
    from paypal_extract import _new_session  # type: ignore

#: ``<meta id="payload" data-message="<base64url>">`` —— 两个属性顺序都可能。
PAYLOAD_META_RE = re.compile(r'<meta\b[^>]*\bid=["\']payload["\'][^>]*\bdata-message=["\']([^"\']+)', re.I)
PAYLOAD_META_RE_REVERSED = re.compile(r'<meta\b[^>]*\bdata-message=["\']([^"\']+)[^>]*\bid=["\']payload["\']', re.I)
FAM_RE = re.compile(r"[?&]fam=([^&]+)")

#: States that mean the mandate was actually signed.
PASSING_STATES = ("requires_action", "processing")
#: ``fam`` values that identify the ₹1999 payment chain (not the ₹0 mandate).
PAYMENT_CHAIN_FAM = ("1999.00", "1999")
#: Outcomes that mean "we could not tell", not "the link is fake".
INCONCLUSIVE = frozenset({"unreachable", "http_5xx", "no_payload", "empty_url"})


def decode_instructions_payload(html: str) -> dict[str, Any] | None:
    """Decode the ``payload`` meta tag of a Stripe UPI instructions page."""
    text = str(html or "")
    match = PAYLOAD_META_RE.search(text) or PAYLOAD_META_RE_REVERSED.search(text)
    if not match:
        return None
    payload = _upi_decode_base64url_json(match.group(1).replace("&quot;", '"'))
    return payload if isinstance(payload, dict) else None


def judge_instructions_payload(payload: dict[str, Any]) -> tuple[bool, str]:
    """Return ``(is_real_link, label)`` for a decoded instructions payload.

    ``requires_action`` / ``processing`` = just issued, deliverable;
    ``requires_payment_method`` = Stripe declined, no mandate (fake link);
    ``canceled`` = window closed or superseded by a later attempt;
    ``succeeded`` = already authorised (do not deliver it twice).
    """
    state = str(payload.get("intent_state") or "")
    uri = str(payload.get("mobile_auth_url") or payload.get("upi_uri") or "")
    match = FAM_RE.search(uri)
    fam = match.group(1) if match else ""
    label = f"state={state or 'unknown'} fam={fam or '?'}"
    if state not in PASSING_STATES:
        return False, label
    if fam in PAYMENT_CHAIN_FAM:
        return False, label
    return True, label


def verify_instructions_url(
    url: str,
    *,
    proxy: str = "",
    timeout: float = 30.0,
    attempts: int = 3,
    session_factory: Any = None,
    accept: str = "text/html,application/xhtml+xml,*/*;q=0.8",
) -> tuple[bool, str]:
    """Fetch an instructions page and judge it. Returns ``(is_real_link, label)``.

    ``proxy`` is the fallback route: the page is public, so a direct read is
    tried first and the egress is only used when the direct read is blocked.
    ``session_factory(proxy)`` mirrors ``extract._new_session`` so tests can
    inject a stub.
    """
    if not url:
        return False, "empty_url"
    factory = session_factory or _new_session
    try:
        total_attempts = max(1, int(attempts))
    except (TypeError, ValueError):
        total_attempts = 1

    routes: list[str] = ["", str(proxy or "").strip()] if str(proxy or "").strip() else [""]
    last = "unreachable"
    for route in routes:
        session = None
        try:
            session = factory(route)
        except Exception:
            session = None
        for attempt in range(total_attempts):
            try:
                resp = session.get(url, timeout=timeout, headers={"Accept": accept}) if session is not None else None
            except Exception:
                resp = None
            if resp is not None:
                try:
                    status = int(getattr(resp, "status_code", 0) or 0)
                except (TypeError, ValueError):
                    status = 0
                if status < 400:
                    payload = decode_instructions_payload(getattr(resp, "text", "") or "")
                    if payload is None:
                        return False, "no_payload"
                    return judge_instructions_payload(payload)
                last = f"http_{status}"
                if status < 500:
                    return False, last  # 4xx is a definite verdict: token dead/expired
            if attempt + 1 < total_attempts:
                time.sleep(0.6 * (attempt + 1))
    return False, last


def is_verification_target(url: str) -> bool:
    """True when ``url`` is an instructions page worth verifying."""
    return _upi_is_instructions_url(str(url or ""))


__all__ = [
    "FAM_RE",
    "INCONCLUSIVE",
    "PASSING_STATES",
    "PAYMENT_CHAIN_FAM",
    "decode_instructions_payload",
    "is_verification_target",
    "judge_instructions_payload",
    "verify_instructions_url",
]
