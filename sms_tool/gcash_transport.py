"""Production HTTP adapter for the GCash custom-payment-method seam."""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any, Mapping

from .gcash_provider import GCashProviderError, GCashTransportRequest
from .phone_proxy import normalize_proxy_url
from .sanitizer import sanitize_text


_BASE = "https://chatgpt.com/backend-api/payments"
_STRIPE_ELEMENTS_URL = "https://api.stripe.com/v1/elements/sessions"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) "
    "Gecko/20100101 Firefox/144.0"
)
_ROUTES = {
    "checkout": "/backend-api/payments/checkout",
    "update": "/backend-api/payments/checkout/update",
    "taxes": "/backend-api/payments/checkout/taxes",
    "confirm": "/backend-api/payments/checkout/confirm",
    "start": "/backend-api/payments/checkout/custom_payment_method/start",
}


class ChatGPTGCashTransport:
    def __init__(self, *, timeout: int = 60) -> None:
        self.timeout = max(5, int(timeout or 60))

    def create_checkout(self, request: GCashTransportRequest) -> Mapping[str, Any]:
        return self._post(request, f"{_BASE}/checkout", _ROUTES["checkout"])

    def update_checkout(self, request: GCashTransportRequest) -> Mapping[str, Any]:
        return self._post(request, f"{_BASE}/checkout/update", _ROUTES["update"])

    def update_taxes(self, request: GCashTransportRequest) -> Mapping[str, Any]:
        return self._post(request, f"{_BASE}/checkout/taxes", _ROUTES["taxes"])

    def resolve_checkout(self, request: GCashTransportRequest) -> Mapping[str, Any]:
        route = f"/backend-api/payments/checkout/{request.processor_entity}/{request.checkout_session_id}"
        session = _new_http_session(self._stage_proxy(request))
        try:
            response = session.get(
                f"{_BASE}/checkout/{request.processor_entity}/{request.checkout_session_id}",
                headers=self._headers(request, route),
                timeout=self.timeout,
            )
        except Exception as exc:
            raise GCashProviderError(
                _transport_error("resolve", exc),
                error_code="gcash_resolve_transport_failed",
                error_stage="resolve",
                retryable=True,
                status="failed",
            ) from exc
        return self._json_response(response, "resolve")

    def probe_custom_payment(self, request: GCashTransportRequest) -> Mapping[str, Any]:
        from .pp_link_helpers import DEFAULT_STRIPE_PK, STRIPE_VERSION

        checkout = request.payload.get("checkout")
        if not isinstance(checkout, Mapping):
            checkout = {}
        customer_secret = str(checkout.get("customer_session_client_secret") or "").strip()
        if not customer_secret:
            raise GCashProviderError(
                "checkout response did not contain a customer session secret",
                error_code="gcash_customer_session_missing",
                error_stage="custom_capability",
                status="unknown",
            )
        publishable_key = str(
            checkout.get("publishable_key")
            or checkout.get("stripe_publishable_key")
            or DEFAULT_STRIPE_PK
        ).strip()
        params: dict[str, str] = {
            "customer_session_client_secret": customer_secret,
            "client_betas[0]": "custom_checkout_server_updates_1",
            "client_betas[1]": "custom_checkout_manual_approval_1",
            "deferred_intent[mode]": "subscription",
            "deferred_intent[amount]": "0",
            "deferred_intent[currency]": request.contract.currency.lower(),
            "currency": request.contract.currency.lower(),
            "key": publishable_key,
            "elements_init_source": "stripe.elements",
            "referrer_host": "chatgpt.com",
            "stripe_js_id": str(uuid.uuid4()),
            "locale": request.contract.payment_locale,
            "type": "deferred_intent",
            "deferred_intent[setup_future_usage]": "off_session",
            "_stripe_version": STRIPE_VERSION,
        }
        methods = checkout.get("payment_method_types")
        if isinstance(methods, list):
            for index, method in enumerate(methods):
                token = str(method or "").strip().lower()
                if token:
                    params[f"deferred_intent[payment_method_types][{index}]"] = token
        custom_methods = checkout.get("custom_payment_methods")
        if not isinstance(custom_methods, list) or not custom_methods:
            custom_methods = [request.payload.get("custom_payment_method_type_id")]
        for index, method in enumerate(custom_methods):
            token = str(method or "").strip()
            if token:
                params[f"custom_payment_methods[{index}]"] = token
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            "Authorization": f"Bearer {publishable_key}",
            "Origin": "https://js.stripe.com",
            "Referer": "https://js.stripe.com/",
        }
        session = _new_http_session(self._stage_proxy(request))
        try:
            response = session.get(
                _STRIPE_ELEMENTS_URL,
                params=params,
                headers=headers,
                timeout=self.timeout,
            )
        except Exception as exc:
            raise GCashProviderError(
                _transport_error("custom capability", exc),
                error_code="gcash_custom_capability_transport_failed",
                error_stage="custom_capability",
                retryable=True,
                status="failed",
            ) from exc
        return self._json_response(response, "custom_capability")

    def confirm_custom_payment(self, request: GCashTransportRequest) -> Mapping[str, Any]:
        return self._post(request, f"{_BASE}/checkout/confirm", _ROUTES["confirm"])

    def start_custom_payment(self, request: GCashTransportRequest) -> Mapping[str, Any]:
        return self._post(request, f"{_BASE}/checkout/custom_payment_method/start", _ROUTES["start"])

    def _post(self, request: GCashTransportRequest, url: str, route: str) -> Mapping[str, Any]:
        from . import gen_pp_link

        try:
            response = gen_pp_link._checkout_post(
                url,
                dict(request.payload),
                request.access_token,
                str(request.auth_context.get("cookie_header") or ""),
                self._stage_proxy(request),
                self.timeout,
                extra_headers=self._headers(request, route, include_authorization=False),
            )
        except Exception as exc:
            raise GCashProviderError(
                _transport_error(request.stage, exc),
                error_code=f"gcash_{request.stage}_transport_failed",
                error_stage=request.stage,
                retryable=request.stage not in {"confirm", "start"},
                status="unknown" if request.stage in {"confirm", "start"} else "failed",
            ) from exc
        return self._json_response(response, request.stage)

    def _stage_proxy(self, request: GCashTransportRequest) -> str:
        plan = request.transport_context.get("payment_route_plan")
        if plan is not None and hasattr(plan, "proxy_for"):
            canonical_stage = {
                "update": "promotion",
                "taxes": "stripe_init",
                "resolve": "stripe_init",
                "custom_capability": "payment_method",
                "start": "redirect",
            }.get(request.stage, request.stage)
            planned = str(plan.proxy_for(canonical_stage) or "").strip()
            if planned:
                return normalize_proxy_url(planned)
        keys = {
            "checkout": ("checkout_proxy",),
            "update": ("promotion_proxy", "update_proxy"),
            "taxes": ("provider_proxy", "checkout_proxy"),
            "resolve": ("provider_proxy", "checkout_proxy"),
            "custom_capability": ("provider_proxy", "checkout_proxy"),
            "confirm": ("confirm_proxy", "provider_proxy", "checkout_proxy"),
            "start": ("provider_proxy", "confirm_proxy", "checkout_proxy"),
        }.get(request.stage, ())
        for key in (*keys, "default_proxy", "proxy"):
            value: Any = request.transport_context.get(key)
            if isinstance(value, Mapping):
                value = value.get("https") or value.get("http") or ""
            if str(value or "").strip():
                return normalize_proxy_url(str(value).strip())
        return ""

    @staticmethod
    def _headers(
        request: GCashTransportRequest,
        route: str,
        *,
        include_authorization: bool = True,
    ) -> dict[str, str]:
        account_id = _account_id(request.access_token, request.auth_context)
        device_id = str(request.auth_context.get("device_id") or "").strip() or str(uuid.uuid4())
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "User-Agent": _USER_AGENT,
            "OAI-Device-Id": device_id,
            "x-openai-target-path": route,
            "x-openai-target-route": route,
        }
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        if include_authorization:
            headers["Authorization"] = f"Bearer {request.access_token}"
            cookie = str(request.auth_context.get("cookie_header") or "").strip()
            if cookie:
                headers["Cookie"] = cookie
        return headers

    @staticmethod
    def _json_response(response: Any, stage: str) -> Mapping[str, Any]:
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code >= 400:
            detail = _response_error_detail(response)
            if (
                stage == "confirm"
                and status_code == 400
                and "unsupported custom payment method" in detail.lower()
            ):
                raise GCashProviderError(
                    "GCash custom payment method is not supported by this checkout session",
                    error_code="gcash_custom_method_unsupported",
                    error_stage=stage,
                    retryable=False,
                    status="failed",
                )
            if stage == "confirm" and status_code == 409:
                raise GCashProviderError(
                    "GCash checkout was not confirmed",
                    error_code="gcash_checkout_not_confirmed",
                    error_stage=stage,
                    retryable=False,
                    status="failed",
                )
            uncertain = stage in {"confirm", "start"}
            raise GCashProviderError(
                f"{stage} returned HTTP {status_code}{detail}",
                error_code=f"gcash_{stage}_http_error",
                error_stage=stage,
                retryable=not uncertain and (status_code in {408, 425, 429} or status_code >= 500),
                status="unknown" if uncertain else "failed",
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise GCashProviderError(
                f"{stage} returned invalid JSON",
                error_code=f"gcash_{stage}_bad_json",
                error_stage=stage,
                retryable=stage not in {"confirm", "start"},
                status="unknown" if stage in {"confirm", "start"} else "failed",
            ) from exc
        if not isinstance(payload, Mapping):
            raise GCashProviderError(
                f"{stage} returned a non-object JSON payload",
                error_code=f"gcash_{stage}_bad_json",
                error_stage=stage,
                retryable=False,
                status="unknown" if stage in {"confirm", "start"} else "failed",
            )
        return payload


def _account_id(access_token: str, auth_context: Mapping[str, Any]) -> str:
    direct = str(auth_context.get("account_id") or auth_context.get("chatgpt_account_id") or "").strip()
    if direct:
        return direct
    account = auth_context.get("account")
    if isinstance(account, Mapping):
        direct = str(account.get("id") or account.get("account_id") or "").strip()
        if direct:
            return direct
    auth_session = auth_context.get("auth_session")
    if isinstance(auth_session, Mapping):
        account = auth_session.get("account")
        if isinstance(account, Mapping):
            direct = str(account.get("id") or account.get("account_id") or "").strip()
            if direct:
                return direct
    try:
        part = str(access_token or "").split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return ""
    auth = claims.get("https://api.openai.com/auth") if isinstance(claims, Mapping) else {}
    if isinstance(auth, Mapping):
        return str(auth.get("chatgpt_account_id") or auth.get("account_id") or "").strip()
    return ""


def _new_http_session(proxy: str):
    normalized = normalize_proxy_url(str(proxy or "").strip())
    try:
        from curl_cffi.requests import Session as CurlSession

        session = CurlSession(impersonate="firefox144")
    except (ImportError, TypeError):
        import requests

        session = requests.Session()
    try:
        session.trust_env = False
    except Exception:
        pass
    session.headers.update({"User-Agent": _USER_AGENT})
    if normalized:
        session.proxies = {"http": normalized, "https": normalized}
    return session


def _transport_error(stage: str, exc: BaseException) -> str:
    detail = sanitize_text(exc).replace("\r", " ").replace("\n", " ").strip()
    if len(detail) > 240:
        detail = detail[:237] + "..."
    suffix = f": {detail}" if detail else ""
    return f"{stage} transport failed ({type(exc).__name__}){suffix}"


def _response_error_detail(response: Any) -> str:
    try:
        payload = response.json()
    except Exception:
        return ""
    if isinstance(payload, Mapping):
        values = [payload.get(key) for key in ("detail", "error", "message", "code")]
        text = " | ".join(sanitize_text(value) for value in values if value not in (None, ""))
    else:
        text = sanitize_text(payload)
    text = " ".join(str(text).split())
    if not text:
        return ""
    return ": " + (text[:237] + "..." if len(text) > 240 else text)


__all__ = ["ChatGPTGCashTransport"]
