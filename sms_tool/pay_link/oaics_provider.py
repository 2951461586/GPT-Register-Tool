"""In-process OAICS custom-checkout provider for migrated regional methods.

Replaces the six legacy ``services/protocol-payment/`` ``cs_*`` subprocess
extractors (``ideal``, ``pix``, ``kakao``, ``blik``, ``twint``, ``momo``)
that stopped working when ChatGPT Checkout switched from Stripe-hosted
``cs_*`` payment pages to OpenAI Custom Checkout Sessions (``oaics_*``).

Each method runs in-process over the shared cross-lane payment wire kernel
(:mod:`sms_tool.payment.wire`), carrying both:

* the ``chatgpt_checkout`` / ``checkout_session_approval`` Sentinel pair, and
* ``oai-web-deployment-attestation`` (env override or opt-in page extraction).

Two OAICS settlement paths are handled automatically from the resolved session:

1. **Custom payment method** (``cpmt_*`` present in ``custom_payment_methods``):
   ``checkout/confirm`` -> ``checkout/custom_payment_method/start`` -> redirect/QR.
2. **Stripe PaymentMethod** (``stripe_type`` present in ``payment_method_types``):
   ``/v1/payment_methods`` -> ``checkout/confirm`` -> optional ``checkout/approve``
   (with ``checkout_session_approval`` Sentinel) -> redirect/QR/completion.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

from ..kernel.sanitizer import sanitize_text
from ..kernel.timeouts import CHATGPT_TIMEOUT
from ..payment import wire as payment_wire
from ..payment.checkout_contract import (
    CheckoutRequestContract,
    CheckoutSessionContract,
    StripeCapabilityEvidence,
    discount_breakdown,
    normalize_payment_method_token,
)
from ..proxy.entry import normalize_proxy_url
from ..sentinel.client import (
    CHECKOUT_SENTINEL_FLOW,
    SentinelIssueError,
    issue_sentinel_token,
)
from ..sentinel.runner import SentinelRunnerError
from ..transport import endpoints
from ..transport.auth_headers import auth_impersonate, auth_user_agent

_LOGGER = logging.getLogger(__name__)

_PAYMENTS_BASE = endpoints.CHATGPT_BACKEND_API + "/payments"
_APPROVAL_SENTINEL_FLOW = "checkout_session_approval"
_BLIK_CODE_RE = re.compile(r"^\d{6}$")
_ROUTES = {
    "checkout": "/backend-api/payments/checkout",
    "update": "/backend-api/payments/checkout/update",
    "taxes": "/backend-api/payments/checkout/taxes",
    "confirm": "/backend-api/payments/checkout/confirm",
    "approve": "/backend-api/payments/checkout/approve",
    "start": "/backend-api/payments/checkout/custom_payment_method/start",
}
_SIDE_EFFECT_STAGES = frozenset({"confirm", "approve", "start"})
_BASE_ALLOWED_HOSTS = frozenset(
    {
        "chatgpt.com",
        "pay.openai.com",
        "checkout.stripe.com",
        "hooks.stripe.com",
        "pm-redirects.stripe.com",
        "checkoutshopper-live.adyen.com",
        "checkoutshopper-test.adyen.com",
    }
)


@dataclass(frozen=True)
class OAICSMethodProfile:
    key: str
    label: str
    country: str
    currency: str
    stripe_type: str
    payment_locale: str
    artifact_kind: str
    link_type: str
    tokens: tuple[str, ...]
    billing_name: str
    billing_line1: str
    billing_city: str
    billing_state: str
    billing_postal_code: str
    redirect_hosts: tuple[str, ...]


OAICS_METHOD_PROFILES: dict[str, OAICSMethodProfile] = {
    "ideal": OAICSMethodProfile(
        key="ideal",
        label="iDEAL",
        country="NL",
        currency="EUR",
        stripe_type="ideal",
        payment_locale="nl",
        artifact_kind="url",
        link_type="ideal_oaics_redirect",
        tokens=("ideal",),
        billing_name="Jan de Vries",
        billing_line1="Damrak 1",
        billing_city="Amsterdam",
        billing_state="Noord-Holland",
        billing_postal_code="1012 LG",
        redirect_hosts=("ideal.nl", "mollie.com", "adyen.com", "stripe.com"),
    ),
    "pix": OAICSMethodProfile(
        key="pix",
        label="PIX",
        country="BR",
        currency="BRL",
        stripe_type="pix",
        payment_locale="pt-BR",
        artifact_kind="url_or_qr",
        link_type="pix_oaics_qr",
        tokens=("pix",),
        billing_name="Lucas Silva",
        billing_line1="Avenida Paulista 1000",
        billing_city="Sao Paulo",
        billing_state="SP",
        billing_postal_code="01310-100",
        redirect_hosts=("stripe.com", "adyen.com", "mercadopago.com", "pagseguro.uol.com.br"),
    ),
    "kakao": OAICSMethodProfile(
        key="kakao",
        label="Kakao Pay",
        country="KR",
        currency="KRW",
        stripe_type="kakao_pay",
        payment_locale="ko",
        artifact_kind="url",
        link_type="kakao_oaics_redirect",
        tokens=("kakao_pay", "kakaopay", "kakao"),
        billing_name="Minjun Kim",
        billing_line1="152 Teheran-ro",
        billing_city="Seoul",
        billing_state="Seoul",
        billing_postal_code="06236",
        redirect_hosts=("kakaopay.com", "kakao.com", "adyen.com", "stripe.com", "nicepay.co.kr"),
    ),
    "blik": OAICSMethodProfile(
        key="blik",
        label="BLIK",
        country="PL",
        currency="PLN",
        stripe_type="blik",
        payment_locale="pl",
        artifact_kind="completion",
        link_type="blik_oaics_completed",
        tokens=("blik",),
        billing_name="Piotr Kowalski",
        billing_line1="Marszalkowska 100",
        billing_city="Warsaw",
        billing_state="Mazowieckie",
        billing_postal_code="00-026",
        redirect_hosts=("blik.com", "przelewy24.pl", "adyen.com", "stripe.com"),
    ),
    "twint": OAICSMethodProfile(
        key="twint",
        label="TWINT",
        country="CH",
        currency="CHF",
        stripe_type="twint",
        payment_locale="de",
        artifact_kind="url",
        link_type="twint_oaics_redirect",
        tokens=("twint",),
        billing_name="Lukas Meier",
        billing_line1="Bahnhofstrasse 42",
        billing_city="Zurich",
        billing_state="ZH",
        billing_postal_code="8001",
        redirect_hosts=("twint.ch", "adyen.com", "stripe.com"),
    ),
    "momo": OAICSMethodProfile(
        key="momo",
        label="MoMo",
        country="VN",
        currency="VND",
        stripe_type="momo",
        payment_locale="vi",
        artifact_kind="url_or_qr",
        link_type="momo_oaics_qr",
        tokens=("momo", "momo_qr", "momo_wallet"),
        billing_name="Nguyen Van A",
        billing_line1="72 Le Thanh Ton",
        billing_city="Ho Chi Minh City",
        billing_state="Ho Chi Minh",
        billing_postal_code="700000",
        redirect_hosts=("momo.vn", "payment.momo.vn", "test-payment.momo.vn", "adyen.com", "stripe.com"),
    ),
}


@dataclass(frozen=True)
class OAICSTransportRequest:
    stage: str
    method: str
    contract: CheckoutRequestContract
    checkout_session_id: str = ""
    processor_entity: str = ""
    publishable_key: str = ""
    access_token: str = field(default="", repr=False)
    payload: Mapping[str, Any] = field(default_factory=dict, repr=False)
    auth_context: Mapping[str, Any] = field(default_factory=dict, repr=False)
    transport_context: Mapping[str, Any] = field(default_factory=dict, repr=False)


class OAICSProviderTransport(Protocol):
    def create_checkout(self, request: OAICSTransportRequest) -> Mapping[str, Any]: ...
    def update_checkout(self, request: OAICSTransportRequest) -> Mapping[str, Any]: ...
    def update_taxes(self, request: OAICSTransportRequest) -> Mapping[str, Any]: ...
    def resolve_checkout(self, request: OAICSTransportRequest) -> Mapping[str, Any]: ...
    def create_payment_method(self, request: OAICSTransportRequest) -> Mapping[str, Any]: ...
    def confirm_checkout(self, request: OAICSTransportRequest) -> Mapping[str, Any]: ...
    def approve_checkout(self, request: OAICSTransportRequest) -> Mapping[str, Any]: ...
    def start_custom_payment(self, request: OAICSTransportRequest) -> Mapping[str, Any]: ...


class OAICSProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "oaics_provider_failed",
        error_stage: str = "oaics",
        retryable: bool = False,
        status: str = "failed",
    ) -> None:
        self.error_code = error_code
        self.error_stage = error_stage
        self.retryable = bool(retryable)
        self.status = status if status in {"failed", "cancelled", "timed_out", "unknown"} else "unknown"
        super().__init__(sanitize_text(message))


@dataclass(frozen=True)
class OAICSCapability:
    method: str
    method_available: bool | None
    custom_payment_method_type_id: str
    standard_method_available: bool
    amount_minor: int | None
    currency: str
    subtotal_minor: int | None
    discount_minor: int | None
    declared_total_minor: int | None
    methods_observed: tuple[str, ...]

    @property
    def is_zero_due(self) -> bool:
        if self.amount_minor == 0:
            return True
        return bool(
            self.amount_minor is None
            and self.subtotal_minor
            and self.discount_minor
            and self.discount_minor >= self.subtotal_minor
            and (self.declared_total_minor or 0) == 0
        )


def _walk_mappings(value: Any):
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from _walk_mappings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_mappings(item)


def inspect_oaics_state(
    profile: OAICSMethodProfile,
    *payloads: Mapping[str, Any],
    configured_custom_type_id: str = "",
) -> OAICSCapability:
    """Inspect OAICS checkout/update/resolve payloads for method availability and zero-due state."""
    token_set = {normalize_payment_method_token(tok) for tok in profile.tokens}
    token_set.add(normalize_payment_method_token(profile.stripe_type))
    custom_type_id = ""
    standard_available = False
    custom_available = False
    saw_method_list = False
    observed: list[str] = []
    amount_minor: int | None = None
    currency = profile.currency
    subtotal_minor: int | None = None
    discount_minor: int | None = None
    declared_total: int | None = None

    for payload in payloads:
        if not isinstance(payload, Mapping) or not payload:
            continue
        evidence = StripeCapabilityEvidence.from_payload(dict(payload), fallback_currency=profile.currency)
        if evidence.payment_method_types:
            saw_method_list = True
            for method_token in evidence.payment_method_types:
                if method_token not in observed:
                    observed.append(method_token)
                if method_token in token_set:
                    standard_available = True
        if evidence.amount_minor is not None:
            amount_minor = evidence.amount_minor
        if evidence.currency:
            currency = evidence.currency
        sub, disc, total = discount_breakdown(dict(payload))
        if sub is not None:
            subtotal_minor = sub
        if disc is not None:
            discount_minor = disc
        if total is not None:
            declared_total = total
        for item in _walk_mappings(payload):
            raw_custom = item.get("custom_payment_methods") or item.get("customPaymentMethods")
            if isinstance(raw_custom, list):
                saw_method_list = True
            label = normalize_payment_method_token(
                item.get("display_name") or item.get("name") or item.get("type") or ""
            )
            candidate = str(
                item.get("custom_payment_method_type_id")
                or item.get("payment_method_type_id")
                or item.get("type")
                or item.get("id")
                or ""
            ).strip()
            if candidate.startswith("cpmt_") and (
                label in token_set or any(tok in str(item).lower() for tok in token_set)
            ):
                custom_available = True
                custom_type_id = candidate
                if label and label not in observed:
                    observed.append(label)

    available: bool | None
    if custom_available or standard_available:
        available = True
    elif saw_method_list:
        available = False
    else:
        available = None
    resolved_custom_id = custom_type_id or str(configured_custom_type_id or "").strip()
    return OAICSCapability(
        method=profile.key,
        method_available=available,
        custom_payment_method_type_id=resolved_custom_id,
        standard_method_available=standard_available,
        amount_minor=amount_minor,
        currency=currency,
        subtotal_minor=subtotal_minor,
        discount_minor=discount_minor,
        declared_total_minor=declared_total,
        methods_observed=tuple(observed),
    )


def _account_id(access_token: str, auth_context: Mapping[str, Any]) -> str:
    direct = str(auth_context.get("account_id") or auth_context.get("chatgpt_account_id") or "").strip()
    if direct:
        return direct
    for container_key in ("account", "auth_session"):
        container = auth_context.get(container_key)
        if isinstance(container, Mapping):
            nested = container.get("account") if container_key == "auth_session" else container
            if isinstance(nested, Mapping):
                found = str(nested.get("id") or nested.get("account_id") or "").strip()
                if found:
                    return found
    parts = str(access_token or "").split(".")
    if len(parts) < 2:
        return ""
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, json.JSONDecodeError):
        return ""
    auth = claims.get("https://api.openai.com/auth") if isinstance(claims, Mapping) else None
    if isinstance(auth, Mapping):
        return str(auth.get("chatgpt_account_id") or auth.get("account_id") or "").strip()
    return ""


def _cookie_device_id(cookie_header: str) -> str:
    for part in str(cookie_header or "").split(";"):
        name, _, value = part.partition("=")
        if name.strip().lower() == "oai-did":
            cleaned = value.strip().strip('"')
            if cleaned:
                return cleaned
    return ""


def _stage_proxy(request: OAICSTransportRequest) -> str:
    plan = request.transport_context.get("payment_route_plan")
    if plan is not None and hasattr(plan, "proxy_for"):
        canonical_stage = {
            "update": "promotion",
            "taxes": "stripe_init",
            "resolve": "stripe_init",
            "start": "redirect",
        }.get(request.stage, request.stage)
        planned = str(plan.proxy_for(canonical_stage) or "").strip()
        if planned:
            return normalize_proxy_url(planned)
    stage_keys = {
        "checkout": ("checkout_proxy",),
        "update": ("promotion_proxy", "update_proxy"),
        "taxes": ("provider_proxy", "checkout_proxy"),
        "resolve": ("provider_proxy", "checkout_proxy"),
        "payment_method": ("payment_method_proxy", "provider_proxy", "checkout_proxy"),
        "confirm": ("confirm_proxy", "provider_proxy", "checkout_proxy"),
        "approve": ("approve_proxy", "confirm_proxy", "checkout_proxy"),
        "start": ("redirect_proxy", "provider_proxy", "checkout_proxy"),
    }.get(request.stage, ())
    for key in (*stage_keys, "default_proxy", "proxy"):
        value: Any = request.transport_context.get(key)
        if isinstance(value, Mapping):
            value = value.get("https") or value.get("http") or ""
        if str(value or "").strip():
            return normalize_proxy_url(str(value).strip())
    return ""


class ChatGPTOAICSTransport:
    """Production OAICS transport backed by :mod:`sms_tool.payment.wire`."""

    def __init__(self, *, timeout: int = 60, allow_html_attestation: bool | None = None) -> None:
        self.timeout = max(5, int(timeout or 60))
        self.allow_html_attestation = allow_html_attestation
        self._checkout_so_token = ""

    def create_checkout(self, request: OAICSTransportRequest) -> Mapping[str, Any]:
        return self._post(request, endpoints.CHATGPT_CHECKOUT_API, _ROUTES["checkout"])

    def update_checkout(self, request: OAICSTransportRequest) -> Mapping[str, Any]:
        return self._post(request, endpoints.CHATGPT_CHECKOUT_UPDATE, _ROUTES["update"])

    def update_taxes(self, request: OAICSTransportRequest) -> Mapping[str, Any]:
        return self._post(request, endpoints.CHATGPT_CHECKOUT_TAXES, _ROUTES["taxes"])

    def resolve_checkout(self, request: OAICSTransportRequest) -> Mapping[str, Any]:
        route = f"/backend-api/payments/checkout/{request.processor_entity}/{request.checkout_session_id}"
        url = f"{_PAYMENTS_BASE}/checkout/{request.processor_entity}/{request.checkout_session_id}"
        cookie_header = str(request.auth_context.get("cookie_header") or "")
        proxy = _stage_proxy(request)
        try:
            response = payment_wire._checkout_get(
                url,
                request.access_token,
                cookie_header,
                proxy,
                self.timeout,
                extra_headers=self._headers(request, route),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise OAICSProviderError(
                f"resolve transport failed ({type(exc).__name__}): {sanitize_text(exc)}",
                error_code=f"{request.method}_resolve_transport_failed",
                error_stage="resolve",
                retryable=True,
                status="failed",
            ) from exc
        return self._json_response(response, request.method, "resolve")

    def create_payment_method(self, request: OAICSTransportRequest) -> Mapping[str, Any]:
        proxy = _stage_proxy(request)
        session = payment_wire._new_session(proxy)
        try:
            response = session.post(
                endpoints.STRIPE_PAYMENT_METHODS,
                data=dict(request.payload),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": endpoints.STRIPE_CHECKOUT_BASE,
                    "Referer": f"{endpoints.STRIPE_CHECKOUT_BASE}/c/pay/{request.checkout_session_id}",
                    "User-Agent": auth_user_agent(),
                },
                timeout=self.timeout,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise OAICSProviderError(
                f"payment_method transport failed ({type(exc).__name__}): {sanitize_text(exc)}",
                error_code=f"{request.method}_payment_method_transport_failed",
                error_stage="payment_method",
                retryable=True,
                status="failed",
            ) from exc
        finally:
            close_fn = getattr(session, "close", None)
            if callable(close_fn):
                close_fn()
        return self._json_response(response, request.method, "payment_method")

    def confirm_checkout(self, request: OAICSTransportRequest) -> Mapping[str, Any]:
        return self._post(request, f"{_PAYMENTS_BASE}/checkout/confirm", _ROUTES["confirm"])

    def approve_checkout(self, request: OAICSTransportRequest) -> Mapping[str, Any]:
        extra = self._headers(request, _ROUTES["approve"])
        extra.update(self._approval_sentinel_headers(request))
        return self._post(request, endpoints.CHATGPT_CHECKOUT_APPROVE, _ROUTES["approve"], extra_override=extra)

    def start_custom_payment(self, request: OAICSTransportRequest) -> Mapping[str, Any]:
        return self._post(request, f"{_PAYMENTS_BASE}/checkout/custom_payment_method/start", _ROUTES["start"])

    def _approval_sentinel_headers(self, request: OAICSTransportRequest) -> dict[str, str]:
        device_id = self._device_id(request)
        cookie_header = str(request.auth_context.get("cookie_header") or "")
        proxy = _stage_proxy(request)
        headers: dict[str, str] = {}
        try:
            issued = issue_sentinel_token(
                flow=_APPROVAL_SENTINEL_FLOW,
                device_id=device_id,
                proxy=proxy,
                cookie_header=cookie_header,
                timeout_seconds=min(self.timeout, CHATGPT_TIMEOUT),
            )
            if issued.token:
                headers["openai-sentinel-token"] = issued.token
            so_token = self._checkout_so_token or issued.so_token
            if not so_token:
                checkout_issued = issue_sentinel_token(
                    flow=CHECKOUT_SENTINEL_FLOW,
                    device_id=device_id,
                    proxy=proxy,
                    cookie_header=cookie_header,
                    timeout_seconds=min(self.timeout, CHATGPT_TIMEOUT),
                )
                so_token = checkout_issued.so_token
                if so_token:
                    self._checkout_so_token = so_token
            if so_token:
                headers["openai-sentinel-so-token"] = so_token
        except (SentinelIssueError, SentinelRunnerError, OSError, RuntimeError, ValueError) as exc:
            _LOGGER.debug("oaics approval sentinel unavailable for %s: %s", request.method, sanitize_text(exc))
        return headers

    def _post(
        self,
        request: OAICSTransportRequest,
        url: str,
        route: str,
        *,
        extra_override: dict[str, str] | None = None,
    ) -> Mapping[str, Any]:
        cookie_header = str(request.auth_context.get("cookie_header") or "")
        proxy = _stage_proxy(request)
        headers = extra_override if extra_override is not None else self._headers(request, route)
        attestation = payment_wire._checkout_attestation_headers(
            cookie_header=cookie_header,
            extra_headers=headers,
            proxy=proxy,
            allow_html_fetch=self.allow_html_attestation,
        )
        headers.update(attestation)
        try:
            response = payment_wire._checkout_post(
                url,
                dict(request.payload),
                request.access_token,
                cookie_header,
                proxy,
                self.timeout,
                extra_headers=headers,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            uncertain = request.stage in _SIDE_EFFECT_STAGES
            raise OAICSProviderError(
                f"{request.stage} transport failed ({type(exc).__name__}): {sanitize_text(exc)}",
                error_code=f"{request.method}_{request.stage}_transport_failed",
                error_stage=request.stage,
                retryable=not uncertain,
                status="unknown" if uncertain else "failed",
            ) from exc
        return self._json_response(response, request.method, request.stage)

    @staticmethod
    def _device_id(request: OAICSTransportRequest) -> str:
        cookie_header = str(request.auth_context.get("cookie_header") or "")
        return str(
            request.auth_context.get("device_id")
            or request.auth_context.get("oai_did")
            or _cookie_device_id(cookie_header)
            or ""
        ).strip() or str(uuid.uuid4())

    def _headers(self, request: OAICSTransportRequest, route: str) -> dict[str, str]:
        device_id = self._device_id(request)
        referer = (
            f"{endpoints.CHATGPT_BASE}/checkout/{request.processor_entity}/{request.checkout_session_id}"
            if request.checkout_session_id and request.processor_entity
            else f"{endpoints.CHATGPT_BASE}/?promo_campaign={request.contract.promo_campaign_id}"
        )
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": endpoints.CHATGPT_BASE,
            "Referer": referer,
            "OAI-Device-Id": device_id,
            "OAI-Language": request.contract.browser_locale,
            "x-openai-target-path": route,
            "x-openai-target-route": route,
        }
        account_id = _account_id(request.access_token, request.auth_context)
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        explicit_attestation = str(
            request.auth_context.get("web_deployment_attestation")
            or request.transport_context.get("web_deployment_attestation")
            or ""
        ).strip()
        if explicit_attestation:
            headers["oai-web-deployment-attestation"] = explicit_attestation
        return headers

    @staticmethod
    def _json_response(response: Any, method: str, stage: str) -> Mapping[str, Any]:
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code >= 400:
            uncertain = stage in _SIDE_EFFECT_STAGES
            detail = _error_detail(response)
            raise OAICSProviderError(
                f"{stage} returned HTTP {status_code}{detail}",
                error_code=f"{method}_{stage}_http_error",
                error_stage=stage,
                retryable=not uncertain and (status_code in {408, 425, 429} or status_code >= 500),
                status="unknown" if uncertain else "failed",
            )
        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            uncertain = stage in _SIDE_EFFECT_STAGES
            raise OAICSProviderError(
                f"{stage} returned invalid JSON",
                error_code=f"{method}_{stage}_bad_json",
                error_stage=stage,
                retryable=not uncertain,
                status="unknown" if uncertain else "failed",
            ) from exc
        if not isinstance(payload, Mapping):
            raise OAICSProviderError(
                f"{stage} returned a non-object JSON payload",
                error_code=f"{method}_{stage}_bad_json",
                error_stage=stage,
                retryable=False,
                status="unknown" if stage in _SIDE_EFFECT_STAGES else "failed",
            )
        return payload


def _error_detail(response: Any) -> str:
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, Mapping):
        parts = [sanitize_text(payload.get(k)) for k in ("detail", "error", "message", "code") if payload.get(k)]
        text = " | ".join(part for part in parts if part)
    else:
        text = sanitize_text(getattr(response, "text", "") or "")
    text = " ".join(str(text).split())
    return (": " + text[:200]) if text else ""


def _build_tax_payload(
    profile: OAICSMethodProfile, contract: CheckoutRequestContract, session: CheckoutSessionContract
) -> dict[str, Any]:
    return {
        "checkout_session_id": session.checkout_session_id,
        "checkout_email": "buyer@example.com",
        "billing_country": contract.billing_country,
        "billing_name": profile.billing_name,
        "currency": contract.currency,
        "tax_id": None,
        "processor_entity": session.processor_entity,
        "billing_address": {
            "country": contract.billing_country,
            "line1": profile.billing_line1,
            "line2": "",
            "city": profile.billing_city,
            "state": profile.billing_state,
            "postal_code": profile.billing_postal_code,
        },
    }


def _build_pm_payload(
    profile: OAICSMethodProfile,
    contract: CheckoutRequestContract,
    session: CheckoutSessionContract,
    *,
    blik_code: str = "",
) -> dict[str, str]:
    payload: dict[str, str] = {
        "type": profile.stripe_type,
        "billing_details[name]": profile.billing_name,
        "billing_details[email]": "buyer@example.com",
        "billing_details[address][country]": contract.billing_country,
        "billing_details[address][city]": profile.billing_city,
        "billing_details[address][line1]": profile.billing_line1,
        "billing_details[address][postal_code]": profile.billing_postal_code,
        "key": session.publishable_key,
    }
    if profile.key == "blik" and blik_code:
        payload["blik[code]"] = blik_code
    return payload


def _extract_artifact(profile: OAICSMethodProfile, *payloads: Mapping[str, Any]) -> tuple[str, str, bool]:
    """Return ``(redirect_url, qr_data, completed)`` from OAICS confirm/approve/start responses."""
    redirect_url = ""
    qr_data = ""
    completed = False
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        status = str(payload.get("status") or "").strip().lower()
        if status in {"succeeded", "complete", "completed", "paid"}:
            completed = True
        for item in _walk_mappings(payload):
            if not redirect_url:
                for key in ("redirect_url", "hosted_voucher_url", "hosted_instructions_url", "pay_url", "url"):
                    candidate = str(item.get(key) or "").strip()
                    if candidate.startswith("https://"):
                        redirect_url = candidate
                        break
            if not qr_data:
                for key in ("qr_data", "qr_code_url", "pix_qr_code", "data", "image_url_png"):
                    candidate = str(item.get(key) or "").strip()
                    if candidate and (key != "data" or candidate.startswith(("000201", "https://", "data:image"))):
                        qr_data = candidate
                        break
    return redirect_url, qr_data, completed


def _validate_artifact_url(url: str, profile: OAICSMethodProfile) -> None:
    if not url:
        return
    parsed = urlsplit(url)
    host = str(parsed.hostname or "").lower().rstrip(".")
    allowed = _BASE_ALLOWED_HOSTS | frozenset(profile.redirect_hosts)
    if (
        parsed.scheme.lower() != "https"
        or parsed.username
        or parsed.password
        or not any(host == suffix or host.endswith("." + suffix) for suffix in allowed)
    ):
        raise OAICSProviderError(
            f"{profile.label} redirect host is not allowed",
            error_code=f"{profile.key}_redirect_host_not_allowed",
            error_stage="artifact",
            retryable=False,
            status="unknown",
        )


def _capability_summary(capability: OAICSCapability, *, require_zero: bool) -> dict[str, Any]:
    eligible: bool | None = capability.method_available
    decision = "payment_method_available" if eligible else "payment_method_unavailable"
    if eligible and require_zero and not capability.is_zero_due:
        if capability.amount_minor is None:
            eligible, decision = None, "checkout_amount_unknown"
        else:
            eligible, decision = False, "nonzero_offer"
    classification = "eligible" if eligible is True else "ineligible" if eligible is False else "unknown"
    return {
        "ok": classification != "unknown",
        "status": "completed" if classification != "unknown" else "unknown",
        "classification": classification,
        "decision": decision,
        "eligible": eligible,
        "method_available": capability.method_available,
        "conclusive": classification != "unknown",
        "amount": capability.amount_minor,
        "currency": capability.currency,
        "subtotal": capability.subtotal_minor,
        "discount": capability.discount_minor,
        "payment_method_types": list(capability.methods_observed),
        "custom_payment_method_type_id": capability.custom_payment_method_type_id,
        "session_type": "oaics",
        "offer_state": (
            "zero_due"
            if capability.is_zero_due
            else "nonzero_due"
            if capability.amount_minor is not None
            else "unknown_amount"
        ),
    }


def _make_req(
    stage: str,
    profile: OAICSMethodProfile,
    contract: CheckoutRequestContract,
    token: str,
    payload: Mapping[str, Any],
    auth_context: Mapping[str, Any],
    transport_context: Mapping[str, Any],
    session: CheckoutSessionContract | None = None,
) -> OAICSTransportRequest:
    return OAICSTransportRequest(
        stage=stage,
        method=profile.key,
        contract=contract,
        checkout_session_id=session.checkout_session_id if session else "",
        processor_entity=session.processor_entity if session else "",
        publishable_key=session.publishable_key if session else "",
        access_token=token,
        payload=payload,
        auth_context=auth_context,
        transport_context=transport_context,
    )


def _settle_oaics_session(
    profile: OAICSMethodProfile,
    contract: CheckoutRequestContract,
    session: CheckoutSessionContract,
    capability: OAICSCapability,
    token: str,
    transport: OAICSProviderTransport,
    auth_context: Mapping[str, Any],
    wire_context: Mapping[str, Any],
    *,
    blik_code: str = "",
) -> tuple[str, str, bool]:
    """Execute custom-payment-method or Stripe PaymentMethod confirmation & approval."""
    if capability.custom_payment_method_type_id.startswith("cpmt_"):
        confirm_resp = transport.confirm_checkout(
            _make_req(
                "confirm",
                profile,
                contract,
                token,
                {
                    "checkout_session_id": session.checkout_session_id,
                    "processor_entity": session.processor_entity,
                    "type": "custom_payment_method",
                    "selected_payment_method_type": capability.custom_payment_method_type_id,
                },
                auth_context,
                wire_context,
                session,
            )
        )
        start_resp = transport.start_custom_payment(
            _make_req(
                "start",
                profile,
                contract,
                token,
                {
                    "checkout_session_id": session.checkout_session_id,
                    "custom_payment_method_type_id": capability.custom_payment_method_type_id,
                },
                auth_context,
                wire_context,
                session,
            )
        )
        return _extract_artifact(profile, start_resp, confirm_resp)

    pm_resp = transport.create_payment_method(
        _make_req(
            "payment_method",
            profile,
            contract,
            token,
            _build_pm_payload(profile, contract, session, blik_code=blik_code),
            auth_context,
            wire_context,
            session,
        )
    )
    pm_id = str(pm_resp.get("id") or "").strip()
    if not pm_id.startswith("pm_"):
        raise OAICSProviderError(
            f"{profile.label} payment_method response omitted pm_ id",
            error_code=f"{profile.key}_payment_method_id_missing",
            error_stage="payment_method",
            retryable=False,
        )
    confirm_body: dict[str, Any] = {
        "checkout_session_id": session.checkout_session_id,
        "processor_entity": session.processor_entity,
        "payment_method_id": pm_id,
        "type": profile.stripe_type,
    }
    if profile.key == "blik" and blik_code:
        confirm_body["blik_code"] = blik_code
    confirm_resp = transport.confirm_checkout(
        _make_req("confirm", profile, contract, token, confirm_body, auth_context, wire_context, session)
    )
    url, qr_data, completed = _extract_artifact(profile, confirm_resp)
    if url or qr_data or (profile.key == "blik" and completed):
        return url, qr_data, completed
    approve_resp = transport.approve_checkout(
        _make_req(
            "approve",
            profile,
            contract,
            token,
            {
                "checkout_session_id": session.checkout_session_id,
                "processor_entity": session.processor_entity,
            },
            auth_context,
            wire_context,
            session,
        )
    )
    return _extract_artifact(profile, approve_resp, confirm_resp)


def _prepare_oaics_session(
    profile: OAICSMethodProfile,
    contract: CheckoutRequestContract,
    token: str,
    transport: OAICSProviderTransport,
    ctx: Mapping[str, Any],
    wire_ctx: Mapping[str, Any],
    *,
    configured_custom_type_id: str,
) -> tuple[CheckoutSessionContract, OAICSCapability, list[dict[str, str]]]:
    """Create, promo-update, tax-update, and resolve one OAICS checkout session."""
    warnings: list[dict[str, str]] = []
    checkout_payload = transport.create_checkout(
        _make_req("checkout", profile, contract, token, contract.checkout_payload(), ctx, wire_ctx)
    )
    session = CheckoutSessionContract.from_payload(dict(checkout_payload), billing_country=contract.billing_country)

    update_resp: Mapping[str, Any] = {}
    try:
        update_resp = transport.update_checkout(
            _make_req(
                "update",
                profile,
                contract,
                token,
                {
                    "checkout_session_id": session.checkout_session_id,
                    "processor_entity": session.processor_entity,
                    "plan_name": contract.plan_name,
                    "price_interval": "month",
                    "seat_quantity": 1,
                    "promo_campaign": {
                        "promo_campaign_id": contract.promo_campaign_id,
                        "is_coupon_from_query_param": False,
                    },
                },
                ctx,
                wire_ctx,
                session,
            )
        )
    except OAICSProviderError as exc:
        warnings.append({"stage": "update", "error": sanitize_text(exc)})

    try:
        transport.update_taxes(
            _make_req(
                "taxes",
                profile,
                contract,
                token,
                _build_tax_payload(profile, contract, session),
                ctx,
                wire_ctx,
                session,
            )
        )
    except OAICSProviderError as exc:
        warnings.append({"stage": "taxes", "error": sanitize_text(exc)})

    resolved = transport.resolve_checkout(_make_req("resolve", profile, contract, token, {}, ctx, wire_ctx, session))
    capability = inspect_oaics_state(
        profile,
        checkout_payload,
        update_resp,
        resolved,
        configured_custom_type_id=configured_custom_type_id,
    )
    return session, capability, warnings


def _validate_oaics_preconditions(
    method: str,
    access_token: str,
    *,
    probe_only: bool,
    blik_code: str,
) -> tuple[OAICSMethodProfile | None, str, str, dict[str, Any] | None]:
    key = str(method or "").strip().lower().replace("-", "_")
    profile = OAICS_METHOD_PROFILES.get(key)
    if profile is None:
        return (
            None,
            "",
            "",
            {
                "ok": False,
                "status": "failed",
                "payment_method": key,
                "error": f"unsupported OAICS payment method: {method}",
                "error_code": "oaics_method_unsupported",
                "error_stage": "validation",
                "retryable": False,
            },
        )
    token = str(access_token or "").strip()
    if not token:
        return (
            profile,
            "",
            "",
            {
                "ok": False,
                "status": "failed",
                "payment_method": profile.key,
                "error": "access_token is required",
                "error_code": "missing_access_token",
                "error_stage": "validation",
                "retryable": False,
            },
        )
    cleaned_blik = str(blik_code or "").strip()
    if profile.key == "blik" and not probe_only and not _BLIK_CODE_RE.fullmatch(cleaned_blik):
        return (
            profile,
            token,
            "",
            {
                "ok": False,
                "status": "failed",
                "payment_method": profile.key,
                "error": "BLIK requires an explicit 6-digit code",
                "error_code": "blik_code_required",
                "error_stage": "validation",
                "retryable": False,
            },
        )
    return profile, token, cleaned_blik, None


def run_oaics_provider(
    method: str,
    access_token: str,
    transport: OAICSProviderTransport,
    *,
    probe_only: bool = False,
    auth_context: Mapping[str, Any] | None = None,
    transport_context: Mapping[str, Any] | None = None,
    billing_country: str = "",
    custom_payment_method_type_id: str = "",
    blik_code: str = "",
    require_zero: bool = True,
) -> dict[str, Any]:
    """Run the typed in-process OAICS custom-checkout flow for one migrated method."""
    profile, token, cleaned_blik, failure = _validate_oaics_preconditions(
        method, access_token, probe_only=probe_only, blik_code=blik_code
    )
    if failure is not None or profile is None:
        return failure or {"ok": False, "status": "failed", "error_code": "oaics_method_unsupported"}

    warnings: list[dict[str, str]] = []
    try:
        contract = CheckoutRequestContract.for_payment_method(
            profile.key,
            billing_country=str(billing_country or profile.country).strip().upper(),
        )
        ctx = dict(auth_context or {})
        ctx.setdefault("device_id", str(uuid.uuid4()))
        wire_ctx = dict(transport_context or {})

        session, capability, warnings = _prepare_oaics_session(
            profile,
            contract,
            token,
            transport,
            ctx,
            wire_ctx,
            configured_custom_type_id=custom_payment_method_type_id,
        )
        probe = _capability_summary(capability, require_zero=require_zero)
        if probe_only:
            return {
                **probe,
                "payment_method": profile.key,
                "operation": "payment_method_capability_probe",
                "cs_id": session.checkout_session_id,
                "processor_entity": session.processor_entity,
                "warnings": warnings,
                "error_stage": "" if probe["conclusive"] else "capability_classification",
                "error_code": "" if probe["conclusive"] else str(probe["decision"]),
                "error": "" if probe["conclusive"] else str(probe["decision"]),
                "retryable": not probe["conclusive"],
            }
        if capability.method_available is False:
            raise OAICSProviderError(
                f"{profile.label} is not offered by this OAICS checkout",
                error_code="payment_method_unavailable",
                error_stage="resolve",
            )
        if capability.method_available is None:
            raise OAICSProviderError(
                f"{profile.label} capability evidence is inconclusive",
                error_code=f"{profile.key}_capability_unknown",
                error_stage="resolve",
                status="unknown",
            )
        if require_zero and not capability.is_zero_due:
            code = "checkout_amount_unknown" if capability.amount_minor is None else "nonzero_offer"
            raise OAICSProviderError(
                code,
                error_code=code,
                error_stage="resolve",
                status="unknown" if capability.amount_minor is None else "failed",
            )

        url, qr_data, completed = _settle_oaics_session(
            profile,
            contract,
            session,
            capability,
            token,
            transport,
            ctx,
            wire_ctx,
            blik_code=cleaned_blik,
        )
        _validate_artifact_url(url, profile)
        if not url and not qr_data and not (profile.key == "blik" and completed):
            raise OAICSProviderError(
                f"{profile.label} OAICS flow completed without a redirect URL or QR artifact",
                error_code=f"{profile.key}_artifact_missing",
                error_stage="artifact",
                status="unknown",
            )
        result: dict[str, Any] = {
            "ok": True,
            "status": "completed",
            "operation": "execute_payment" if profile.key == "blik" else "extract_link",
            "payment_method": profile.key,
            "link_type": profile.link_type,
            "url": url,
            "provider_redirect_url": url,
            "cs_id": session.checkout_session_id,
            "processor_entity": session.processor_entity,
            "amount": capability.amount_minor,
            "currency": capability.currency,
            "session_type": "oaics",
            "capability": probe,
            "warnings": warnings,
            "retryable": False,
            "error_stage": "",
            "impersonate": auth_impersonate(),
        }
        if qr_data:
            result["qr_data"] = qr_data
        return result
    except OAICSProviderError as exc:
        return {
            "ok": False,
            "status": exc.status,
            "payment_method": profile.key,
            "error": sanitize_text(exc),
            "error_code": exc.error_code,
            "error_stage": exc.error_stage,
            "retryable": exc.retryable,
            "warnings": warnings,
        }


__all__ = [
    "ChatGPTOAICSTransport",
    "OAICSCapability",
    "OAICSMethodProfile",
    "OAICSProviderError",
    "OAICSProviderTransport",
    "OAICSTransportRequest",
    "OAICS_METHOD_PROFILES",
    "inspect_oaics_state",
    "run_oaics_provider",
]
