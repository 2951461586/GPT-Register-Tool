"""Canonical stage vocabulary and flow profiles for protocol payments."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .payment_catalog import PAYMENT_METHODS as CATALOG_PAYMENT_METHODS


class PaymentStage(str, Enum):
    AUTH_GATE = "auth_gate"
    CHECKOUT = "checkout"
    PROMOTION = "promotion"
    STRIPE_INIT = "stripe_init"
    PAYMENT_METHOD = "payment_method"
    CONFIRM = "confirm"
    APPROVE = "approve"
    REDIRECT = "redirect"
    POLL = "poll"
    ARTIFACT = "artifact"


STAGE_ORDER = tuple(stage.value for stage in PaymentStage)

STAGE_ALIASES = {
    "authentication": PaymentStage.AUTH_GATE.value,
    "jit": PaymentStage.AUTH_GATE.value,
    "jit_auth": PaymentStage.AUTH_GATE.value,
    "update": PaymentStage.PROMOTION.value,
    "promotion_update": PaymentStage.PROMOTION.value,
    "provider": PaymentStage.STRIPE_INIT.value,
    "stripe": PaymentStage.STRIPE_INIT.value,
    "pm": PaymentStage.PAYMENT_METHOD.value,
    "confirmation": PaymentStage.CONFIRM.value,
    "final_review": PaymentStage.APPROVE.value,
    "follow_redirect": PaymentStage.REDIRECT.value,
    "provider_redirect": PaymentStage.REDIRECT.value,
    "extract": PaymentStage.ARTIFACT.value,
    "extracting": PaymentStage.ARTIFACT.value,
}


def normalize_payment_stage(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return ""
    return STAGE_ALIASES.get(raw, raw if raw in STAGE_ORDER else "")


@dataclass(frozen=True)
class PaymentFlowProfile:
    key: str
    stages: tuple[str, ...]
    artifact_kind: str = "url"
    side_effect_stage: str = PaymentStage.CONFIRM.value

    def includes(self, stage: Any) -> bool:
        return normalize_payment_stage(stage) in self.stages


_LEGACY_FLOW_PROFILES: dict[str, PaymentFlowProfile] = {
    "paypal": PaymentFlowProfile(
        "paypal_agreement",
        (
            PaymentStage.AUTH_GATE.value,
            PaymentStage.CHECKOUT.value,
            PaymentStage.PROMOTION.value,
            PaymentStage.STRIPE_INIT.value,
            PaymentStage.PAYMENT_METHOD.value,
            PaymentStage.CONFIRM.value,
            PaymentStage.APPROVE.value,
            PaymentStage.REDIRECT.value,
            PaymentStage.ARTIFACT.value,
        ),
    ),
    "upi": PaymentFlowProfile(
        "upi_hosted",
        (
            PaymentStage.AUTH_GATE.value,
            PaymentStage.CHECKOUT.value,
            PaymentStage.STRIPE_INIT.value,
            PaymentStage.PAYMENT_METHOD.value,
            PaymentStage.CONFIRM.value,
            PaymentStage.APPROVE.value,
            PaymentStage.POLL.value,
            PaymentStage.ARTIFACT.value,
        ),
        artifact_kind="url_or_qr",
    ),
    "gopay": PaymentFlowProfile(
        "wallet_redirect",
        (
            PaymentStage.AUTH_GATE.value,
            PaymentStage.CHECKOUT.value,
            PaymentStage.PROMOTION.value,
            PaymentStage.STRIPE_INIT.value,
            PaymentStage.PAYMENT_METHOD.value,
            PaymentStage.CONFIRM.value,
            PaymentStage.APPROVE.value,
            PaymentStage.POLL.value,
            PaymentStage.REDIRECT.value,
            PaymentStage.ARTIFACT.value,
        ),
    ),
    "qris": PaymentFlowProfile(
        "regional_wallet_redirect",
        (
            PaymentStage.AUTH_GATE.value,
            PaymentStage.CHECKOUT.value,
            PaymentStage.PROMOTION.value,
            PaymentStage.STRIPE_INIT.value,
            PaymentStage.PAYMENT_METHOD.value,
            PaymentStage.CONFIRM.value,
            PaymentStage.REDIRECT.value,
            PaymentStage.ARTIFACT.value,
        ),
        artifact_kind="url_or_qr",
    ),
    "bizum": PaymentFlowProfile(
        "regional_hosted_redirect",
        (
            PaymentStage.AUTH_GATE.value,
            PaymentStage.CHECKOUT.value,
            PaymentStage.STRIPE_INIT.value,
            PaymentStage.PAYMENT_METHOD.value,
            PaymentStage.CONFIRM.value,
            PaymentStage.REDIRECT.value,
            PaymentStage.ARTIFACT.value,
        ),
    ),
    "naver_pay": PaymentFlowProfile(
        "regional_hosted_redirect",
        (
            PaymentStage.AUTH_GATE.value,
            PaymentStage.CHECKOUT.value,
            PaymentStage.STRIPE_INIT.value,
            PaymentStage.PAYMENT_METHOD.value,
            PaymentStage.CONFIRM.value,
            PaymentStage.REDIRECT.value,
            PaymentStage.ARTIFACT.value,
        ),
    ),
    "grabpay": PaymentFlowProfile(
        "wallet_redirect",
        (
            PaymentStage.AUTH_GATE.value,
            PaymentStage.CHECKOUT.value,
            PaymentStage.STRIPE_INIT.value,
            PaymentStage.PAYMENT_METHOD.value,
            PaymentStage.CONFIRM.value,
            PaymentStage.APPROVE.value,
            PaymentStage.POLL.value,
            PaymentStage.REDIRECT.value,
            PaymentStage.ARTIFACT.value,
        ),
    ),
    "gcash": PaymentFlowProfile(
        "custom_payment_method",
        (
            PaymentStage.AUTH_GATE.value,
            PaymentStage.CHECKOUT.value,
            PaymentStage.PROMOTION.value,
            PaymentStage.STRIPE_INIT.value,
            PaymentStage.PAYMENT_METHOD.value,
            PaymentStage.CONFIRM.value,
            PaymentStage.REDIRECT.value,
            PaymentStage.ARTIFACT.value,
        ),
    ),
    "direct_card": PaymentFlowProfile(
        "checkout_artifact",
        (
            PaymentStage.AUTH_GATE.value,
            PaymentStage.CHECKOUT.value,
            PaymentStage.PROMOTION.value,
            PaymentStage.STRIPE_INIT.value,
            PaymentStage.ARTIFACT.value,
        ),
        side_effect_stage=PaymentStage.ARTIFACT.value,
    ),
    "momo": PaymentFlowProfile(
        "wallet_qr",
        (
            PaymentStage.AUTH_GATE.value,
            PaymentStage.CHECKOUT.value,
            PaymentStage.PROMOTION.value,
            PaymentStage.STRIPE_INIT.value,
            PaymentStage.PAYMENT_METHOD.value,
            PaymentStage.CONFIRM.value,
            PaymentStage.APPROVE.value,
            PaymentStage.REDIRECT.value,
            PaymentStage.ARTIFACT.value,
        ),
        artifact_kind="url_or_qr",
    ),
    "blik": PaymentFlowProfile(
        "code_execution",
        (
            PaymentStage.AUTH_GATE.value,
            PaymentStage.CHECKOUT.value,
            PaymentStage.STRIPE_INIT.value,
            PaymentStage.PAYMENT_METHOD.value,
            PaymentStage.CONFIRM.value,
            PaymentStage.ARTIFACT.value,
        ),
        artifact_kind="completion",
    ),
}

FLOW_PROFILES: dict[str, PaymentFlowProfile] = {
    key: PaymentFlowProfile(
        definition.flow_profile,
        tuple(definition.stages),
        artifact_kind=definition.artifact_kind,
        side_effect_stage=definition.side_effect_stage,
    )
    for key, definition in CATALOG_PAYMENT_METHODS.items()
}

_GENERIC_REDIRECT = PaymentFlowProfile(
    "protocol_redirect",
    (
        PaymentStage.AUTH_GATE.value,
        PaymentStage.CHECKOUT.value,
        PaymentStage.PROMOTION.value,
        PaymentStage.STRIPE_INIT.value,
        PaymentStage.PAYMENT_METHOD.value,
        PaymentStage.CONFIRM.value,
        PaymentStage.APPROVE.value,
        PaymentStage.REDIRECT.value,
        PaymentStage.ARTIFACT.value,
    ),
)


def payment_flow_profile(payment_method: Any, method_config: Mapping[str, Any] | None = None) -> PaymentFlowProfile:
    method = str(payment_method or "").strip().lower().replace("-", "_")
    configured = method_config if isinstance(method_config, Mapping) else {}
    configured_key = str(configured.get("flow_profile") or "").strip().lower()
    base = FLOW_PROFILES.get(method, _GENERIC_REDIRECT)
    raw_stages = configured.get("stages")
    if isinstance(raw_stages, (list, tuple)):
        stages = tuple(
            stage for item in raw_stages if (stage := normalize_payment_stage(item))
        )
        if stages:
            return PaymentFlowProfile(
                configured_key or base.key,
                tuple(dict.fromkeys(stages)),
                artifact_kind=base.artifact_kind,
                side_effect_stage=base.side_effect_stage,
            )
    if configured_key and configured_key != base.key:
        return PaymentFlowProfile(
            configured_key,
            base.stages,
            artifact_kind=base.artifact_kind,
            side_effect_stage=base.side_effect_stage,
        )
    return base


__all__ = [
    "FLOW_PROFILES",
    "PaymentFlowProfile",
    "PaymentStage",
    "STAGE_ALIASES",
    "STAGE_ORDER",
    "normalize_payment_stage",
    "payment_flow_profile",
]
