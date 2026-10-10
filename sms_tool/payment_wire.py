#!/usr/bin/env python3
r"""中性支付内核 —— Checkout 原语、会话工厂与共享失败词汇。

自 ``paypal_extract.py`` 逐字搬迁（零行为变化）。本模块拥有**跨泳道共享**的
那部分支付能力，因此刻意不以 PayPal 命名：

* ``_new_session`` -- 非 Checkout 阶段（Stripe / approve / ...）的 requests 会话
* ``_checkout_*`` -- Checkout 原语：请求头、device id、Sentinel 顾问头、
  ``_checkout_post`` / ``_checkout_get``
* ``CURRENCY_MAP`` -- 国家 -> 货币映射（checkout billing 用）
* ``_PROVIDER_STAGES`` -- 供应商阶段顺序
* 五个失败词汇类 -- ``CheckoutNotZeroDueError`` / ``PayPalHttpError`` /
  ``CheckoutApprovalBlockedError`` / ``PayPalCapabilityError`` /
  ``PaymentOutcomeUnknownError``
* ``_compact_diagnostic`` -- 诊断文本压缩助手

消费者（UPI 泳道、账号支付资格、capability probe、PayPal 链接）一律从本模块
导入，不再经过以 PayPal 命名的模块。``paypal_extract`` 仍是 PayPal 提链器
``PPLinkExtractor`` 的归属模块，并再导出本模块的符号以保持历史导入与
``patch(...)`` 目标可用。

依赖方向: ``paypal_extract`` -> 本模块。本模块**不得** import
``paypal_extract`` / ``pp_link_helpers`` / ``checkout_contract`` /
``paypal_proxy`` —— 那会把 PayPal 专属依赖拉回中性内核。``CHATGPT_TIMEOUT``
取自 ``timeouts``（常量唯一来源），而不是从 ``pp_link_helpers`` 转一手。
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

import requests

try:
    from .auth_headers import AUTH_FINGERPRINT_PROFILES
except ImportError:  # pragma: no cover - direct script execution
    from auth_headers import AUTH_FINGERPRINT_PROFILES  # type: ignore

try:
    from .timeouts import CHATGPT_TIMEOUT
except ImportError:  # pragma: no cover - direct script execution
    from timeouts import CHATGPT_TIMEOUT  # type: ignore

# curl_cffi functional API (preferred for checkout to avoid Session cookie conflicts)
try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None


# The cross-lane kernel's surface, and it is exactly the ownership list
# ``docs/architecture.md`` Boundary Rule 20 gives this module -- nothing more.
#
# Deliberately **not** here: ``json`` / ``re`` / ``urlsplit`` / ``requests`` /
# ``curl_requests`` / ``AUTH_FINGERPRINT_PROFILES`` / ``CHATGPT_TIMEOUT``.  Those
# are this module's own imports, not symbols it owns; declaring them would repeat
# the mechanical-split leak that put ``json`` and ``re`` into
# ``paypal_link.__all__`` (see ``tests/test_all_export_hygiene.py``).  They stay
# reachable as attributes, which is all a ``patch.object(payment_wire, ...)``
# target needs.
#
# The leading underscores are part of the contract, not an oversight: the
# Checkout primitives are named that way in Rule 20 and are called across lanes
# (``paypal_extract``, ``upi_link``, ``payment_capability``), so they are the
# interface.
__all__ = [
    "CURRENCY_MAP",
    "CheckoutApprovalBlockedError",
    "CheckoutNotZeroDueError",
    "PayPalCapabilityError",
    "PayPalHttpError",
    "PaymentOutcomeUnknownError",
    "_PROVIDER_STAGES",
    "_checkout_device_id",
    "_checkout_get",
    "_checkout_headers",
    "_checkout_post",
    "_checkout_sentinel_headers",
    "_compact_diagnostic",
    "_is_checkout_create_url",
    "_new_session",
]


# ─── 共享内核（自 paypal_extract 逐字搬迁）────────────────────────────────────

class CheckoutNotZeroDueError(Exception):
    error_code = "checkout_not_zero_due"
    error_stage = "eligibility"
    status = "failed"
    retryable = False

    def __init__(self, amount: int | None, currency: str = ""):
        self.amount = amount
        self.currency = str(currency or "").upper()
        amount_label = "unknown" if amount is None else str(amount)
        super().__init__(f"checkout_not_zero_due: amount={amount_label} {self.currency}".rstrip())


class PayPalHttpError(Exception):
    """Structured, redacted HTTP failure for the PayPal checkout workflow."""

    def __init__(self, stage: str, endpoint: str, response: Any, *, retryable: bool = False):
        self.error_stage = str(stage or "adapter")
        self.http_status = int(getattr(response, "status_code", 0) or 0)
        parts = urlsplit(str(endpoint or ""))
        self.endpoint = f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.netloc else parts.path
        self.provider_error_code = ""
        payload: Any = None
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            for key in ("code", "error_code", "type", "error", "detail"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    self.provider_error_code = value.strip()[:120]
                    break
                if isinstance(value, dict):
                    nested = value.get("code") or value.get("type") or value.get("message")
                    if nested:
                        self.provider_error_code = str(nested).strip()[:120]
                        break
        raw = payload if payload is not None else str(getattr(response, "text", "") or "")
        self.response_summary = _compact_diagnostic(
            json.dumps(raw, ensure_ascii=False) if not isinstance(raw, str) else raw
        )
        self.retryable = bool(retryable)
        super().__init__(self._message())

    def _message(self) -> str:
        code = f" code={self.provider_error_code}" if self.provider_error_code else ""
        return f"{self.error_stage} HTTP {self.http_status}{code}: {self.response_summary}".rstrip()

    def diagnostic(self) -> dict[str, Any]:
        return {
            "http_status": self.http_status,
            "endpoint": self.endpoint,
            "provider_error_code": self.provider_error_code,
            "response_summary": self.response_summary,
        }


class CheckoutApprovalBlockedError(PayPalHttpError):
    """Approval was explicitly blocked; rebuild the Checkout from scratch."""

    rebuild_required = True

    def __init__(self, endpoint: str, response: Any):
        super().__init__("approve", endpoint, response, retryable=True)
        self.error_code = "approve_blocked"
        self.status = "failed"


class PayPalCapabilityError(Exception):
    error_stage = "eligibility"
    status = "failed"
    retryable = False

    def __init__(self, message: str = "paypal_payment_method_unavailable"):
        self.error_code = "paypal_payment_method_unavailable"
        super().__init__(message)


class PaymentOutcomeUnknownError(Exception):
    """A side-effect request was issued but its outcome could not be established.

    ``confirm`` / ``approve`` / ``poll`` are side-effect stages (see
    ``_SIDE_EFFECT_STAGES``): once the request leaves the process, a local
    failure cannot prove the server rejected it.  Re-driving the stage would
    risk a duplicate authorization, so the run terminates as ``unknown`` and the
    caller reconciles instead of retrying.  The attribute names match what
    ``payment_link_manager._classify_exception`` reads.
    """

    status = "unknown"
    retryable = False
    outcome_unknown = True

    def __init__(
        self,
        message: str,
        *,
        stage: str = "approve",
        error_code: str = "payment_outcome_unknown",
    ):
        self.error_stage = stage
        self.stage = stage
        self.error_code = error_code
        super().__init__(message)


# Provider-stage order; every entry owns a ``<stage>_proxy`` attribute.
_PROVIDER_STAGES = ("provider", "stripe_init", "payment_method", "confirm")

CURRENCY_MAP = {
    "US": "USD",
    "GB": "GBP",
    "DE": "EUR",
    "FR": "EUR",
    "JP": "JPY",
    "AU": "AUD",
    "CA": "CAD",
    "SG": "SGD",
    "NZ": "NZD",
    "IE": "EUR",
    "TH": "THB",
    "ID": "IDR",
    "IN": "INR",
    "BR": "BRL",
    "KR": "KRW",
    "TR": "TRY",
}

# ─── Session 工厂 ─────────────────────────────────────────────────────────────


def _new_session(proxy: str = ""):
    """Create a requests Session for non-checkout stages (Stripe, approve, etc.).
    The checkout stage uses ``_checkout_post`` instead to avoid Cloudflare
    session-cookie conflicts."""
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        }
    )
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


_CHECKOUT_FINGERPRINT = AUTH_FINGERPRINT_PROFILES["chrome124"]


def _checkout_headers(access_token, cookie_header="", extra_headers=None):
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Referer": "https://chatgpt.com/",
        "User-Agent": _CHECKOUT_FINGERPRINT["user_agent"],
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    if extra_headers:
        headers.update(extra_headers)
    # Endpoint-specific headers must not change the TLS/browser pairing.
    headers["User-Agent"] = _CHECKOUT_FINGERPRINT["user_agent"]
    return headers


#: The Checkout *create* path. Only this route needs the ``chatgpt_checkout``
#: Sentinel pair; update/taxes/approve/confirm carry their own gates.
_CHECKOUT_CREATE_PATH = "/backend-api/payments/checkout"


def _checkout_device_id(cookie_header, extra_headers):
    """Resolve the device id the checkout request will present.

    Prefer an explicit ``OAI-Device-Id`` header (the capability/gcash/wallet
    transports set one) and fall back to the account's ``oai-did`` cookie. The
    Sentinel token is device-bound, so a mismatch would invalidate the pair.
    """
    for key, value in (extra_headers or {}).items():
        if str(key).strip().lower() in {"oai-device-id", "oai-device_id"}:
            candidate = str(value or "").strip()
            if candidate:
                return candidate
    for part in str(cookie_header or "").split(";"):
        name, _, value = part.partition("=")
        if name.strip().lower() == "oai-did":
            candidate = value.strip().strip('"')
            if candidate:
                return candidate
    return ""


def _is_checkout_create_url(url):
    return str(url or "").split("?", 1)[0].rstrip("/").endswith(_CHECKOUT_CREATE_PATH)


def _checkout_sentinel_headers(cookie_header, extra_headers, proxy):
    """Advisory ``chatgpt_checkout`` token pair for a Checkout create POST.

    Without both ``openai-sentinel-token`` and ``openai-sentinel-so-token`` the
    create endpoint answers ``400 unusual activity``. Never fatal: a missing
    local runner degrades to no headers so the caller keeps its own retry /
    classification path.
    """
    device_id = _checkout_device_id(cookie_header, extra_headers)
    if not device_id:
        return {}
    try:
        from .sentinel import checkout_sentinel_headers

        return checkout_sentinel_headers(
            device_id=device_id,
            proxy=proxy or "",
            cookie_header=str(cookie_header or ""),
            timeout_seconds=CHATGPT_TIMEOUT,
        )
    except Exception:
        return {}


def _checkout_post(url, json_body, access_token, cookie_header="", proxy="", timeout=30, extra_headers=None):
    """Execute a ChatGPT checkout POST using the functional curl_cffi API.

    The functional API (not Session) is required here because ``curl_cffi``
    Session accumulates a Cloudflare ``__cf_bm`` cookie that conflicts with
    the account's own cookies and causes 403 Forbidden on checkout.

    ``extra_headers`` lets callers add endpoint-specific headers such as
    ``x-openai-target-path``/``x-openai-target-route`` for /checkout/update
    and /checkout/taxes, plus a per-session Referer.
    """
    headers = _checkout_headers(access_token, cookie_header, extra_headers)
    if _is_checkout_create_url(url):
        for name, value in _checkout_sentinel_headers(cookie_header, extra_headers, proxy).items():
            headers.setdefault(name, value)
    headers["Content-Type"] = "application/json"
    proxies = {"http": proxy, "https": proxy} if proxy else None
    if curl_requests is None:
        raise RuntimeError("curl_cffi is required for Checkout browser impersonation")
    return curl_requests.post(
        url,
        json=json_body,
        headers=headers,
        # curl_cffi's ProxySpec is an invariant dict alias, so Pyright rejects the
        # dict[str, str] the API actually accepts.
        proxies=proxies,  # pyright: ignore[reportArgumentType]
        timeout=timeout,
        impersonate=_CHECKOUT_FINGERPRINT["impersonate"],
    )


def _checkout_get(url, access_token, cookie_header="", proxy="", timeout=30, extra_headers=None):
    """Read a custom Checkout session without accumulating unrelated cookies."""
    if curl_requests is None:
        raise RuntimeError("curl_cffi is required for Checkout browser impersonation")
    headers = _checkout_headers(access_token, cookie_header, extra_headers)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    return curl_requests.get(
        url,
        headers=headers,
        # same invariant-alias false positive as _checkout_post
        proxies=proxies,  # pyright: ignore[reportArgumentType]
        timeout=timeout,
        impersonate=_CHECKOUT_FINGERPRINT["impersonate"],
    )


# ─── 代理工具 ──────────────────────────────────────────────────────────────────


def _compact_diagnostic(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."
