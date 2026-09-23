"""Registry of SMS-receiving providers this tool can rent numbers from.

Single source of truth
----------------------
The provider vocabulary used to be spread across five unrelated places that
had no compiler connection between them:

  * the ``--phone-source`` argparse ``choices`` in ``cli_parsers/core.py``,
  * the alias table inside ``phone_reuse._phone_source``,
  * the ``SMSBOWER_API_KEY`` env lookup in ``phone_reuse._resolve_secret``,
  * the hardcoded ``DefaultEndpoint`` in ``SmsWorkbench/SmsProviderCatalogClient.cs``,
  * the settings rows in ``SmsWorkbench/SettingsCatalog.cs``.

Adding a provider meant editing all of them, and the C# half had no mechanical
link to the Python half at all. They now derive from :data:`PROVIDERS` below.
``tests/test_sms_provider_registry.py`` pins this module, and
``tests/test_settings_catalog_provider_parity.py`` pins the C# mirror
(``SmsWorkbench/SmsProviderCatalog.cs``) against it.

Protocol families
-----------------
``sms_activate_handler`` is the ``/stubs/handler_api.php`` protocol (GET with an
``action`` query parameter) that ``sms_tool.smsbower`` already implements.
Vendors that speak it are drop-in: only the host changes, which is why
``SmsBowerClient`` takes ``endpoint`` per instance.

``nexsms_json`` is a *different* family and does **not** fit the adapter above.
It answers a JSON envelope (``{code, message, data}``) on plain REST paths, and
its lifecycle is keyed on the **rented phone number** rather than on an
activation id -- there is no ``activationId`` and no ``setStatus``, so
"ready for another code" and "activation complete" have no wire representation
at all. ``sms_tool.nexsms`` implements it. Because the two families disagree on
what the lifecycle key *is*, callers must not assume ``activation_id`` is the
handle they pass to a client; see ``PhoneSlot.rental_handle``.

``unverified`` marks a vendor whose protocol has not been established -- those
carry ``client_available=False`` so no surface offers them as a working choice.
:data:`IMPLEMENTED_PROTOCOLS` is what makes that flag checkable rather than
merely asserted: a spec may only be ``client_available`` when its protocol is
in that set.

Scope note
----------
**No credentials live here.** ``default_endpoint`` values are public API hosts
taken from each vendor's own documentation and ``api_key_env`` names the
environment variable the operator supplies their *own* key in. Nothing in this
module is a secret, and no vendor key is bundled with the repository.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PROTOCOL_SMS_ACTIVATE = "sms_activate_handler"
PROTOCOL_NEXSMS = "nexsms_json"
#: Reserved for a vendor whose protocol has not been established. Currently
#: unused -- ``nexsms`` was the last entry in this state and its protocol is now
#: confirmed -- but kept because that is the state a *new* vendor starts in.
PROTOCOL_UNVERIFIED = "unverified"

#: Protocols this repository ships a client for. A spec may only claim
#: ``client_available`` when its ``protocol`` is in here; that is what
#: ``tests/test_sms_provider_registry.py`` pins, so "we offer it" can never drift
#: away from "we can actually talk to it".
IMPLEMENTED_PROTOCOLS: frozenset[str] = frozenset({PROTOCOL_SMS_ACTIVATE, PROTOCOL_NEXSMS})

#: Provider used when ``phone_reuse.source`` is absent or unrecognised.
DEFAULT_PROVIDER = "smsbower"


@dataclass(frozen=True)
class SmsProviderSpec:
    """Metadata for one rentable-number provider, the single source of truth."""

    key: str
    label: str
    # Every spelling that resolves to this provider. Consumed by
    # :func:`normalize_provider` and, via :data:`KNOWN_PROVIDER_ALIASES`, by the
    # config validators.
    aliases: frozenset[str] = field(default_factory=frozenset)
    protocol: str = PROTOCOL_SMS_ACTIVATE
    # Public API host. Empty when the vendor's protocol is unverified.
    default_endpoint: str = ""
    # Environment variable the operator supplies their own key in.
    api_key_env: str = ""
    docs_url: str = ""
    # False => this repository ships no client for the vendor's protocol, so no
    # CLI choice, settings row, or dialog may offer it. Must equal
    # :attr:`has_client`; the registry test pins that.
    client_available: bool = True
    note: str = ""

    @property
    def has_client(self) -> bool:
        """True when a client for :attr:`protocol` ships with this repository."""
        return self.protocol in IMPLEMENTED_PROTOCOLS

    @property
    def config_section(self) -> str:
        """``phone_reuse`` sub-section holding this provider's settings."""
        return f"phone_reuse.{self.key}"

    @property
    def api_key_path(self) -> str:
        return f"{self.config_section}.api_key"

    @property
    def endpoint_path(self) -> str:
        return f"{self.config_section}.endpoint"

    @property
    def speaks_sms_activate(self) -> bool:
        return self.protocol == PROTOCOL_SMS_ACTIVATE

    @property
    def speaks_nexsms(self) -> bool:
        return self.protocol == PROTOCOL_NEXSMS

    @property
    def is_rental(self) -> bool:
        """True when the vendor rents a number whose lifecycle we can drive.

        Both implemented families rent, so this is currently the same as
        :attr:`has_client`; it exists as the predicate call sites should use, so
        that a future non-rental family does not silently become rentable.
        """
        return self.has_client and self.protocol in {
            PROTOCOL_SMS_ACTIVATE,
            PROTOCOL_NEXSMS,
        }


