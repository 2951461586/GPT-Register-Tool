"""Versioned payment-method catalog shared by Python and the desktop app."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


CATALOG_SCHEMA = "payment_methods.v1"

_COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2}$")


def _country_code_tuple(value: Any, *, owner: str) -> tuple[str, ...]:
    """Normalize an optional country-code allowlist from the catalog JSON.

    Entries may be plain ISO codes or ``{"code": "JP", "label": ...}`` objects
    (the desktop display shape); only the code is kept.  Codes are uppercased
    and must be 2-letter ISO country codes; anything else is a catalog error
    naming the owning method id (or the top-level default).
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"payment catalog country list for {owner} must be an array")
    codes: list[str] = []
    for item in value:
        raw_code = item.get("code") if isinstance(item, Mapping) else item
        code = str(raw_code or "").strip().upper()
        if not _COUNTRY_CODE_RE.fullmatch(code):
            raise ValueError(f"invalid country code {item!r} in payment catalog for {owner}")
        codes.append(code)
    return tuple(dict.fromkeys(codes))


@dataclass(frozen=True)
class PaymentMethodDefinition:
    key: str
    label: str
    registration_label: str
    country: str
    currency: str
    adapter: str
    stripe_type: str = ""
    payment_locale: str = "en"
    flow_profile: str = "protocol_redirect"
    stages: tuple[str, ...] = ("auth_gate", "checkout", "artifact")
    artifact_kind: str = "url"
    artifact_validator: str = "http_url"
    probe_output_kind: str = "capability"
    reconciliation_policy: str = "artifact"
    side_effect_stage: str = "confirm"
    provider: str = ""
    redirect_hosts: tuple[str, ...] = ()
    transport: str = ""
    release_tier: str = "production"
    script: str = ""
    aliases: tuple[str, ...] = ()
    batch_enabled: bool = True
    registration_enabled: bool = True
    # Optional stage-country allowlists.  A non-empty per-method list overrides
    # the top-level catalog default; when neither is present these are empty.
    checkout_countries: tuple[str, ...] = ()
    approve_countries: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaymentMethodCatalog:
    schema: str
    default_method: str
    methods: Mapping[str, PaymentMethodDefinition]
    aliases: Mapping[str, str]
    checkout_countries: tuple[str, ...] = ()
    approve_countries: tuple[str, ...] = ()

    def normalize(self, value: Any, *, default_for_blank: bool = True) -> str:
        raw = str(value or "").strip().lower().replace(" ", "_")
        if not raw:
            return self.default_method if default_for_blank else ""
        normalized = self.aliases.get(raw, raw)
        return normalized if normalized in self.methods else ""


def catalog_path() -> Path:
    return Path(__file__).resolve().parent.parent / "payment_methods.json"


