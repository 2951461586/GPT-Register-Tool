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
from .screening import ScreeningSample, mask_proxy, screen_proxy

#: Quality floor used by the reference's dynamic path. Kept as a default rather
#: than a hard-coded 80 so a pool with thinner evidence can opt down explicitly.
DEFAULT_QUALITY_MIN = 80

#: Bounded retry count. A provider that ignores the requested ASN can otherwise
#: spin forever; 30 matches the reference project's default.
DEFAULT_MAX_ATTEMPTS = 30


class ProxyGenerationError(RuntimeError):
    """No generated session matched the ASN/country/quality gate."""


@dataclass(frozen=True)
class DynamicGeneration:
    """What :meth:`DynamicProxyFactory.generate_with_status` actually did.

    The silent ``[]`` return made "the switch is on but nothing was generated"
    indistinguishable from "the switch is off": both printed nothing and both
    fell back to the static pool.  The batch report now carries these three
    fields so a run can be told apart without reading the log.
    """

    used: bool = False
    count: int = 0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"used": bool(self.used), "count": int(self.count), "reason": str(self.reason or "")}


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
                # Masked, never the raw URL: this field is diagnostic state that a
                # future report may serialise, and the raw entry carries the
                # provider's username/password.  ``mask_proxy`` is the same helper
                # the desktop pool table and CLI report use.
                "proxy": mask_proxy(candidate),
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
        return list(self.generate_with_status(count))

    def generate_with_status(self, count: int | None = None) -> "GenerationOutcome":
        """Like :meth:`generate`, but also reports *why* it came back short."""
        wanted = max(1, _as_int(count, self.config.count))
        generated: list[str] = []
        reason = ""
        for _ in range(wanted):
            try:
                proxy = self.next_proxy()
            except ProxyGenerationError as exc:
                reason = _reason_token(str(exc))
                break
            if proxy not in generated:
                generated.append(proxy)
        if not generated:
            reason = reason or "no_eligible_exit"
        return GenerationOutcome(generated, reason)


class GenerationOutcome(list):
    """The generated proxies plus the status the caller reports.

    Subclasses ``list`` so every existing caller (``generate`` returned a list,
    and ``if generated:`` / ``len(generated)`` are the only uses) keeps working
    unchanged while the batch can read ``outcome.status``.
    """

    def __init__(self, values: list[str], reason: str = "") -> None:
        super().__init__(values)
        self.status = DynamicGeneration(used=bool(values), count=len(values), reason=reason)


def _reason_token(text: str) -> str:
    """Bound a generation failure message to one log/JSON token.

    The message carries the last screening reason code (``asn_mismatch``,
    ``datacenter_detected`` …); it never carries a proxy URL, but the bound keeps
    a future change from smuggling one into the audit record.
    """
    cleaned = " ".join(str(text or "").split())
    if ": " in cleaned:
        cleaned = cleaned.rsplit(": ", 1)[-1]
    return cleaned[:60]


def generate_pool(
    config: Mapping[str, Any] | None = None,
    *,
    screen: Callable[..., ScreeningSample] | None = None,
    rotate: Callable[[str, str], str] | None = None,
) -> list[str]:
    """Convenience entry point: ``[]`` when the feature is off or generation fails."""
    return list(generate_pool_report(config, screen=screen, rotate=rotate))


def generate_pool_report(
    config: Mapping[str, Any] | None = None,
    *,
    screen: Callable[..., ScreeningSample] | None = None,
    rotate: Callable[[str, str], str] | None = None,
) -> GenerationOutcome:
    """Same as :func:`generate_pool`, carrying the status for the batch report."""
    parsed = DynamicProxyConfig.from_config(config)
    if parsed is None:
        return GenerationOutcome([], "disabled")
    factory = DynamicProxyFactory(
        parsed,
        screen=screen or screen_proxy,
        rotate=rotate or rotate_session,
    )
    try:
        return factory.generate_with_status()
    except ProxyGenerationError as exc:
        return GenerationOutcome([], _reason_token(str(exc)))


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_QUALITY_MIN",
    "DynamicGeneration",
    "DynamicProxyConfig",
    "DynamicProxyFactory",
    "GenerationOutcome",
    "ProxyGenerationError",
    "generate_pool",
    "generate_pool_report",
]
