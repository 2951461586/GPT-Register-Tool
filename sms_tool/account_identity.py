"""Stable per-account network and protocol identity.

The public interface allocates identity once during registration, persists only
safe affinity metadata, reconstructs the configured proxy later, and rebinds
thread-local fingerprint state at every worker entry.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urlsplit

from .fingerprint_pool import shared_fingerprint_pool
from .phone_proxy import normalize_proxy_url
from .proxy_entry import infer_region, rebuild_proxy_credentials


IDENTITY_VERSION = 1
_USER_SID_RE = re.compile(r"(?<=-sid-)[A-Za-z0-9]+(?=-t-|-|$)")
_KOOKEEY_PASSWORD_RE = re.compile(
    r"^(?P<base>.+?)-(?P<cc>[A-Za-z]{2})-(?P<sid>[A-Za-z0-9]+)-(?P<ttl>\d+[smhd])$"
)


def create_registration_identity(
    proxy: str | None,
    *,
    pool_index: int = -1,
    fingerprint_key: str = "",
    device_id: str = "",
    auth_session_logging_id: str = "",
    account_key: str = "",
    browser_identity: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Allocate the canonical identity persisted for one account."""
    key = _canonical_fingerprint_key(fingerprint_key)
    if not key:
        key = _canonical_fingerprint_key(shared_fingerprint_pool(config).next(proxy).name)
    identity: dict[str, Any] = {
        "version": IDENTITY_VERSION,
        "proxy_affinity": _proxy_affinity(proxy, pool_index=pool_index),
        "fingerprint_key": key,
        "device_id": str(device_id or "").strip(),
        "auth_session_logging_id": str(auth_session_logging_id or "").strip(),
        "account_key": str(account_key or "").strip(),
    }
    if browser_identity:
        identity["browser_identity"] = dict(browser_identity)
    return identity


def complete_registration_identity(
    identity: Mapping[str, Any] | None,
    *,
    device_id: str = "",
    auth_session_logging_id: str = "",
) -> dict[str, Any]:
    """Attach device values discovered inside a registration driver."""
    value = dict(identity or {})
    value.setdefault("version", IDENTITY_VERSION)
    value["proxy_affinity"] = dict(value.get("proxy_affinity") or {})
    value["fingerprint_key"] = _canonical_fingerprint_key(value.get("fingerprint_key"))
    if device_id:
        value["device_id"] = str(device_id).strip()
    else:
        value.setdefault("device_id", "")
    if auth_session_logging_id:
        value["auth_session_logging_id"] = str(auth_session_logging_id).strip()
    else:
        value.setdefault("auth_session_logging_id", "")
    value.setdefault("account_key", "")
    return value


def account_identity(account: Mapping[str, Any] | None) -> dict[str, Any]:
    """Read the canonical context, with compatibility for earlier sessions."""
    value = account if isinstance(account, Mapping) else {}
    stored = value.get("identity_context")
    identity = dict(stored) if isinstance(stored, Mapping) else {}
    identity.setdefault("version", IDENTITY_VERSION)
    identity["proxy_affinity"] = dict(identity.get("proxy_affinity") or {})
    identity["fingerprint_key"] = _canonical_fingerprint_key(
        identity.get("fingerprint_key")
        or value.get("auth_fingerprint_profile")
        or value.get("fingerprint_key")
    )
    identity["device_id"] = str(identity.get("device_id") or value.get("device_id") or "").strip()
    identity["auth_session_logging_id"] = str(
        identity.get("auth_session_logging_id")
        or value.get("auth_session_logging_id")
        or ""
    ).strip()
    identity.setdefault("account_key", str(value.get("account_key") or value.get("email") or "").strip())
    if identity.get("browser_identity") is None and value.get("browser_identity"):
        identity["browser_identity"] = dict(value["browser_identity"])
    return identity


def bind_account_identity(identity_or_account: Mapping[str, Any] | None) -> dict[str, Any]:
    """Bind an account identity to the current worker thread."""
    from .auth_headers import (
        set_auth_fingerprint,
        set_fingerprint_device,
        set_fingerprint_geo,
    )

    identity = account_identity(identity_or_account)
    key = identity.get("fingerprint_key")
    if key:
        set_auth_fingerprint(str(key))
    affinity = identity.get("proxy_affinity") or {}
    country = str(affinity.get("country") or "").strip().upper()
    if country:
        set_fingerprint_geo(country)
    device_id = str(identity.get("device_id") or "").strip()
    if device_id:
        set_fingerprint_device(device_id)
    return identity


