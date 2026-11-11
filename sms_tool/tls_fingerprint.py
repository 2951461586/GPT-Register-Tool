"""Per-account TLS ClientHello (JA3) uniqueness for the protocol lane.

Why this module exists
----------------------
``curl_cffi`` impersonation presets are *shared by every client on earth*: the
whole world's ``firefox144`` sends one ClientHello.  Measured 2026-11-11 against
``tls.peet.ws``, every Firefox profile this repo ships
(``firefox144``/``147``/``135``) produces the **same** ``ja3_hash``
``6f7889b9fb1a62a9577e685c1fcfa919`` and the same Akamai hash, i.e. 50 % of the
fingerprint-pool weight sits behind one value a WAF can blacklist in a single
line.  Chrome presets are fine (curl-impersonate permutes their extension order
per handshake); Safari is fixed too but see the limitation note below.

What this module does
---------------------
For the families where a permutation is *verified safe* (currently Firefox), it
permutes the **order** of the profile's own TLS extensions — never the set —
deterministically from the account's device seed, and installs the result as a
``CurlOpt.TLS_EXTENSION_ORDER`` session option.  Same ciphers, same extension
ids, same curves, same HTTP/2 settings; only the order differs, so the JA3 (and
JA4) hash becomes per-account while the handshake stays valid.

Measured after the change (``scripts/tls_fingerprint_probe.py``, through the
configured residential exit): three seeds -> three distinct ``ja3_hash`` values,
all answering HTTP 200 from ``auth.openai.com`` with the project's own header
set.  A per-handshake permutation was deliberately **not** used: a real browser
keeps one ClientHello per machine, so a stable-per-account value is the more
faithful of the two.

Known limitations (measured, do not "fix" blindly)
-------------------------------------------------
* **Safari is excluded.** Permuting its list makes BoringSSL reject the
  ClientHello (``curl: (35) Invalid TLS extensions``) — Safari's extension set
  carries ordering constraints this BoringSSL fork enforces.  Safari therefore
  keeps its preset order: ~18 % of accounts still share a JA3 per Safari
  version.
* **HTTP/2 (Akamai) is not variable through this path.** The presets are
  ``curl-impersonate`` native targets; ``extra_fp`` cannot rewrite their SETTINGS
  and ``Session(akamai=...)`` needs the *exact* base string, which curl_cffi
  0.16.0 does not expose for native targets (guestimation produced a hash that
  matched no real Chrome).  Left alone on purpose: a wrong Akamai profile is
  worse than a shared one.
* ``extra_fp["tls_grease"]`` / ``tls_signature_algorithms`` are **no-ops** on
  these native presets (verified: the JA3 does not move), so they are not used.

Configuration
-------------
``registration.tls_fingerprint``:

``enabled`` (default ``true``)
    Master switch.  Off = every account sends the raw preset (pre-fix behaviour).
``rotate_extensions`` (default ``true``)
    Permute the extension order for the families in
    :data:`VERIFIED_EXTENSION_ORDERS`.  Turning this off keeps the module loaded
    (so ``summary()`` still logs) but sends the preset order.
``seed_salt`` (default ``""``)
    Mixed into the per-account seed.  Changing it reshuffles every account
    without touching the pool.
"""

from __future__ import annotations

import hashlib
import logging
import random
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .auth_headers import _device_seed, current_auth_fingerprint

logger = logging.getLogger(__name__)

#: Extension id lists captured from the live ``curl_cffi`` presets
#: (``scripts/tls_fingerprint_probe.py --capture``, curl_cffi 0.16.0).
#:
#: Only families listed here are permuted.  A family is added only after the
#: probe proves the permuted order still completes a handshake **and** passes
#: the auth edge with this project's header set.
VERIFIED_EXTENSION_ORDERS: dict[str, str] = {
    # curl-impersonate firefox144/147/135 (identical list).
    "firefox": "0-23-65281-10-11-35-16-5-34-18-51-43-13-45-28-27-65037",
}

