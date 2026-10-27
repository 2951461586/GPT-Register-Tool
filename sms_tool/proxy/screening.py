"""Proxy quality screening: the IP-intelligence layer.

Why this module exists
----------------------
The pool already answers "does this exit reach ChatGPT?" — ``proxy/edge_probe``
classifies a Cloudflare 403/429/challenge and ``scripts/proxy_pool_probe.py``
applies it to a whole pool.  It does **not** answer "is this exit actually a
residential line in the country we think it is?", which is the question that
decides whether a batch burns accounts on a datacenter IP that OpenAI's risk
engine already recognises.

Screening is therefore a second, orthogonal axis:

* **residential vs datacenter** — org/ASN/privacy-flag classification.
* **stability** — the same session must report the same exit twice.
* **country match** — both passes must agree with the target country.
* **cross-source evidence** — two independent intel documents (ipinfo and
  ipwho/ipapi answer different fields) must not contradict each other.
* **latency + a bounded quality score** — comparable across candidates.

This mirrors the reference project's ``webui/proxy_screening.py`` (dual-pass
``evaluate_sample`` + ``score_probe_quality`` + ``aggregate_by_asn``) without
its 711proxy-specific template parsing: this repository already owns proxy
string parsing in :mod:`sms_tool.proxy.entry`, and provider session generation
belongs there, not here.

What it deliberately does not do
--------------------------------
* No caching. The shared geo resolver caches per proxy; a stability check that
  reads the cache twice would always "pass". Every pass here calls
  ``geo.resolver.probe_exit_geo`` directly.
* No policy. It classifies and scores; deciding a minimum score or which ASNs
  to keep belongs to the caller (``proxy.asn_registry`` / the CLI/UI layer).
* No proxy parsing. Inputs are already-normalized proxy URLs.

Bounded failure contract
------------------------
Every failure is a ``(reason_code, reason)`` pair, never an exception. The
caller groups samples by ASN and decides; nothing here silently promotes a
proxy because its probe crashed.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from statistics import median
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:  # annotation-only: see the delayed-import note below
    from ..geo.resolver import ProxyGeo

# Why the geo import is function-local
# ------------------------------------
# ``scripts/import_layer_ratchet.py`` freezes module-level cross-directory edges
# inside ``sms_tool`` and has no ``proxy -> geo`` edge at all. Screening needs
# ``geo.resolver`` on every call, so the import is deferred to the two functions
# that actually probe (``probe_once`` / ``secondary_endpoints``) rather than
# widening the architecture ceiling for a module that only the proxy screen and
# the batch runner touch. ``ProxyGeo`` itself is only ever used for annotations
# and duck-typed attribute access, so the ``TYPE_CHECKING`` import is enough.

# ── vocabulary ────────────────────────────────────────────────────────────────
NETWORK_RESIDENTIAL = "residential"
NETWORK_MOBILE = "mobile"
NETWORK_DATACENTER = "datacenter"
NETWORK_UNKNOWN = "unknown"

REASON_OK = ""
REASON_PROXY_FAILED = "proxy_failed"
REASON_UNSTABLE_EXIT = "unstable_exit"
REASON_COUNTRY_MISMATCH = "country_mismatch"
REASON_ASN_CHANGED = "asn_changed"
REASON_PROXY_DETECTED = "proxy_detected"
REASON_DATACENTER = "datacenter_detected"
REASON_INTEL_UNKNOWN = "intel_unknown"
REASON_INTEL_INSUFFICIENT = "intel_insufficient"

#: Org/ISP substrings that mean "this is not a consumer line".
#:
#: Kept as prose in the error message (``reason``) so an operator reading the
#: report does not have to look up a code. Ordered roughly by how often a
#: residential provider hands one out by accident.
DATACENTER_KEYWORDS: tuple[str, ...] = (
    "hosting",
    "datacenter",
    "data center",
    "data centre",
    "cloud",
    "colocation",
    "server",
    "vps",
    "amazon",
    "aws",
    "google llc",
    "google cloud",
    "microsoft",
    "azure",
    "digitalocean",
    "linode",
    "ovh",
    "hetzner",
    "vultr",
    "contabo",
    "oracle",
    "tencent",
    "alibaba",
    "aliyun",
    "huawei cloud",
    "leaseweb",
    "choopa",
    "m247",
    "proxy",
    "vpn",
)

#: Mobile carriers are a distinct (and often *better*) risk profile than fixed
#: residential: CGNAT makes per-IP history meaningless. They are accepted, but
#: reported as ``mobile`` so ASN aggregation can rank them separately.
MOBILE_KEYWORDS: tuple[str, ...] = (
    "mobile",
    "cellular",
    "wireless",
    "4g",
    "5g",
    "lte",
    "telkomsel",
    "indosat",
    "xl axiata",
    "verizon wireless",
    "t-mobile",
    "vodafone",
    "airtel",
    "jio",
    "globe telecom",
    "smart communications",
)

#: ``scheme://user:pass@host`` and bare ``user:pass:host:port`` both appear in
#: provider paste boxes and in probe error strings. Redaction runs before any
#: text reaches a log line or a UI table.
_URL_CREDENTIALS_RE = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<userinfo>[^/@\s]+)@", re.IGNORECASE)
#: ``user:pass@host:port`` with **no scheme**, i.e. the curl/SOCKS paste form.
#: ``normalize_proxy_url`` accepts it (it becomes ``http://user:pass@host:port``),
#: so it is a real configured value -- but ``_URL_CREDENTIALS_RE`` above requires
#: the ``scheme://`` prefix, so the userinfo passed both redactors untouched.
#: Measured 2026-10-27: ``mask_proxy("user:secret@h.example:8080")`` echoed the
#: password into the screening table and into support-bundle text. The lookbehind
#: excludes ``/`` and the mask characters (``*``) so the already-masked
#: ``scheme://***:***@host`` output of the previous rule cannot be re-matched
#: (without ``*`` it ate one star and produced ``****:***@``), and the port
#: lookahead keeps ``user:pass@host`` (a mailbox, not a proxy) out of scope.
_SCHEMELESS_CREDENTIALS_RE = re.compile(
    r"(?<![\w:/@.*-])(?P<user>[^\s:@/\\]+):(?P<password>[^\s:@/\\]+)@(?=[^\s@/\\]+:\d{1,5})"
)
_BARE_CREDENTIALS_RE = re.compile(
    r"(?<![\w.:/-])(?P<user>[^\s:@/\\]+):(?P<password>[^\s:@/\\]+):(?P<host>[^\s:@/\\]+:\d{1,5})"
)
#: The provider UI's other paste form: ``host:port:user:pass``. The regex above
#: only knows ``user:pass:host:port``, so a bare host-first value used to pass
#: through ``mask_proxy`` unchanged (measured: ``host:8080:user:pass`` printed
#: the password). Both forms are accepted by ``proxy.entry.parse_proxy``, so
#: both must be masked before a probe error or a report line can reach a sink.
_HOST_FIRST_CREDENTIALS_RE = re.compile(
    r"(?<![\w.:/-])(?P<host>[^\s:@/]+:\d{1,5}):(?P<user>[^\s:@/]+):(?P<password>[^\s:@/]+)"
)


def sanitize_proxy_text(text: Any) -> str:
    """Mask credentials in free-form text (probe errors, report lines)."""

    def _mask_url(match: re.Match[str]) -> str:
        return f"{match.group('scheme')}***:***@"

    def _mask_bare(match: re.Match[str]) -> str:
        return f"***:***:{match.group('host')}"

    def _mask_host_first(match: re.Match[str]) -> str:
        return f"{match.group('host')}:***:***"

    def _mask_schemeless(match: re.Match[str]) -> str:
        return "***:***@"

    value = str(text or "")
    value = _URL_CREDENTIALS_RE.sub(_mask_url, value)
    value = _SCHEMELESS_CREDENTIALS_RE.sub(_mask_schemeless, value)
    value = _HOST_FIRST_CREDENTIALS_RE.sub(_mask_host_first, value)
    return _BARE_CREDENTIALS_RE.sub(_mask_bare, value)


def mask_proxy(proxy: str) -> str:
    """Return the scheme/host/port of a proxy with credentials replaced."""
    value = str(proxy or "")
    try:
        parsed = urlsplit(value)
    except ValueError:
        return sanitize_proxy_text(value)
    if not parsed.hostname:
        return sanitize_proxy_text(value)
    credentials = "***:***@" if (parsed.username or parsed.password) else ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme or 'http'}://{credentials}{parsed.hostname}{port}"


# ── classification ────────────────────────────────────────────────────────────
def _intel_text(intel: Any) -> str:
    """Flatten the org-ish fields of a geo/profile/mapping into one haystack."""
    if isinstance(intel, Mapping):
        parts = tuple(intel.get(key) or "" for key in ("org", "isp", "as", "asn", "hostname", "reverse_dns", "type"))
    else:
        # Duck-typed on purpose: ``ProxyGeo`` is annotation-only here, so a
        # ``getattr`` walk also accepts the desktop's plain profile dicts.
        parts = tuple(str(getattr(intel, key, "") or "") for key in ("org", "isp", "asn", "reverse_dns"))
    return " ".join(str(part) for part in parts).lower()


def _privacy_flag(intel: Any, name: str) -> bool:
    if isinstance(intel, Mapping):
        block = intel.get("privacy")
        nested = intel.get("security")
        for candidate in (intel, block, nested):
            if isinstance(candidate, Mapping) and candidate.get(name) is True:
                return True
        return False
    return getattr(intel, f"is_{name}", False) is True


def classify_network_type(intel: Any | None) -> str:
    """Classify an exit as ``datacenter`` / ``mobile`` / ``residential`` / ``unknown``.

    Explicit privacy flags win over org text: a provider that *says* ``hosting``
    is believed even when the org string reads like a consumer ISP (a resold
    residential block routed through a datacenter is still a datacenter exit).
    """
    if intel is None:
        return NETWORK_UNKNOWN
    text = _intel_text(intel)
    if any(_privacy_flag(intel, name) for name in ("hosting", "proxy", "vpn")):
        return NETWORK_DATACENTER
    if any(keyword in text for keyword in DATACENTER_KEYWORDS):
        return NETWORK_DATACENTER
    if any(keyword in text for keyword in MOBILE_KEYWORDS):
        return NETWORK_MOBILE
    country = intel.get("country") if isinstance(intel, Mapping) else getattr(intel, "country", "")
    has_org = bool(str((intel.get("org") if isinstance(intel, Mapping) else getattr(intel, "org", "")) or "").strip())
    if country and has_org:
        return NETWORK_RESIDENTIAL
    return NETWORK_UNKNOWN


def _evidence_confidence(network_type: str, intel_sources: int, conflict: bool) -> int:
    """How strong the *evidence* is — never a claim that the exit is clean."""
    if network_type == NETWORK_DATACENTER:
        return 0
    if network_type == NETWORK_UNKNOWN:
        return 0
    base = 60 if network_type == NETWORK_RESIDENTIAL else 50
    if intel_sources >= 2:
        base += 20
    if conflict:
        base -= 30
    return max(0, min(100, base))


# ── one probe pass ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ScreeningPass:
    """One side of the stability check: a single uncached intel measurement."""

    ok: bool = False
    ip: str = ""
    country: str = ""
    asn: str = ""
    isp: str = ""
    org: str = ""
    timezone: str = ""
    network_type: str = NETWORK_UNKNOWN
    reverse_dns: str = ""
    is_proxy: bool = False
    is_vpn: bool = False
    is_hosting: bool = False
    residential_confidence: int = 0
    intel_sources: int = 1
    intel_conflict: bool = False
    latency_ms: int | None = None
    error_code: str = ""
    error: str = ""

    @classmethod
    def from_geo(cls, geo: "ProxyGeo", *, latency_ms: int | None = None, source: str = "") -> "ScreeningPass":
        """Build a pass from a measured ``ProxyGeo`` (empty geo means failure)."""
        if not geo.known:
            return cls(ok=False, latency_ms=latency_ms, error_code=REASON_PROXY_FAILED, error="no intel answer")
        network_type = classify_network_type(geo)
        flags = (geo.is_proxy, geo.is_vpn, geo.is_hosting)
        return cls(
            ok=True,
            ip=geo.ip,
            country=geo.country.upper(),
            asn=geo.asn.upper(),
            isp=geo.isp,
            org=geo.org,
            timezone=geo.timezone,
            network_type=network_type,
            reverse_dns=geo.reverse_dns,
            is_proxy=geo.is_proxy,
            is_vpn=geo.is_vpn,
            is_hosting=geo.is_hosting,
            residential_confidence=_evidence_confidence(network_type, geo.intel_sources, geo.intel_conflict),
            intel_sources=max(1, _as_int(geo.intel_sources, 1)),
            intel_conflict=bool(geo.intel_conflict),
            latency_ms=latency_ms,
            error_code="" if not any(flags) else REASON_PROXY_DETECTED,
            error="" if not any(flags) else "exit reports proxy/VPN/hosting",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "ip": self.ip,
            "country": self.country,
            "asn": self.asn,
            "isp": self.isp,
            "org": self.org,
            "timezone": self.timezone,
            "network_type": self.network_type,
            "reverse_dns": self.reverse_dns,
            "is_proxy": self.is_proxy,
            "is_vpn": self.is_vpn,
            "is_hosting": self.is_hosting,
            "residential_confidence": self.residential_confidence,
            "intel_sources": self.intel_sources,
            "intel_conflict": self.intel_conflict,
            "latency_ms": self.latency_ms,
            "error_code": self.error_code,
            "error": self.error,
        }


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce an external/optional number without raising (probe data is untrusted)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _elapsed_ms(started: float) -> int:
    return max(0, _as_int((time.perf_counter() - started) * 1000))


def probe_once(
    proxy: str,
    *,
    timeout: float = 6.0,
    endpoints: Sequence[tuple[str, str]] | None = None,
    probe: Callable[..., "ProxyGeo"] | None = None,
) -> ScreeningPass:
    """One uncached intel pass. Never raises."""
    from ..geo.resolver import GEO_ENDPOINTS, probe_exit_geo

    started = time.perf_counter()
    probe_fn = probe or probe_exit_geo
    try:
        geo = probe_fn(
            proxy,
            timeout=timeout,
            endpoints=tuple(endpoints) if endpoints else GEO_ENDPOINTS,
            need_timezone=True,
        )
    except Exception as exc:  # a probe failure is data, not a crash
        return ScreeningPass(
            ok=False,
            latency_ms=_elapsed_ms(started),
            error_code=REASON_PROXY_FAILED,
            error=sanitize_proxy_text(f"{type(exc).__name__}: {exc}"),
        )
    return ScreeningPass.from_geo(geo, latency_ms=_elapsed_ms(started))


#: Second pass walks the same documents in the opposite order. Same sources,
#: different precedence: a provider that answers inconsistently shows up as an
#: ``intel_conflict`` instead of being masked by whichever endpoint is first.
#:
#: A function rather than a module constant because ``geo.resolver`` is imported
#: lazily (see the note at the top of this module).
def secondary_endpoints() -> tuple[tuple[str, str], ...]:
    from ..geo.resolver import GEO_ENDPOINTS

    return tuple(reversed(GEO_ENDPOINTS))


# ── scoring + verdict ─────────────────────────────────────────────────────────
def score_probe_quality(first: ScreeningPass, second: ScreeningPass) -> int:
    """Score the strength of the residential evidence, 0-100.

    Bounded and additive on purpose: an operator compares candidates, so the
    number must move for reasons a human can name (same IP, same ASN, two
    sources, no privacy flags) rather than for an unexplained formula.
    """
    if not first.ok or not second.ok:
        return 0
    # Explicit proxy/VPN/hosting evidence means there is no residential quality
    # to score. This is a hard zero rather than a penalty because a flagged
    # exit must never outrank a failed probe (also 0) in an operator's table.
    if any(getattr(item, name, False) for item in (first, second) for name in ("is_proxy", "is_vpn", "is_hosting")):
        return 0
    score = 25  # two usable answers
    if first.ip and first.ip == second.ip:
        score += 25
    if first.asn and first.asn == second.asn:
        score += 15
    if first.country and first.country == second.country:
        score += 15
    if second.network_type == NETWORK_RESIDENTIAL:
        score += 15
    elif second.network_type == NETWORK_MOBILE:
        score += 10
    if second.reverse_dns:
        score += 5
    if min(first.intel_sources, second.intel_sources) >= 2:
        score += 10
    else:
        score -= 20
    if first.intel_conflict or second.intel_conflict:
        score -= 25
    return max(0, min(100, score))


def evaluate_sample(
    first: ScreeningPass,
    second: ScreeningPass,
    target_country: str = "",
    *,
    quality_min: int = 0,
) -> tuple[bool, str, str]:
    """Return ``(eligible, reason_code, reason)`` for one dual-pass sample.

    Order matters: the cheapest, most decisive failure wins so the reason an
    operator reads is the actual blocker, not a downstream consequence.
    """
    if not first.ok or not second.ok:
        return False, REASON_PROXY_FAILED, first.error or second.error or "intel probe failed"
    if first.ip != second.ip:
        return False, REASON_UNSTABLE_EXIT, "两次出口 IP 不一致"
    country = str(target_country or "").strip().upper()
    if country and (first.country != country or second.country != country):
        return False, REASON_COUNTRY_MISMATCH, "出口国家与目标地区不一致"
    if first.asn != second.asn:
        return False, REASON_ASN_CHANGED, "两次 ASN 不一致"
    flagged = any(
        getattr(item, name, False) for item in (first, second) for name in ("is_proxy", "is_vpn", "is_hosting")
    )
    if flagged:
        return False, REASON_PROXY_DETECTED, "出口被标记为代理、VPN 或托管网络"
    if second.network_type == NETWORK_DATACENTER:
        return False, REASON_DATACENTER, "识别为托管/机房网络"
    if second.network_type == NETWORK_UNKNOWN:
        return False, REASON_INTEL_UNKNOWN, "网络类型证据不足"
    quality = score_probe_quality(first, second)
    if quality_min and quality < _as_int(quality_min, 0):
        return (
            False,
            REASON_INTEL_INSUFFICIENT,
            f"住宅证据不足（质量分 {quality}，要求 {_as_int(quality_min, 0)}）",
        )
    return True, REASON_OK, "国家匹配、出口稳定、非明确机房网络"


# ── sample + pool ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ScreeningSample:
    """One proxy's dual-pass result. Shape matches what the desktop table renders."""

    index: int
    proxy: str
    masked_proxy: str = ""
    first: ScreeningPass = field(default_factory=ScreeningPass)
    second: ScreeningPass = field(default_factory=ScreeningPass)
    eligible: bool = False
    reason_code: str = REASON_PROXY_FAILED
    reason: str = ""
    network_type: str = NETWORK_UNKNOWN
    asn: str = ""
    isp: str = ""
    country: str = ""
    latency_ms: int | None = None
    quality_score: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "proxy": self.masked_proxy or mask_proxy(self.proxy),
            "eligible": self.eligible,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "network_type": self.network_type,
            "asn": self.asn,
            "isp": self.isp,
            "country": self.country,
            "latency_ms": self.latency_ms,
            "quality_score": self.quality_score,
            "first": self.first.to_dict(),
            "second": self.second.to_dict(),
        }