# Canonical provider registry. This is the only place the provider vocabulary,
# its aliases, and its endpoints are declared. Per-provider settings live under
# ``phone_reuse.<key>``, which keeps the existing ``phone_reuse.smsbower.*``
# block valid -- adding a provider needs no config migration.
PROVIDERS: dict[str, SmsProviderSpec] = {
    "smsbower": SmsProviderSpec(
        "smsbower",
        "SMSBower",
        # "platform"/"provider" are legacy generic spellings that always meant
        # this vendor; kept so existing configs keep resolving.
        frozenset({"smsbower", "sms_bower", "sms-bower", "platform", "provider"}),
        default_endpoint="https://smsbower.page/stubs/handler_api.php",
        api_key_env="SMSBOWER_API_KEY",
    ),
    "herosms": SmsProviderSpec(
        "herosms",
        "HeroSMS",
        frozenset({"herosms", "hero_sms", "hero-sms", "hero"}),
        default_endpoint="https://hero-sms.com/stubs/handler_api.php",
        api_key_env="HEROSMS_API_KEY",
        docs_url="https://www.hero-sms.xyz/api",
        note="SMS-Activate compatible; serves getNumberV2/getStatus/setStatus.",
    ),
    "grizzly": SmsProviderSpec(
        "grizzly",
        "Grizzly SMS",
        frozenset({"grizzly", "grizzlysms", "grizzly_sms", "grizzly-sms"}),
        default_endpoint="https://api.grizzlysms.com/stubs/handler_api.php",
        api_key_env="GRIZZLY_API_KEY",
        docs_url="https://api.grizzlysms.com/docs/client",
        note="SMS-Activate compatible; multiplexes every operation through action.",
    ),
    "nexsms": SmsProviderSpec(
        "nexsms",
        "NexSMS",
        frozenset({"nexsms", "nex_sms", "nex-sms"}),
        protocol=PROTOCOL_NEXSMS,
        # Base host only: this family's paths are ``/api/...`` and each call
        # appends its own, so the endpoint must NOT carry a handler path the way
        # the sms-activate vendors' ``/stubs/handler_api.php`` does.
        default_endpoint="https://api.nexsms.net",
        api_key_env="NEXSMS_API_KEY",
        docs_url="https://doc.nexsms.net/index-en.html",
        note=(
            "JSON REST family, not sms-activate: GET/POST with apiKey in the "
            "query string, a {code, message, data} envelope, and a lifecycle "
            "keyed on the phone number (no activationId, no setStatus). "
            "Service codes and country ids follow the sms-activate numbering "
            "(OpenAI = dr), so those are shared with the other vendors."
        ),
    ),
}

#: Spellings of the removed static phone-pool mode. They used to select a list of
#: ``{phone, sms_api_url}`` entries instead of a rental provider. Kept here so a
#: stale config fails with an explanation rather than silently renting numbers.
REMOVED_SOURCE_VALUES: dict[str, str] = {
    "phone_pool": "the static phone pool was removed; set phone_reuse.source to a provider",
    "static": "the static phone pool was removed; set phone_reuse.source to a provider",
    "legacy": "the static phone pool was removed; set phone_reuse.source to a provider",
    "sms_link": "the static phone pool was removed; set phone_reuse.source to a provider",
    "link": "the static phone pool was removed; set phone_reuse.source to a provider",
}


def _build_alias_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for key, spec in PROVIDERS.items():
        index[key] = key
        for alias in spec.aliases:
            index[alias] = key
    return index


#: alias -> canonical key. Includes every canonical key.
KNOWN_PROVIDER_ALIASES: dict[str, str] = _build_alias_index()


def _normalize_token(value: object) -> str:
    return str(value or "").strip().lower()


def normalize_provider(value: object) -> str:
    """Canonical provider key for ``value``, or ``""`` when unrecognised."""
    return KNOWN_PROVIDER_ALIASES.get(_normalize_token(value), "")


def resolve_provider(value: object, default: str = DEFAULT_PROVIDER) -> str:
    """Canonical provider key, falling back to ``default`` when unrecognised.

    A removed static-pool value also falls back: callers that must *reject* it
    rather than coerce should ask :func:`is_removed_source_value` first.
    """
    return normalize_provider(value) or default


def is_removed_source_value(value: object) -> bool:
    """True when ``value`` names the removed static phone-pool mode."""
    return _normalize_token(value) in REMOVED_SOURCE_VALUES


def removed_source_reason(value: object) -> str:
    """Operator-facing explanation for a removed source value, else ``""``."""
    return REMOVED_SOURCE_VALUES.get(_normalize_token(value), "")


def provider_spec(key: object) -> SmsProviderSpec | None:
    """Spec for ``key`` (canonical or alias), or ``None`` when unknown."""
    canonical = normalize_provider(key)
    return PROVIDERS.get(canonical) if canonical else None


def provider_keys() -> tuple[str, ...]:
    """Every declared provider key, in declaration order."""
    return tuple(PROVIDERS)


def available_provider_keys() -> tuple[str, ...]:
    """Providers this repository can actually talk to, in declaration order."""
    return tuple(key for key, spec in PROVIDERS.items() if spec.client_available)


def default_endpoint(key: object, fallback: str = "") -> str:
    """Public API host for ``key``, or ``fallback`` when unknown/unset."""
    spec = provider_spec(key)
    return spec.default_endpoint if spec and spec.default_endpoint else fallback


def api_key_env(key: object) -> str:
    """Environment variable name holding the operator's own key for ``key``."""
    spec = provider_spec(key)
    return spec.api_key_env if spec else ""


def config_section(key: object) -> str:
    """``phone_reuse.<key>`` for a known provider, else ``""``."""
    spec = provider_spec(key)
    return spec.config_section if spec else ""


def provider_labels() -> dict[str, str]:
    """``{key: label}`` for every available provider, in declaration order."""
    return {key: spec.label for key, spec in PROVIDERS.items() if spec.client_available}