#: ``server_name`` (0) heads every real ClientHello; keep it there.
_FIRST_EXTENSION = "0"
#: ``padding`` (21) must stay last; BoringSSL rejects anything after it.
_PADDING_EXTENSION = "21"


@dataclass(frozen=True)
class TlsFingerprint:
    """The per-account ClientHello override and how it was produced."""

    profile: str = ""
    family: str = ""
    extension_order: str = ""
    rotated: bool = False
    reason: str = ""

    @property
    def active(self) -> bool:
        return bool(self.extension_order)

    def summary(self) -> str:
        if not self.active:
            return f"tls:{self.profile or '-'} preset-order ({self.reason or 'not-rotated'})"
        return f"tls:{self.profile or '-'} ext-order=rotated ({self.reason or 'seed'})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "family": self.family,
            "rotated": self.rotated,
            "reason": self.reason,
            # The order itself is not a secret, but it is noise in every log
            # line; keep it out of telemetry and available only on demand.
        }


_LOCAL = threading.local()
_EMPTY = TlsFingerprint(reason="unbound")


# ─────────────────────── config ───────────────────────


def _section(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if config is None:
        try:
            from .config import CFG

            config = CFG
        except ImportError:
            return {}
    if not isinstance(config, Mapping):
        return {}
    registration = config.get("registration")
    if not isinstance(registration, Mapping):
        return {}
    section = registration.get("tls_fingerprint")
    return section if isinstance(section, Mapping) else {}


def _flag(section: Mapping[str, Any], key: str, default: bool) -> bool:
    value = section.get(key, default)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class TlsFingerprintConfig:
    enabled: bool = True
    rotate_extensions: bool = True
    seed_salt: str = ""

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None = None) -> "TlsFingerprintConfig":
        section = _section(config)
        return cls(
            enabled=_flag(section, "enabled", True),
            rotate_extensions=_flag(section, "rotate_extensions", True),
            seed_salt=str(section.get("seed_salt") or ""),
        )


# ─────────────────────── derivation ───────────────────────


def _family(impersonate: str) -> str:
    token = str(impersonate or "").strip().lower()
    for family in ("firefox", "chrome", "safari", "edge"):
        if token.startswith(family):
            return family
    return ""


def permute_extension_order(base: str, seed: str) -> str:
    """Deterministically permute a preset ``a-b-c`` extension list.

    ``server_name`` stays first, ``padding`` stays last, everything between is
    shuffled by a seed-derived RNG.  The *set* is preserved exactly, which is
    what keeps the ClientHello a valid client of that family.
    """
    parts = [part for part in str(base or "").split("-") if part]
    if len(parts) < 3 or not seed:
        return ""
    head = [parts[0]] if parts[0] == _FIRST_EXTENSION else []
    middle = [part for part in parts[len(head) :] if part != _PADDING_EXTENSION]
    tail = [_PADDING_EXTENSION] if _PADDING_EXTENSION in parts[len(head) :] else []
    if len(middle) < 2:
        return ""
    digest = hashlib.sha256(f"tls-order/v1/{seed}".encode("utf-8", "surrogatepass")).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    rng.shuffle(middle)
    return "-".join(head + middle + tail)


def build(*, profile: str = "", seed: str = "", config: Mapping[str, Any] | None = None) -> TlsFingerprint:
    """Build the override for one account. Never raises."""
    settings = TlsFingerprintConfig.from_config(config)
    family = _family(profile)
    if not settings.enabled:
        return TlsFingerprint(profile=profile, family=family, reason="disabled")
    base = VERIFIED_EXTENSION_ORDERS.get(family, "")
    if not base:
        return TlsFingerprint(profile=profile, family=family, reason="family-not-verifiable")
    if not settings.rotate_extensions:
        return TlsFingerprint(profile=profile, family=family, reason="rotate-off")
    if not seed:
        return TlsFingerprint(profile=profile, family=family, reason="no-seed")
    order = permute_extension_order(base, f"{seed}|{settings.seed_salt}")
    if not order:
        return TlsFingerprint(profile=profile, family=family, reason="permute-failed")
    return TlsFingerprint(profile=profile, family=family, extension_order=order, rotated=True, reason="seed")