@lru_cache(maxsize=1)
def load_payment_catalog(path: str | Path | None = None) -> PaymentMethodCatalog:
    source = Path(path).resolve() if path else catalog_path()
    raw = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict) or raw.get("schema") != CATALOG_SCHEMA:
        raise ValueError(f"unsupported payment catalog schema: {raw.get('schema') if isinstance(raw, dict) else ''}")
    entries = raw.get("methods")
    if not isinstance(entries, list) or not entries:
        raise ValueError("payment catalog methods must be a non-empty array")
    default_checkout_countries = _country_code_tuple(
        raw.get("checkout_countries"), owner="catalog default checkout_countries"
    )
    default_approve_countries = _country_code_tuple(
        raw.get("approve_countries"), owner="catalog default approve_countries"
    )
    methods: dict[str, PaymentMethodDefinition] = {}
    aliases: dict[str, str] = {}
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            raise ValueError(f"payment catalog methods[{index}] must be an object")
        key = str(item.get("id") or "").strip().lower()
        if not key or key in methods:
            raise ValueError(f"invalid or duplicate payment method id: {key}")
        method_checkout_countries = _country_code_tuple(item.get("checkout_countries"), owner=f"payment method {key}")
        method_approve_countries = _country_code_tuple(item.get("approve_countries"), owner=f"payment method {key}")
        definition = PaymentMethodDefinition(
            key=key,
            label=str(item.get("display_name") or key),
            registration_label=str(item.get("registration_display_name") or item.get("display_name") or key),
            country=str(item.get("country") or "").upper(),
            currency=str(item.get("currency") or "").upper(),
            adapter=str(item.get("adapter") or "").strip(),
            stripe_type=str(item.get("stripe_type") or key).strip().lower(),
            payment_locale=str(item.get("payment_locale") or "en").strip(),
            flow_profile=str(item.get("flow_profile") or "protocol_redirect").strip(),
            stages=tuple(
                str(stage or "").strip().lower()
                for stage in item.get("stages") or ("auth_gate", "checkout", "artifact")
            ),
            artifact_kind=str(item.get("artifact_kind") or "url").strip(),
            artifact_validator=str(item.get("artifact_validator") or "http_url").strip().lower(),
            probe_output_kind=str(item.get("probe_output_kind") or "capability").strip().lower(),
            reconciliation_policy=str(item.get("reconciliation_policy") or "artifact").strip().lower(),
            side_effect_stage=str(item.get("side_effect_stage") or "confirm").strip(),
            provider=str(item.get("provider") or "").strip(),
            redirect_hosts=tuple(str(host or "").strip().lower() for host in item.get("redirect_hosts") or ()),
            transport=str(item.get("transport") or "").strip(),
            release_tier=str(
                item.get("release_tier")
                or ("offline" if str(item.get("adapter") or "").strip() == "regional_wallet" else "production")
            ).strip().lower(),
            script=str(item.get("script") or "").strip(),
            aliases=tuple(str(alias).strip().lower().replace(" ", "_") for alias in item.get("aliases") or ()),
            batch_enabled=bool(item.get("batch_enabled", True)),
            registration_enabled=bool(item.get("registration_enabled", True)),
            checkout_countries=method_checkout_countries or default_checkout_countries,
            approve_countries=method_approve_countries or default_approve_countries,
        )
        if (
            len(definition.country) != 2
            or len(definition.currency) != 3
            or not definition.adapter
            or not definition.stripe_type
            or not definition.stages
            or definition.artifact_kind not in {"url", "url_or_qr", "completion"}
            or definition.artifact_validator not in {"http_url", "paypal_ba_url", "url_or_qr", "completion", "provider_redirect", "checkout_url"}
            or definition.probe_output_kind not in {"capability", "availability", "provider_redirect"}
            or definition.reconciliation_policy not in {"artifact", "paypal_return", "provider_status", "none"}
            or definition.release_tier not in {"production", "canary", "offline"}
        ):
            raise ValueError(f"invalid payment catalog definition: {key}")
        methods[key] = definition
        aliases[key] = key
        for alias in definition.aliases:
            if alias in aliases and aliases[alias] != key:
                raise ValueError(f"duplicate payment method alias: {alias}")
            aliases[alias] = key
    default_method = str(raw.get("default_method") or "").strip().lower()
    if default_method not in methods:
        raise ValueError(f"payment catalog default method is invalid: {default_method}")
    return PaymentMethodCatalog(
        schema=CATALOG_SCHEMA,
        default_method=default_method,
        methods=MappingProxyType(methods),
        aliases=MappingProxyType(aliases),
        checkout_countries=default_checkout_countries,
        approve_countries=default_approve_countries,
    )


PAYMENT_CATALOG = load_payment_catalog()
PAYMENT_METHODS = PAYMENT_CATALOG.methods


def normalize_payment_method(value: Any, *, default_for_blank: bool = True) -> str:
    return PAYMENT_CATALOG.normalize(value, default_for_blank=default_for_blank)


def validate_catalog_consistency(*, adapter_methods: set[str] | None = None) -> None:
    """Fail fast when the canonical catalog drifts from executable contracts."""
    from .checkout_contract import PAYMENT_METHOD_PROFILES
    from .payment_flow import FLOW_PROFILES

    errors: list[str] = []
    for key, definition in PAYMENT_CATALOG.methods.items():
        profile = PAYMENT_METHOD_PROFILES.get(key)
        if profile is None:
            errors.append(f"{key}: missing checkout contract profile")
        else:
            if profile.country != definition.country or profile.currency != definition.currency:
                errors.append(f"{key}: catalog country/currency disagrees with checkout contract")
            if definition.stripe_type != profile.stripe_type:
                errors.append(f"{key}: catalog stripe_type disagrees with checkout contract")
        flow = FLOW_PROFILES.get(key)
        if flow is None:
            errors.append(f"{key}: missing payment flow profile")
        elif definition.stages and tuple(definition.stages) != tuple(flow.stages):
            errors.append(f"{key}: catalog stages disagree with payment flow profile")
        if adapter_methods is not None and key not in adapter_methods:
            errors.append(f"{key}: missing registered payment adapter")
    if errors:
        raise ValueError("payment catalog consistency failure: " + "; ".join(errors))
