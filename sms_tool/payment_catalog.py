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
            script=str(item.get("script") or "").strip(),
            aliases=tuple(str(alias).strip().lower().replace(" ", "_") for alias in item.get("aliases") or ()),
            batch_enabled=bool(item.get("batch_enabled", True)),
            registration_enabled=bool(item.get("registration_enabled", True)),
            checkout_countries=method_checkout_countries or default_checkout_countries,
            approve_countries=method_approve_countries or default_approve_countries,
        )
        if len(definition.country) != 2 or len(definition.currency) != 3 or not definition.adapter:
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