# ─────────────────────── thread binding ───────────────────────


def set_tls_fingerprint(
    seed: str = "", *, profile: str = "", config: Mapping[str, Any] | None = None
) -> TlsFingerprint:
    """Bind the current thread's override explicitly.

    An explicit binding is **authoritative** until it is replaced or cleared:
    callers that already know the account's seed and profile must not be
    re-derived from the looser thread state in :mod:`sms_tool.auth_headers`
    (which is empty before ``prepare_identity`` binds it).
    """
    fingerprint = build(profile=profile, seed=seed, config=config)
    _LOCAL.fingerprint = fingerprint
    _LOCAL.explicit = True
    return fingerprint


def clear_tls_fingerprint() -> None:
    _LOCAL.fingerprint = _EMPTY
    _LOCAL.explicit = False
    _LOCAL.key = None


def _current_profile() -> str:
    try:
        return str(current_auth_fingerprint().get("impersonate") or "")
    except (AttributeError, TypeError, ValueError):
        return ""


def _current_seed() -> str:
    try:
        return str(_device_seed() or "")
    except (AttributeError, TypeError, ValueError):
        return ""


def current_tls_fingerprint(config: Mapping[str, Any] | None = None) -> TlsFingerprint:
    """The override for the current thread, rebuilt when its inputs changed.

    Two sources, in order: an explicit :func:`set_tls_fingerprint` binding, then
    a lazy derivation from the auth-fingerprint thread state (device seed +
    selected profile).  The lazy cache key is ``(seed, profile)`` because
    ``prepare_identity`` binds the seed and *then* swaps the pooled profile; a
    stale value would install a Firefox order on a Safari session.
    """
    if getattr(_LOCAL, "explicit", False):
        cached = getattr(_LOCAL, "fingerprint", None)
        if isinstance(cached, TlsFingerprint):
            return cached
    seed = _current_seed()
    profile = _current_profile()
    cached = getattr(_LOCAL, "fingerprint", None)
    key = (seed, profile)
    if isinstance(cached, TlsFingerprint) and getattr(_LOCAL, "key", None) == key:
        return cached
    fingerprint = build(profile=profile, seed=seed, config=config)
    _LOCAL.fingerprint = fingerprint
    _LOCAL.key = key
    return fingerprint


# ─────────────────────── session application ───────────────────────


def _curl_extension_order_option():
    """``CurlOpt.TLS_EXTENSION_ORDER`` without importing curl_cffi at module load."""
    from curl_cffi.const import CurlOpt

    return CurlOpt.TLS_EXTENSION_ORDER


def apply_to_session(session: Any, config: Mapping[str, Any] | None = None) -> TlsFingerprint:
    """Install the current thread's extension order on an existing session.

    ``session.curl_options`` is read on every request by curl_cffi, so setting
    it after construction is equivalent to passing it to the constructor.  A
    failure is never fatal: the account simply keeps the preset order, which is
    what every account had before this module existed.
    """
    fingerprint = current_tls_fingerprint(config)
    if not fingerprint.active:
        return fingerprint
    try:
        existing = getattr(session, "curl_options", None)
        options = dict(existing) if isinstance(existing, dict) else {}
        options[_curl_extension_order_option()] = fingerprint.extension_order
        session.curl_options = options
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        logger.debug("tls_fingerprint: could not install session override (%s)", type(exc).__name__)
        return TlsFingerprint(
            profile=fingerprint.profile,
            family=fingerprint.family,
            reason=f"install-failed:{type(exc).__name__}",
        )
    return fingerprint


__all__ = [
    "VERIFIED_EXTENSION_ORDERS",
    "TlsFingerprint",
    "TlsFingerprintConfig",
    "apply_to_session",
    "build",
    "clear_tls_fingerprint",
    "current_tls_fingerprint",
    "permute_extension_order",
    "set_tls_fingerprint",
]
