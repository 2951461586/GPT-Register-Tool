"""HTTP transport for the shared GoPay and GrabPay wallet adapter."""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

from .checkout_contract import CHECKOUT_PATH, CHECKOUT_URL, STRIPE_INIT_URL
from .wallet_provider import WALLET_METHODS, WalletProviderError, WalletTransportRequest


PAYMENT_METHOD_URL = "https://api.stripe.com/v1/payment_methods"
UPDATE_PATH = "/backend-api/payments/checkout/update"
UPDATE_URL = f"https://chatgpt.com{UPDATE_PATH}"
APPROVE_PATH = "/backend-api/payments/checkout/approve"
APPROVE_URL = f"https://chatgpt.com{APPROVE_PATH}"
_STRIPE_PAGE_URL = "https://api.stripe.com/v1/payment_pages/{checkout_session_id}"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_BASE_REDIRECT_HOSTS = frozenset({
    "pm-redirects.stripe.com",
    "hooks.stripe.com",
    "checkout.stripe.com",
    "pay.openai.com",
    "chatgpt.com",
})
_HTML_REDIRECT_RE = re.compile(
    r"(?i)(?:url\s*=\s*|window\.location(?:\.href)?\s*=\s*)[\"']?"
    r"(https://[^\"'<>\s]+)"
)


class WalletHTTPError(RuntimeError):
    def __init__(self, message: str, status_code: int = 0) -> None:
        self.status_code = int(status_code or 0)
        super().__init__(message)


