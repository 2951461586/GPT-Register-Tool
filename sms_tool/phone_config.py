"""Configuration and API-key resolution for the phone-reuse provider registry.

Split out of :mod:`sms_tool.phone_reuse` so the pool/lifecycle modules do not
have to know how a provider key is spelled. Everything here is pure config
reading: no network, no pool state.

The **single** decision point for "where does this provider's key come from" is
:func:`_key_lookup`; :func:`_resolve_secret` takes the value from it and
:func:`provider_key_status` reads the *origin* from it, so the two can never
disagree.

``phone_reuse`` re-exports every public name here for back-compat.
"""

from __future__ import annotations

import os

from . import sms_providers
from .config import CFG


def _phone_reuse_cfg():
    cfg = CFG.get("phone_reuse") if isinstance(CFG.get("phone_reuse"), dict) else {}
    return cfg


def _int_value(value, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _provider_cfg(cfg: dict, provider: str) -> dict:
    """The ``phone_reuse.<provider>`` sub-section, or an empty dict."""
    section = cfg.get(provider)
    return section if isinstance(section, dict) else {}


def _send_cooldown_seconds(cfg: dict | None = None) -> int:
    cfg = cfg if isinstance(cfg, dict) else _phone_reuse_cfg()
    return _int_value(cfg.get("send_cooldown_seconds"), 45)


def _send_retry_attempts(cfg: dict | None = None) -> int:
    cfg = cfg if isinstance(cfg, dict) else _phone_reuse_cfg()
    return max(1, _int_value(cfg.get("send_retry_attempts"), 3))


def _send_retry_delay_seconds(cfg: dict | None = None) -> int:
    cfg = cfg if isinstance(cfg, dict) else _phone_reuse_cfg()
    return max(0, _int_value(cfg.get("send_retry_delay_seconds"), 45))


def _number_attempts(cfg: dict | None = None, provider: str | None = None) -> int:
    cfg = cfg if isinstance(cfg, dict) else _phone_reuse_cfg()
    provider_cfg = _provider_cfg(cfg, provider or _phone_source(cfg))
    return max(1, _int_value(provider_cfg.get("number_attempts") or cfg.get("number_attempts"), 3))


def _phone_source(cfg: dict | None = None) -> str:
    """Canonical provider key selected by ``phone_reuse.source``.

    Unset, unknown, and removed-static-pool values all resolve to
    :data:`sms_providers.DEFAULT_PROVIDER`; :func:`phone_reuse_source_error` is
    what turns a removed or unknown spelling into a loud failure.
    """
    cfg = cfg if isinstance(cfg, dict) else _phone_reuse_cfg()
    return sms_providers.resolve_provider(cfg.get("source") or cfg.get("mode"))


#: Where a resolved provider key came from, as reported by
#: :func:`provider_key_status`. ``KEY_ORIGIN_ENV`` covers all three indirection
#: spellings (blank, ``$NAME`` and ``YOUR_NAME``), because they all end up as an
#: environment lookup -- the distinction that matters to an operator is
#: "I wrote this into the config" vs "it came from the environment".
KEY_ORIGIN_CONFIG = "config"
KEY_ORIGIN_ENV = "env"
KEY_ORIGIN_MISSING = "missing"


def _key_lookup(value: str, provider: str) -> tuple[str, str]:
    """Split a configured ``api_key`` into ``(env_name, literal)``.

    Exactly one side is non-empty: either the value names an environment
    variable -- directly as ``$ENV_NAME``, by being the vendor placeholder
    ``YOUR_ENV_NAME``, or by being blank, which means the provider's own
    variable -- or it is a literal key.

    This is the **single** place that decision is made. :func:`_resolve_secret`
    takes the value from it and :func:`provider_key_status` reads the *origin*
    from it, so the two can never disagree about where a key came from.
    """
    raw = str(value or "").strip()
    env_name = sms_providers.api_key_env(provider)
    if raw.startswith("$") and len(raw) > 1:
        return raw[1:], ""
    if not raw or raw == f"YOUR_{env_name}":
        return env_name, ""
    return "", raw


def _resolve_secret(value: str, provider: str) -> str:
    """Resolve a configured provider key, honouring indirection.

    ``api_key`` may be a literal, ``$ENV_NAME``, or a vendor placeholder such as
    ``YOUR_SMSBOWER_API_KEY``. The latter two read the provider's environment
    variable, so a committed config never has to carry a real key.
    """
    env_name, literal = _key_lookup(value, provider)
    if literal:
        return literal
    return os.environ.get(env_name, "").strip()


def _provider_api_key(cfg: dict, provider: str) -> str:
    """Resolve the selected provider's own key."""
    return _resolve_secret(_provider_cfg(cfg, provider).get("api_key") or "", provider)


def phone_reuse_source_error(raw: object) -> str:
    """Operator-facing error for a removed or unknown ``phone_reuse.source``.

    Returns ``""`` when the value is usable. Callers that must fail loudly ask
    this *before* coercing with :func:`_phone_source`, because coercion alone
    cannot tell "unset" from "stale" -- both land on the default provider.
    """
    reason = sms_providers.removed_source_reason(raw)
    if reason:
        return reason
    if str(raw or "").strip() and not sms_providers.normalize_provider(raw):
        known = ", ".join(sms_providers.available_provider_keys())
        return f"unknown phone_reuse.source {str(raw).strip()!r}; expected one of: {known}"
    return ""


def has_phone_reuse_config() -> bool:
    """True when the selected provider has a usable key configured."""
    cfg = _phone_reuse_cfg()
    return bool(_provider_api_key(cfg, _phone_source(cfg)))


def _key_origin(value: str, provider: str) -> str:
    """Where :func:`_resolve_secret` would take this provider's key from."""
    _env_name, literal = _key_lookup(value, provider)
    return KEY_ORIGIN_CONFIG if literal else KEY_ORIGIN_ENV


def missing_key_hint(provider: str | None = None) -> str:
    """What an operator should set so ``provider`` gets a key.

    ``provider`` defaults to whatever ``phone_reuse.source`` selects. Shared by
    the runtime ``phone_pool_unavailable`` message and ``--doctor`` so the two
    cannot tell an operator different things about the same missing key -- the
    desktop's settings comment promises this message names the key to set, and
    a hardcoded ``smsbower`` made that true only for the default provider.
    """
    key = sms_providers.resolve_provider(provider) if provider else _phone_source()
    env_name = sms_providers.api_key_env(key)
    section = sms_providers.config_section(key)
    return f"set {section}.api_key in proxy.json, or export {env_name}"


def provider_key_status(cfg: dict | None = None) -> list[dict[str, object]]:
    """Per-provider key readiness, for diagnostics that must not leak the key.

    One row per provider this repository ships a client for, in registry order:

    ``provider`` / ``label``
        Registry key and display name.
    ``selected``
        True for the one provider ``phone_reuse.source`` currently picks. Exactly
        one row carries it, so a caller never has to re-derive the selection --
        which is how the two ends would drift apart.
    ``configured``
        Whether a usable key resolved -- **not** whether the config mentions one.
        A ``$ENV_NAME`` placeholder whose variable is unset is not configured.
    ``origin``
        ``config`` (a literal in the config), ``env`` (resolved from the
        provider's environment variable, including the placeholder spellings),
        or ``missing``. This is the distinction that is otherwise invisible:
        both a literal and a working env var produce a successful run, so
        "why does this machine work and that one not" has no answer without it.
    ``env`` / ``endpoint``
        The variable to set, and the endpoint that will actually be used
        (config override, else the registry default).

    The key value itself is never returned or logged: the whole point is that
    the report can be pasted into a ticket. ``cfg`` is the ``phone_reuse``
    section; it defaults to the live config, so ``--doctor`` and the desktop
    probe share one implementation.
    """
    cfg = cfg if isinstance(cfg, dict) else _phone_reuse_cfg()
    selected_key = _phone_source(cfg)
    rows: list[dict[str, object]] = []
    for key in sms_providers.available_provider_keys():
        spec = sms_providers.provider_spec(key)
        provider_cfg = _provider_cfg(cfg, key)
        resolved = _provider_api_key(cfg, key)
        rows.append({
            "provider": key,
            "label": spec.label if spec else key,
            "selected": key == selected_key,
            "configured": bool(resolved),
            "origin": (
                _key_origin(str(provider_cfg.get("api_key") or ""), key)
                if resolved else KEY_ORIGIN_MISSING
            ),
            "env": sms_providers.api_key_env(key),
            "endpoint": (
                str(provider_cfg.get("endpoint") or "").strip()
                or sms_providers.default_endpoint(key)
            ),
        })
    return rows


def provider_key_collisions(cfg: dict | None = None) -> list[str]:
    """Providers whose *resolved* API key is byte-identical to another's.

    Returns the colliding provider keys, sorted, or ``[]`` when every configured
    key is distinct. Unconfigured providers are ignored -- two empty keys are
    "both missing", not a collision. The key values themselves are never
    returned, logged or hashed into the result: the caller gets names only.

    Why this exists
    ---------------
    ``proxy.json`` is written by more than one surface, and on the desktop the
    供应商 dropdown and the API Key box are separate controls. Measured
    2026-09-23: one pass through that dropdown stamped SMSBower's key into
    ``phone_reuse.herosms``, ``.grizzly`` and ``.nexsms``. Each vendor then
    answered with its own credential error (herosms ``401 BAD_KEY``, grizzly
    ``NO_KEY``, nexsms ``401``) and the desktop surfaced
    "无法读取 OpenAI 号码地区和价格档位" -- a message that names neither the key nor
    the section, so a stale credential read as a broken vendor.

    No single vendor account legitimately serves two of these registries (they
    are separate businesses with separate balance endpoints), so a shared key is
    always worth saying out loud. This is offline: no request is made, which is
    what lets ``--doctor`` report it on a machine with no network.
    """
    cfg = cfg if isinstance(cfg, dict) else _phone_reuse_cfg()
    by_key: dict[str, list[str]] = {}
    for key in sms_providers.available_provider_keys():
        resolved = _provider_api_key(cfg, key)
        if not resolved:
            continue
        by_key.setdefault(resolved, []).append(key)
    collided = {provider for providers in by_key.values() if len(providers) > 1 for provider in providers}
    return sorted(collided)


__all__ = [
    "KEY_ORIGIN_CONFIG",
    "KEY_ORIGIN_ENV",
    "KEY_ORIGIN_MISSING",
    "has_phone_reuse_config",
    "missing_key_hint",
    "phone_reuse_source_error",
    "provider_key_collisions",
    "provider_key_status",
]
