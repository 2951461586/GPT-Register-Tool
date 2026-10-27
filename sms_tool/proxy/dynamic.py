"""Generate and gate fresh sticky-session proxies for an ASN allowlist.

The static pool answers "which configured exits are healthy *now*". This module
answers the other question the reference project's ``DynamicProxyFactory``
answers: "give me a **new** session that actually leaves through one of these
ASNs, and prove it before the registration uses it".

It deliberately does not invent provider templates. :func:`rotate_session` in
:mod:`sms_tool.proxy.entry` is the single authority for session-id rotation and
region retargeting, so a provider template works here only if it already works
there. Screening is the existing dual-pass :func:`screen_proxy`, which keeps the
gate (stability, country, datacenter, quality) identical to the CLI/desktop
report.

Wiring is opt-in (``proxy.dynamic_generation.enabled``). The batch's static pool
is unchanged when the switch is off; when it is on and generation fails, the
caller falls back to the static pool rather than killing the batch -- a
generation failure is an environment problem, not a reason to consume mailboxes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .entry import parse_proxy, rotate_session
from .screening import ScreeningSample, screen_proxy

#: Quality floor used by the reference's dynamic path. Kept as a default rather
#: than a hard-coded 80 so a pool with thinner evidence can opt down explicitly.
DEFAULT_QUALITY_MIN = 80

#: Bounded retry count. A provider that ignores the requested ASN can otherwise
#: spin forever; 30 matches the reference project's default.
DEFAULT_MAX_ATTEMPTS = 30


class ProxyGenerationError(RuntimeError):
    """No generated session matched the ASN/country/quality gate."""


@dataclass(frozen=True)
class DynamicProxyConfig:
    base_proxy: str
    country: str
    asns: tuple[str, ...] = ()
    count: int = 1
    session_minutes: int = 60
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    quality_min: int = DEFAULT_QUALITY_MIN

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None = None) -> "DynamicProxyConfig | None":
        """Parse ``proxy.dynamic_generation``; ``None`` when disabled/invalid."""
        proxy = config.get("proxy") if isinstance(config, Mapping) else None
        section = proxy.get("dynamic_generation") if isinstance(proxy, Mapping) else None
        if not isinstance(section, Mapping) or not bool(section.get("enabled")):
            return None
        base = str(section.get("base_proxy") or "").strip()
        if not base:
            base = _first_registration_proxy(proxy)
        entry = parse_proxy(base) if base else None
        if entry is None:
            return None
        country = str(section.get("country") or "").strip().upper()
        if len(country) != 2 or not country.isalpha():
            return None
        raw_asns = section.get("asns") or ()
        asns = tuple(
            dict.fromkeys(
                str(item).strip().upper()
                for item in (raw_asns if isinstance(raw_asns, (list, tuple)) else [raw_asns])
                if str(item).strip()
            )
        )
        return cls(
            base_proxy=entry.url,
            country=country,
            asns=asns,
            count=max(1, _as_int(section.get("count"), 1)),
            session_minutes=max(1, _as_int(section.get("session_minutes"), 60)),
            max_attempts=max(1, _as_int(section.get("max_attempts"), DEFAULT_MAX_ATTEMPTS)),
            quality_min=max(0, min(100, _as_int(section.get("quality_min"), DEFAULT_QUALITY_MIN))),
        )


def _first_registration_proxy(proxy: Mapping[str, Any] | None) -> str:
    if not isinstance(proxy, Mapping):
        return ""
    for key in ("registration", "default"):
        value = proxy.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    pool = proxy.get("pool")
    if isinstance(pool, (list, tuple)):
        for item in pool:
            if str(item or "").strip():
                return str(item).strip()
    return ""


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class DynamicProxyFactory:
    """Rotate a provider session, screen it, and require the configured ASN."""

    config: DynamicProxyConfig
    screen: Callable[..., ScreeningSample] = screen_proxy
    rotate: Callable[[str, str], str] = rotate_session
    last_screening: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None = None) -> "DynamicProxyFactory | None":
        parsed = DynamicProxyConfig.from_config(config)
        return cls(parsed) if parsed is not None else None

    def next_proxy(self) -> str:
        """Return one eligible, ASN-matching proxy or raise ``ProxyGenerationError``."""
        last_reason = "no attempt"
        for attempt in range(1, self.config.max_attempts + 1):
            candidate = self.rotate(self.config.base_proxy, self.config.country)
            if not candidate:
                last_reason = "rotation returned an empty proxy"
                continue
            sample = self.screen(
                candidate,
                country=self.config.country,
                quality_min=self.config.quality_min,
            )
            asn = str(getattr(sample, "asn", "") or "").upper()
            eligible = bool(getattr(sample, "eligible", False))
            matched = eligible and (not self.config.asns or asn in self.config.asns)
            self.last_screening = {
                "accepted": matched,
                "proxy": candidate,
                "asn": asn,
                "country": str(getattr(sample, "country", "") or ""),
                "network_type": str(getattr(sample, "network_type", "") or "unknown"),
                "quality_score": int(getattr(sample, "quality_score", 0) or 0),
                "attempt": attempt,
                "reason_code": (
                    ""
                    if matched
                    else (
                        "asn_mismatch"
                        if eligible and self.config.asns and asn not in self.config.asns
                        else str(getattr(sample, "reason_code", "") or "ineligible")
                    )
                ),
            }
            if matched:
                return candidate
            last_reason = str(self.last_screening["reason_code"] or "ineligible")
        raise ProxyGenerationError(
            f"dynamic proxy generation failed after {self.config.max_attempts} attempt(s): {last_reason}"
        )

    def generate(self, count: int | None = None) -> list[str]:
        """Generate up to ``count`` distinct eligible proxies (stops on exhaustion)."""
        wanted = max(1, _as_int(count, self.config.count))
        generated: list[str] = []
        for _ in range(wanted):
            try:
                proxy = self.next_proxy()
            except ProxyGenerationError:
                break
            if proxy not in generated:
                generated.append(proxy)
        return generated


def generate_pool(
    config: Mapping[str, Any] | None = None,
    *,
    screen: Callable[..., ScreeningSample] | None = None,
    rotate: Callable[[str, str], str] | None = None,
) -> list[str]:
    """Convenience entry point: ``[]`` when the feature is off or generation fails."""
    parsed = DynamicProxyConfig.from_config(config)
    if parsed is None:
        return []
    factory = DynamicProxyFactory(
        parsed,
        screen=screen or screen_proxy,
        rotate=rotate or rotate_session,
    )
    try:
        return factory.generate()
    except ProxyGenerationError:
        return []


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_QUALITY_MIN",
    "DynamicProxyConfig",
    "DynamicProxyFactory",
    "ProxyGenerationError",
    "generate_pool",
]