def _median_latency(first: ScreeningPass, second: ScreeningPass) -> int | None:
    values = [value for value in (first.latency_ms, second.latency_ms) if value is not None]
    return _as_int(median(values)) if values else None


def screen_proxy(
    proxy: str,
    *,
    country: str = "",
    quality_min: int = 0,
    timeout: float = 6.0,
    index: int = 0,
    probe: Callable[..., "ProxyGeo"] | None = None,
    secondary: Sequence[tuple[str, str]] | None = None,
) -> ScreeningSample:
    """Run the dual pass + verdict for one (sticky) proxy entry."""
    first = probe_once(proxy, timeout=timeout, probe=probe)
    second = probe_once(
        proxy,
        timeout=timeout,
        endpoints=secondary or secondary_endpoints(),
        probe=probe,
    )
    eligible, reason_code, reason = evaluate_sample(first, second, country, quality_min=quality_min)
    if first.ok and second.ok and not second.intel_conflict:
        # Two independent documents answered the same exit: that is what the
        # quality score rewards. ``ScreeningPass`` is frozen, so replace rather
        # than mutate — a shared/derived pass must never change under a caller.
        second = replace(second, intel_sources=max(2, second.intel_sources))
    return ScreeningSample(
        index=index,
        proxy=proxy,
        masked_proxy=mask_proxy(proxy),
        first=first,
        second=second,
        eligible=eligible,
        reason_code=reason_code,
        reason=reason,
        network_type=second.network_type,
        asn=second.asn or first.asn,
        isp=second.isp or first.isp,
        country=second.country or first.country,
        latency_ms=_median_latency(first, second),
        quality_score=score_probe_quality(first, second),
    )


