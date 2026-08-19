"""Independent, offline-verifiable adapters for regional payment methods.

The adapters own only typed contract construction and response validation.  A
caller injects the transport, so capability probes never create a payment
method or confirm a payment and provider-specific behavior remains isolated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

from .checkout_contract import CheckoutContractError, CheckoutRequestContract, StripeCapabilityEvidence


@dataclass(frozen=True)
class RegionalPaymentProfile:
    key: str
    stripe_type: str
    country: str
    currency: str
    provider: str
    redirect_hosts: tuple[str, ...]
    artifact_kind: str = "redirect"


REGIONAL_PAYMENT_PROFILES: dict[str, RegionalPaymentProfile] = {
    "qris": RegionalPaymentProfile(
        "qris", "gopay", "ID", "IDR", "midtrans", ("midtrans.com", "gopay.co.id"), "url_or_qr"
    ),
    "bizum": RegionalPaymentProfile(
        "bizum", "bizum", "ES", "EUR", "bizum", ("bizum.es", "stripe.com"), "redirect"
    ),
    "naver_pay": RegionalPaymentProfile(
        "naver_pay", "naver_pay", "KR", "KRW", "naver_nicepay", ("pay.naver.com", "naver.com", "nicepay.co.kr"), "redirect"
    ),
}


class RegionalPaymentTransport(Protocol):
    def create_checkout(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def stripe_init(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def create_payment_method(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def confirm_payment(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def follow_redirect(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


class RegionalPaymentError(RuntimeError):
    def __init__(self, message: str, *, error_code: str, error_stage: str, retryable: bool, status: str = "failed") -> None:
        self.error_code = error_code
        self.error_stage = error_stage
        self.retryable = retryable
        self.status = status
        super().__init__(message)


def regional_profile(method: str) -> RegionalPaymentProfile:
    key = str(method or "").strip().lower().replace("-", "_")
    try:
        return REGIONAL_PAYMENT_PROFILES[key]
    except KeyError as exc:
        raise RegionalPaymentError(
            f"unsupported regional payment method: {method}",
            error_code="regional_method_unsupported",
            error_stage="validation",
            retryable=False,
        ) from exc


def build_regional_payment_method_payload(
    profile: RegionalPaymentProfile,
    *,
    checkout_session_id: str,
    publishable_key: str,
    billing_country: str,
    billing_details: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    session = str(checkout_session_id or "").strip()
    key = str(publishable_key or "").strip()
    country = str(billing_country or profile.country).strip().upper()
    if not session or not key.startswith("pk_"):
        raise RegionalPaymentError(
            "regional payment method payload is missing checkout session or publishable key",
            error_code="regional_payment_method_context_missing",
            error_stage="payment_method",
            retryable=True,
        )
    if country != profile.country:
        raise RegionalPaymentError(
            f"{profile.key} requires billing country {profile.country}",
            error_code="regional_country_mismatch",
            error_stage="validation",
            retryable=False,
        )
    details = dict(billing_details or {})
    return {
        "type": profile.stripe_type,
        "billing_details[country]": country,
        "billing_details[email]": str(details.get("email") or "").strip(),
        "client_attribution_metadata[checkout_session_id]": session,
        "key": key,
    }


def validate_provider_redirect(profile: RegionalPaymentProfile, value: str) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"https"} or not host:
        raise RegionalPaymentError(
            "provider redirect must be an HTTPS URL",
            error_code="regional_redirect_invalid",
            error_stage="redirect",
            retryable=False,
        )
    if not any(host == allowed or host.endswith("." + allowed) for allowed in profile.redirect_hosts):
        raise RegionalPaymentError(
            f"unexpected {profile.provider} redirect host",
            error_code="regional_redirect_host_invalid",
            error_stage="redirect",
            retryable=False,
        )
    return url


class RegionalPaymentAdapter:
    def __init__(self, profile: RegionalPaymentProfile, transport: RegionalPaymentTransport) -> None:
        self.profile = profile
        self.transport = transport

    def run(
        self,
        *,
        access_token: str,
        billing_country: str = "",
        billing_details: Mapping[str, Any] | None = None,
        checkout_request: Mapping[str, Any] | None = None,
        probe_only: bool = False,
        progress: Any = None,
    ) -> dict[str, Any]:
        if not str(access_token or "").strip():
            raise RegionalPaymentError(
                "access_token is required",
                error_code="auth_missing",
                error_stage="auth_gate",
                retryable=False,
            )
        country = str(billing_country or self.profile.country).strip().upper()
        if country != self.profile.country:
            raise RegionalPaymentError(
                f"{self.profile.key} requires billing country {self.profile.country}",
                error_code="regional_country_mismatch",
                error_stage="validation",
                retryable=False,
            )
        base = dict(checkout_request or {})
        base.update({"access_token": access_token, "payment_method": self.profile.key, "billing_country": country})

        def emit(stage: str, status: str = "running", detail: str = "") -> None:
            if callable(progress):
                progress({"stage": stage, "status": status, "detail": detail, "method": self.profile.key, "country": country})

        try:
            emit("checkout")
            checkout = dict(self.transport.create_checkout(base) or {})
            session_id = str(checkout.get("checkout_session_id") or checkout.get("session_id") or checkout.get("id") or "").strip()
            publishable_key = str(checkout.get("publishable_key") or "").strip()
            if not session_id or not publishable_key.startswith("pk_"):
                raise RegionalPaymentError(
                    "checkout response missing session or publishable key",
                    error_code="checkout_response_invalid",
                    error_stage="checkout",
                    retryable=True,
                )
            emit("checkout", "completed")
            emit("stripe_init")
            init = dict(self.transport.stripe_init({**base, "checkout_session_id": session_id, "publishable_key": publishable_key}) or {})
            evidence = StripeCapabilityEvidence.from_payload(init, fallback_currency=self.profile.currency)
            expected = self.profile.stripe_type
            classification, eligible = evidence.classification_for(expected)
            result: dict[str, Any] = {
                "ok": eligible is True,
                "operation": "payment_method_capability_probe" if probe_only else "extract_link",
                "payment_method": self.profile.key,
                "provider": self.profile.provider,
                "capability": classification,
                "currency": evidence.currency,
                "amount": evidence.amount_minor,
                "side_effects": False if probe_only else None,
            }
            if probe_only:
                emit("stripe_init", "completed" if eligible is True else "failed", classification)
                return result
            if eligible is not True:
                emit("stripe_init", "failed", classification)
                result.update({"ok": False, "error": "payment method is not advertised by Stripe init", "error_stage": "stripe_init"})
                return result
            emit("stripe_init", "completed", classification)
            payload = build_regional_payment_method_payload(
                self.profile,
                checkout_session_id=session_id,
                publishable_key=publishable_key,
                billing_country=country,
                billing_details=billing_details,
            )
            emit("payment_method")
            payment_method = dict(self.transport.create_payment_method({**base, **payload}) or {})
            emit("payment_method", "completed")
            emit("confirm")
            confirmed = dict(self.transport.confirm_payment({**base, **payment_method, "checkout_session_id": session_id}) or {})
            redirect_value = _redirect_value(confirmed)
            if not redirect_value:
                raise RegionalPaymentError(
                    "provider redirect was not returned",
                    error_code="regional_redirect_missing",
                    error_stage="confirm",
                    retryable=False,
                    status="unknown",
                )
            redirect_url = validate_provider_redirect(self.profile, redirect_value)
            emit("confirm", "completed")
            emit("redirect")
            followed = dict(self.transport.follow_redirect({**base, "redirect_url": redirect_url}) or {})
            artifact = _redirect_value(followed) or redirect_url
            result.update({"ok": True, "url": validate_provider_redirect(self.profile, artifact), "side_effects": True, "status": "completed"})
            if self.profile.artifact_kind == "url_or_qr":
                result["qr_data"] = str(followed.get("qr_data") or confirmed.get("qr_data") or "")
            emit("redirect", "completed")
            emit("artifact", "completed")
            return result
        except RegionalPaymentError:
            raise
        except Exception as exc:
            raise RegionalPaymentError(
                "regional payment transport failed",
                error_code="regional_transport_failed",
                error_stage="transport",
                retryable=True,
            ) from exc


def _redirect_value(payload: Mapping[str, Any]) -> str:
    for key in ("redirect_url", "url", "hosted_url", "next_action_url"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = payload.get("next_action")
    if isinstance(nested, Mapping):
        return _redirect_value(nested)
    return ""