class ChatGPTStripeWalletTransport:
    """Production wire transport with stage-specific proxy routing."""

    def __init__(self, *, timeout: int = 45, max_redirect_hops: int = 8) -> None:
        self.timeout = max(5, int(timeout or 45))
        self.max_redirect_hops = max(1, min(int(max_redirect_hops or 8), 12))
        self._sessions: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()

    def create_checkout(self, request: WalletTransportRequest) -> Mapping[str, Any]:
        from . import gen_pp_link

        proxy = self._stage_proxy(request)
        cookie_header = str(request.auth_context.get("cookie_header") or "")
        try:
            response = gen_pp_link._checkout_post(
                CHECKOUT_URL,
                dict(request.payload),
                request.access_token,
                cookie_header,
                proxy,
                self.timeout,
                extra_headers={
                    "x-openai-target-path": CHECKOUT_PATH,
                    "x-openai-target-route": CHECKOUT_PATH,
                },
            )
        except Exception as exc:
            raise WalletHTTPError(f"checkout transport failed: {type(exc).__name__}") from exc
        return self._json_response(response, request.stage)

    def update_checkout(self, request: WalletTransportRequest) -> Mapping[str, Any]:
        from . import gen_pp_link

        cookie_header = str(request.auth_context.get("cookie_header") or "")
        proxy = self._stage_proxy(request)
        referer = f"https://chatgpt.com/checkout/{request.processor_entity}/{request.checkout_session_id}"
        try:
            response = gen_pp_link._checkout_post(
                UPDATE_URL,
                dict(request.payload),
                request.access_token,
                cookie_header,
                proxy,
                self.timeout,
                extra_headers={
                    "Referer": referer,
                    "x-openai-target-path": UPDATE_PATH,
                    "x-openai-target-route": UPDATE_PATH,
                },
            )
        except Exception as exc:
            raise WalletHTTPError(f"promotion transport failed: {type(exc).__name__}") from exc
        return self._json_response(response, request.stage)

    def stripe_init(self, request: WalletTransportRequest) -> Mapping[str, Any]:
        response = self._session(request).post(
            STRIPE_INIT_URL.format(checkout_session_id=request.checkout_session_id),
            data=dict(request.payload),
            headers=self._stripe_headers(request),
            timeout=self.timeout,
        )
        return self._json_response(response, request.stage)

    def create_payment_method(self, request: WalletTransportRequest) -> Mapping[str, Any] | str:
        response = self._session(request).post(
            PAYMENT_METHOD_URL,
            data=dict(request.payload),
            headers=self._stripe_headers(request),
            timeout=self.timeout,
        )
        return self._json_response(response, request.stage)

    def confirm_payment(self, request: WalletTransportRequest) -> Mapping[str, Any]:
        response = self._session(request).post(
            _STRIPE_PAGE_URL.format(checkout_session_id=request.checkout_session_id) + "/confirm",
            data=dict(request.payload),
            headers=self._stripe_headers(request),
            timeout=self.timeout,
        )
        return self._json_response(response, request.stage)

    def approve_checkout(self, request: WalletTransportRequest) -> Mapping[str, Any]:
        from . import gen_pp_link

        cookie_header = str(request.auth_context.get("cookie_header") or "")
        proxy = self._stage_proxy(request)
        referer = f"https://chatgpt.com/checkout/{request.processor_entity}/{request.checkout_session_id}"
        try:
            response = gen_pp_link._checkout_post(
                APPROVE_URL,
                dict(request.payload),
                request.access_token,
                cookie_header,
                proxy,
                self.timeout,
                extra_headers={
                    "Referer": referer,
                    "x-openai-target-path": APPROVE_PATH,
                    "x-openai-target-route": APPROVE_PATH,
                },
            )
        except Exception as exc:
            raise WalletHTTPError(f"approve transport failed: {type(exc).__name__}") from exc
        return self._json_response(response, request.stage)

    def poll_payment(self, request: WalletTransportRequest) -> Mapping[str, Any]:
        response = self._session(request).get(
            _STRIPE_PAGE_URL.format(checkout_session_id=request.checkout_session_id),
            params=dict(request.payload),
            headers=self._stripe_headers(request),
            timeout=self.timeout,
        )
        return self._json_response(response, request.stage)

    def follow_redirect(self, request: WalletTransportRequest) -> Mapping[str, Any] | str:
        current = str(request.redirect_url or "").strip()
        if not current:
            raise WalletProviderError(
                "wallet redirect URL is missing",
                error_code="wallet_redirect_missing",
                error_stage="follow_redirect",
            )
        session = self._session(request)
        seen: set[str] = set()
        for _ in range(self.max_redirect_hops):
            self._validate_redirect_url(current, request.method)
            fingerprint = _url_fingerprint(current)
            if fingerprint in seen:
                raise WalletProviderError(
                    "wallet redirect loop detected",
                    error_code="wallet_redirect_loop",
                    error_stage="follow_redirect",
                )
            seen.add(fingerprint)
            response = session.get(
                current,
                headers={
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Referer": "https://checkout.stripe.com/",
                },
                timeout=self.timeout,
                allow_redirects=False,
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code in _REDIRECT_STATUSES:
                location = str((getattr(response, "headers", {}) or {}).get("Location") or "").strip()
                if not location:
                    raise WalletHTTPError("wallet redirect response omitted Location", status_code)
                current = urljoin(current, location)
                continue
            if status_code >= 400:
                raise WalletHTTPError(f"follow_redirect returned HTTP {status_code}", status_code)
            body = str(getattr(response, "text", "") or "")[:20000]
            candidate = _html_redirect(body)
            if candidate:
                current = urljoin(current, candidate)
                continue
            final_url = str(getattr(response, "url", "") or current).strip()
            self._validate_redirect_url(final_url, request.method)
            return {"final_url": final_url}
        raise WalletProviderError(
            "wallet redirect exceeded the hop limit",
            error_code="wallet_redirect_hop_limit",
            error_stage="follow_redirect",
            retryable=False,
            status="unknown",
        )

    def _session(self, request: WalletTransportRequest) -> Any:
        from . import gen_pp_link

        proxy = self._stage_proxy(request)
        key = (request.flow_id, proxy)
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = gen_pp_link._new_session(proxy)
                self._sessions[key] = session
        return session

    def _stage_proxy(self, request: WalletTransportRequest) -> str:
        context = request.transport_context
        plan = context.get("payment_route_plan")
        if plan is not None and hasattr(plan, "proxy_for"):
            canonical_stage = {
                "final_review": "approve",
                "follow_redirect": "redirect",
            }.get(request.stage, request.stage)
            planned = str(plan.proxy_for(canonical_stage) or "").strip()
            if planned:
                return self._attempt_proxy(request, planned, planned=True)
        stage_keys = {
            "checkout": ("checkout_proxy",),
            "promotion": ("promotion_proxy", "update_proxy"),
            "stripe_init": ("stripe_init_proxy", "provider_proxy"),
            "payment_method": ("payment_method_proxy", "provider_proxy"),
            "confirm": ("confirm_proxy", "provider_proxy"),
            "approve": ("approve_proxy", "final_review_proxy", "checkout_proxy"),
            "final_review": ("final_review_proxy", "approve_proxy", "checkout_proxy"),
            "poll": ("provider_proxy", "stripe_init_proxy"),
            "follow_redirect": ("redirect_proxy", "provider_proxy"),
        }
        for key in (*stage_keys.get(request.stage, ()), "proxy", "default_proxy"):
            value = context.get(key)
            if isinstance(value, Mapping):
                value = value.get("https") or value.get("http") or ""
            if str(value or "").strip():
                return self._attempt_proxy(request, str(value).strip())
        return ""

    @staticmethod
    def _attempt_proxy(request: WalletTransportRequest, proxy: str, *, planned: bool = False) -> str:
        context = request.transport_context
        resolver = (
            context.get(f"{request.stage}_proxy_resolver")
            or (
                context.get("final_review_proxy_resolver")
                if request.stage == "approve"
                else None
            )
            or context.get("proxy_resolver")
        )
        if callable(resolver):
            resolved = resolver(request.stage, request.attempt, proxy)
            if isinstance(resolved, Mapping):
                resolved = resolved.get("https") or resolved.get("http") or ""
            value = str(resolved or "").strip()
            if value:
                return value
        if planned:
            return proxy
        rotate = context.get("rotate_proxy_sessions")
        if isinstance(rotate, str):
            rotate = rotate.strip().lower() in {"1", "true", "yes", "on"}
        if not rotate or request.stage not in {"approve", "final_review", "poll"}:
            return proxy
        countries = context.get("stage_proxy_countries")
        if not isinstance(countries, Mapping):
            countries = {}
        country_key = "approve" if request.stage == "final_review" else request.stage
        country = str(countries.get(country_key) or countries.get("provider") or "").strip().upper()
        from .paypal_proxy import rotate_proxy_session

        return rotate_proxy_session(proxy, country)

    @staticmethod
    def _stripe_headers(request: WalletTransportRequest) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://checkout.stripe.com",
            "Referer": f"https://checkout.stripe.com/c/pay/{request.checkout_session_id}",
        }

    @staticmethod
    def _json_response(response: Any, stage: str) -> Mapping[str, Any]:
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code >= 400:
            raise WalletHTTPError(f"{stage} returned HTTP {status_code}", status_code)
        try:
            payload = response.json()
        except Exception as exc:
            raise WalletHTTPError(f"{stage} returned invalid JSON", status_code) from exc
        if not isinstance(payload, Mapping):
            raise WalletHTTPError(f"{stage} returned a non-object JSON payload", status_code)
        return payload

    @staticmethod
    def _validate_redirect_url(url: str, method: str) -> None:
        try:
            parsed = urlsplit(str(url or "").strip())
        except Exception as exc:
            raise WalletProviderError(
                "wallet redirect URL is invalid",
                error_code="wallet_redirect_invalid",
                error_stage="follow_redirect",
            ) from exc
        host = str(parsed.hostname or "").lower().rstrip(".")
        spec = WALLET_METHODS.get(str(method or "").lower())
        allowed = _BASE_REDIRECT_HOSTS | frozenset(spec.redirect_hosts if spec else ())
        if parsed.scheme.lower() != "https" or parsed.username or parsed.password or not any(
            host == suffix or host.endswith(f".{suffix}") for suffix in allowed
        ):
            raise WalletProviderError(
                "wallet redirect host is not allowed",
                error_code="wallet_redirect_host_not_allowed",
                error_stage="follow_redirect",
            )


def _html_redirect(body: str) -> str:
    match = _HTML_REDIRECT_RE.search(str(body or "").replace("&amp;", "&"))
    return match.group(1) if match else ""


def _url_fingerprint(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    return f"{parsed.scheme.lower()}://{str(parsed.hostname or '').lower()}{parsed.path}?{parsed.query}"


__all__ = ["ChatGPTStripeWalletTransport", "WalletHTTPError"]