def screen_pool(
    proxies: Iterable[str],
    *,
    country: str = "",
    workers: int = 8,
    quality_min: int = 0,
    timeout: float = 6.0,
    probe: Callable[..., "ProxyGeo"] | None = None,
) -> list[ScreeningSample]:
    """Screen a whole pool concurrently, preserving input order."""
    entries = [str(item or "").strip() for item in proxies if str(item or "").strip()]
    if not entries:
        return []
    try:
        requested = _as_int(workers, 8)
    except (TypeError, ValueError):
        requested = 8
    max_workers = max(1, min(requested, 32, len(entries)))

    def run(indexed: tuple[int, str]) -> ScreeningSample:
        index, proxy = indexed
        return screen_proxy(
            proxy,
            country=country,
            quality_min=quality_min,
            timeout=timeout,
            index=index,
            probe=probe,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return list(executor.map(run, list(enumerate(entries))))


# ── ASN aggregation ───────────────────────────────────────────────────────────
def aggregate_by_asn(samples: Sequence[ScreeningSample]) -> list[dict[str, Any]]:
    """Group samples by ASN and rank the groups an operator should import first.

    Ranking is lexicographic, not a weighted sum, so every position can be
    explained: eligible count → median quality → stability rate → latency → ASN.
    """
    groups: dict[str, list[ScreeningSample]] = {}
    for sample in samples:
        if sample.asn:
            groups.setdefault(sample.asn, []).append(sample)
    rows: list[dict[str, Any]] = []
    for asn, items in groups.items():
        latencies = [item.latency_ms for item in items if item.latency_ms is not None]
        scores = [item.quality_score for item in items if item.quality_score]
        isps = [item.isp for item in items if item.isp]
        rows.append(
            {
                "asn": asn,
                "isp": max(set(isps), key=isps.count) if isps else "",
                "sample_count": len(items),
                "success_count": sum(1 for item in items if item.first.ok and item.second.ok),
                "eligible_count": sum(1 for item in items if item.eligible),
                "country_match_rate": _rate(
                    items, lambda item: bool(item.country) and item.country == _target_of(item)
                ),
                "stability_rate": _rate(items, lambda item: item.first.ok and item.first.ip == item.second.ip),
                "residential_rate": _rate(
                    items, lambda item: item.network_type in (NETWORK_RESIDENTIAL, NETWORK_MOBILE)
                ),
                "median_latency_ms": _as_int(median(latencies)) if latencies else None,
                "median_quality_score": _as_int(median(scores)) if scores else 0,
            }
        )
    rows.sort(
        key=lambda row: (
            -row["eligible_count"],
            -row["median_quality_score"],
            -row["stability_rate"],
            row["median_latency_ms"] if row["median_latency_ms"] is not None else 999999,
            row["asn"],
        )
    )
    return rows


def _target_of(sample: ScreeningSample) -> str:
    """The country this sample was judged against, recovered from its passes.

    ``aggregate_by_asn`` only sees samples, not the screening request. Two
    agreeing passes mean "the country it matched"; a mismatch sample keeps the
    target in ``first.country``/``second.country`` disagreement, so the rate
    falls back to comparing the two passes with each other.
    """
    if sample.first.country and sample.first.country == sample.second.country:
        return sample.first.country
    return sample.country or sample.first.country or sample.second.country


def _rate(items: Sequence[ScreeningSample], predicate: Callable[[ScreeningSample], bool]) -> float:
    if not items:
        return 0.0
    return round(sum(1 for item in items if predicate(item)) / len(items) * 100, 1)


def summarize(samples: Sequence[ScreeningSample]) -> dict[str, Any]:
    """Counts for a CLI/UI header; ``eligible`` is a subset of ``total``."""
    total = len(samples)
    eligible = [item for item in samples if item.eligible]
    reasons: dict[str, int] = {}
    for item in samples:
        if item.eligible:
            continue
        reasons[item.reason_code] = reasons.get(item.reason_code, 0) + 1
    return {
        "total": total,
        "eligible": len(eligible),
        "rejected": total - len(eligible),
        "reasons": reasons,
        "asns": len({item.asn for item in samples if item.asn}),
    }


def format_report(samples: Sequence[ScreeningSample]) -> str:
    """One line per sample plus an ASN ranking; credentials are always masked."""
    lines = []
    for sample in samples:
        verdict = "OK " if sample.eligible else "NO "
        lines.append(
            f"{verdict}{sample.masked_proxy}  "
            f"country={sample.country or '-'} asn={sample.asn or '-'} net={sample.network_type} "
            f"q={sample.quality_score} lat={sample.latency_ms if sample.latency_ms is not None else '-'}ms "
            f"reason={sample.reason_code or 'ok'}"
        )
    rows = aggregate_by_asn(samples)
    if rows:
        lines.append("")
        lines.append("ASN ranking (eligible / samples, median quality):")
        for row in rows:
            lines.append(
                f"  {row['asn']:<12} {row['eligible_count']:>3}/{row['sample_count']:<3} "
                f"q={row['median_quality_score']:<3} stability={row['stability_rate']}% "
                f"residential={row['residential_rate']}% isp={row['isp'] or '-'}"
            )
    return "\n".join(lines)


__all__ = [
    "DATACENTER_KEYWORDS",
    "MOBILE_KEYWORDS",
    "NETWORK_DATACENTER",
    "NETWORK_MOBILE",
    "NETWORK_RESIDENTIAL",
    "NETWORK_UNKNOWN",
    "REASON_ASN_CHANGED",
    "REASON_COUNTRY_MISMATCH",
    "REASON_DATACENTER",
    "REASON_INTEL_INSUFFICIENT",
    "REASON_INTEL_UNKNOWN",
    "REASON_OK",
    "REASON_PROXY_DETECTED",
    "REASON_PROXY_FAILED",
    "REASON_UNSTABLE_EXIT",
    "ScreeningPass",
    "ScreeningSample",
    "aggregate_by_asn",
    "classify_network_type",
    "evaluate_sample",
    "format_report",
    "mask_proxy",
    "probe_once",
    "sanitize_proxy_text",
    "score_probe_quality",
    "screen_pool",
    "screen_proxy",
    "secondary_endpoints",
    "summarize",
]