def resolve_account_proxy(
    account: Mapping[str, Any] | None,
    *,
    fallback_proxy: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> str | None:
    """Resolve a saved account's original proxy; fallback is legacy-only."""
    identity = account_identity(account)
    affinity = identity.get("proxy_affinity") or {}
    if affinity:
        base = _configured_affinity_base(affinity, config)
        restored = _restore_session(base, affinity) if base else ""
        if restored:
            return restored
    normalized = normalize_proxy_url(fallback_proxy)
    return normalized or None


def _proxy_affinity(proxy: str | None, *, pool_index: int) -> dict[str, Any]:
    normalized = normalize_proxy_url(proxy)
    if not normalized:
        return {}
    parsed = urlsplit(normalized)
    session_kind, session_id = _session_parts(parsed)
    return {
        "pool_index": int(pool_index) if int(pool_index) >= 0 else -1,
        "scheme": str(parsed.scheme or "http").lower(),
        "host": str(parsed.hostname or "").lower(),
        "port": int(parsed.port or 0),
        "country": infer_region(normalized),
        "session_kind": session_kind,
        "session_id": session_id,
    }


def _configured_affinity_base(
    affinity: Mapping[str, Any],
    config: Mapping[str, Any] | None,
) -> str:
    proxy_cfg = config.get("proxy", {}) if isinstance(config, Mapping) else {}
    if not isinstance(proxy_cfg, Mapping):
        proxy_cfg = {}
    configured = proxy_cfg.get("pool") or []
    if isinstance(configured, str):
        configured = [
            item.strip()
            for item in configured.replace(";", "\n").replace(",", "\n").splitlines()
            if item.strip()
        ]
    candidates = list(configured) if isinstance(configured, (list, tuple)) else []
    for key in ("registration", "default"):
        if proxy_cfg.get(key):
            candidates.append(proxy_cfg[key])

    index = _as_int(affinity.get("pool_index"), -1)
    if 0 <= index < len(candidates):
        candidate = normalize_proxy_url(candidates[index])
        if _endpoint_matches(candidate, affinity):
            return candidate
    for raw in candidates:
        candidate = normalize_proxy_url(raw)
        if _endpoint_matches(candidate, affinity):
            return candidate
    return ""


def _endpoint_matches(proxy: str, affinity: Mapping[str, Any]) -> bool:
    if not proxy:
        return False
    parsed = urlsplit(proxy)
    return (
        str(parsed.scheme or "http").lower() == str(affinity.get("scheme") or "http").lower()
        and str(parsed.hostname or "").lower() == str(affinity.get("host") or "").lower()
        and int(parsed.port or 0) == _as_int(affinity.get("port"), 0)
    )


def _session_parts(parsed: Any) -> tuple[str, str]:
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    match = _USER_SID_RE.search(username)
    if match:
        return "username_sid", match.group(0)
    match = _KOOKEEY_PASSWORD_RE.match(password)
    if match:
        return "password_sid", match.group("sid")
    return "", ""


def _restore_session(base_proxy: str, affinity: Mapping[str, Any]) -> str:
    if not base_proxy:
        return ""
    session_id = str(affinity.get("session_id") or "").strip()
    session_kind = str(affinity.get("session_kind") or "").strip()
    if not session_id or not session_kind:
        return base_proxy
    parsed = urlsplit(base_proxy)
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if session_kind == "username_sid":
        username, count = _USER_SID_RE.subn(session_id, username, count=1)
        return rebuild_proxy_credentials(parsed, username, password) if count else ""
    if session_kind == "password_sid":
        match = _KOOKEEY_PASSWORD_RE.match(password)
        if not match:
            return ""
        password = (
            f"{match.group('base')}-{match.group('cc')}-"
            f"{session_id}-{match.group('ttl')}"
        )
        return rebuild_proxy_credentials(parsed, username, password)
    return ""


def _canonical_fingerprint_key(value: Any) -> str:
    key = str(value or "").strip().lower().split("_", 1)[0]
    try:
        from .auth_headers import AUTH_FINGERPRINT_PROFILES

        return key if key in AUTH_FINGERPRINT_PROFILES else ""
    except Exception:
        return ""


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "account_identity",
    "bind_account_identity",
    "complete_registration_identity",
    "create_registration_identity",
    "resolve_account_proxy",
]
