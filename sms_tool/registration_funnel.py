"""Safe, offline registration and promotion denominators for batch reports."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from .error_classification import classify_error
from .failure_registry import FAILURE_CLASSES
from .registration_state import RegistrationState

_FAILURE_CLASSES = frozenset(item.code for item in FAILURE_CLASSES)
_STAGES = frozenset(state.value for state in RegistrationState) - {"failed", "completed"}
_PROXY_SOURCES = frozenset({"registration_affinity", "operation_pool", "explicit", "direct"})


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _count(value: Any) -> int:
    """Coerce one audit counter; a hand-edited row must not break the funnel."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _edge_challenge_totals(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Sum the per-run in-flow challenge counters from ``proxy_audit``.

    These are the A/B's only view of "did an exit challenge actually happen, and
    did the run move off that exit": the counters are counts, never exit
    identities (see ``registration_result.safe_proxy_audit``).
    """
    hits = rotations = failed = unknown = 0
    for row in rows:
        audit = row.get("proxy_audit")
        audit = audit if isinstance(audit, Mapping) else {}
        hits += _count(audit.get("edge_challenge_hits"))
        unknown += _count(audit.get("edge_challenge_unknown"))
        rotations += _count(audit.get("edge_challenge_rotations"))
        failed += _count(audit.get("edge_challenge_rotate_failed"))
    return {"hits": hits, "unknown": unknown, "rotations": rotations, "rotate_failed": failed}


def _failure_stage(result: Mapping[str, Any]) -> str:
    machine = result.get("registration_machine")
    history = machine.get("history") if isinstance(machine, Mapping) else None
    if isinstance(history, list):
        for transition in reversed(history):
            state = transition.get("state") if isinstance(transition, Mapping) else None
            if state in _STAGES:
                return str(state)
    return "unknown"


def _identity_checks(row: Mapping[str, Any]) -> tuple[str, str]:
    identity = row.get("identity_context")
    identity = identity if isinstance(identity, Mapping) else {}
    fingerprint = str(row.get("auth_fingerprint_profile") or "").strip().lower()
    saved_fingerprint = str(identity.get("fingerprint_key") or "").strip().lower()
    device = str(row.get("device_id") or "").strip()
    saved_device = str(identity.get("device_id") or "").strip()
    fingerprint_match = (
        ("matched" if fingerprint == saved_fingerprint else "mismatch")
        if fingerprint and saved_fingerprint
        else "unknown"
    )
    device_match = ("matched" if device == saved_device else "mismatch") if device and saved_device else "unknown"
    return fingerprint_match, device_match


def _egress_check(row: Mapping[str, Any]) -> str:
    audit = row.get("proxy_audit")
    audit = audit if isinstance(audit, Mapping) else {}
    expected = str(audit.get("expected_country") or "").strip().upper()
    actual = str(audit.get("actual_country") or "").strip().upper()
    if expected and actual:
        return "matched" if expected == actual else "mismatch"
    return "unknown"


def _promotion_sources(promotion: Mapping[str, Any]) -> dict[str, int]:
    rows = promotion.get("results")
    if not isinstance(rows, list):
        return {}
    counts = Counter()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        probe = row.get("probe")
        source = str(probe.get("proxy_source") or "").strip().lower() if isinstance(probe, Mapping) else ""
        counts[source if source in _PROXY_SOURCES else "unknown"] += 1
    return dict(sorted(counts.items()))


def summarize_registration_funnel(
    results: Iterable[Mapping[str, Any] | None],
    *,
    attempted: int,
    promotion: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Report separate registration and trial denominators, never raw account data.

    A missing promotion report means *not measured*, not zero eligible accounts.
    Successful registration is counted from backend results, not from the
    number of saved session files or a payment-method probe.
    """
    rows = [row for row in results if isinstance(row, Mapping)]
    attempted = max(0, int(attempted))
    registered = sum(1 for row in rows if row.get("success"))
    failures = [row for row in rows if not row.get("success")]
    by_class = Counter()
    by_stage = Counter()
    for row in failures:
        code = str(row.get("failure_class") or "").strip().lower()
        if code not in _FAILURE_CLASSES:
            code = classify_error(row)
        by_class[code if code in _FAILURE_CLASSES else "unknown"] += 1
        by_stage[_failure_stage(row)] += 1
    fingerprint_checks: Counter[str] = Counter()
    device_checks: Counter[str] = Counter()
    egress_checks: Counter[str] = Counter()
    for row in rows:
        egress_checks[_egress_check(row)] += 1
        if row.get("success"):
            fingerprint, device = _identity_checks(row)
            fingerprint_checks[fingerprint] += 1
            device_checks[device] += 1

    checked = isinstance(promotion, Mapping)
    probe_total = max(0, int(promotion.get("total") or 0)) if checked else None
    probe_ok = max(0, int(promotion.get("success") or 0)) if checked else None
    eligible = max(0, int(promotion.get("trial_eligible") or 0)) if checked else None
    return {
        "attempted": attempted,
        "registered": registered,
        "registered_per_attempted": _rate(registered, attempted),
        "unreported": max(0, attempted - len(rows)),
        "registration_failures_by_class": dict(sorted(by_class.items())),
        "registration_failures_by_stage": dict(sorted(by_stage.items())),
        "egress_country_consistency": dict(sorted(egress_checks.items())),
        "fingerprint_consistency": dict(sorted(fingerprint_checks.items())),
        "device_consistency": dict(sorted(device_checks.items())),
        "edge_challenge": _edge_challenge_totals(rows),
        "promotion": {
            "checked": checked,
            "total": probe_total,
            "successful": probe_ok,
            "failed": max(0, probe_total - probe_ok) if checked else None,
            "successful_per_registered": _rate(probe_ok, registered) if checked else None,
            "unauthorized": max(0, int(promotion.get("unauthorized") or 0)) if checked else None,
            "transport_failed": max(0, int(promotion.get("transport_failed") or 0)) if checked else None,
            "trial_eligible": eligible,
            "eligible_per_successful_probe": _rate(eligible, probe_ok) if checked else None,
            "probe_proxy_sources": _promotion_sources(promotion) if checked else {},
        },
    }


def combine_registration_funnels(funnels: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Combine per-round safe counters without retaining account results."""
    rows = list(funnels)
    attempted = sum(int(row.get("attempted") or 0) for row in rows)
    registered = sum(int(row.get("registered") or 0) for row in rows)
    classes: Counter[str] = Counter()
    stages: Counter[str] = Counter()
    egress: Counter[str] = Counter()
    fingerprint: Counter[str] = Counter()
    devices: Counter[str] = Counter()
    for row in rows:
        classes.update(row.get("registration_failures_by_class") or {})
        stages.update(row.get("registration_failures_by_stage") or {})
        egress.update(row.get("egress_country_consistency") or {})
        fingerprint.update(row.get("fingerprint_consistency") or {})
        devices.update(row.get("device_consistency") or {})
    checked = [row["promotion"] for row in rows if (row.get("promotion") or {}).get("checked")]
    sources: Counter[str] = Counter()
    for row in checked:
        sources.update(row.get("probe_proxy_sources") or {})
    edge_hits = edge_rotations = edge_rotate_failed = edge_unknown = 0
    for row in rows:
        edge = row.get("edge_challenge") if isinstance(row.get("edge_challenge"), Mapping) else {}
        edge_hits += _count(edge.get("hits"))
        edge_unknown += _count(edge.get("unknown"))
        edge_rotations += _count(edge.get("rotations"))
        edge_rotate_failed += _count(edge.get("rotate_failed"))
    probe_total = sum(int(row.get("total") or 0) for row in checked) if checked else None
    probe_ok = sum(int(row.get("successful") or 0) for row in checked) if checked else None
    eligible = sum(int(row.get("trial_eligible") or 0) for row in checked) if checked else None
    return {
        "attempted": attempted,
        "registered": registered,
        "registered_per_attempted": _rate(registered, attempted),
        "unreported": sum(int(row.get("unreported") or 0) for row in rows),
        "registration_failures_by_class": dict(sorted(classes.items())),
        "registration_failures_by_stage": dict(sorted(stages.items())),
        "egress_country_consistency": dict(sorted(egress.items())),
        "fingerprint_consistency": dict(sorted(fingerprint.items())),
        "device_consistency": dict(sorted(devices.items())),
        "edge_challenge": {
            "hits": edge_hits,
            "unknown": edge_unknown,
            "rotations": edge_rotations,
            "rotate_failed": edge_rotate_failed,
        },
        "promotion": {
            "checked": bool(checked),
            "total": probe_total,
            "successful": probe_ok,
            "failed": max(0, probe_total - probe_ok) if checked else None,
            "successful_per_registered": _rate(probe_ok, registered) if checked else None,
            "unauthorized": sum(int(row.get("unauthorized") or 0) for row in checked) if checked else None,
            "transport_failed": sum(int(row.get("transport_failed") or 0) for row in checked) if checked else None,
            "trial_eligible": eligible,
            "eligible_per_successful_probe": _rate(eligible, probe_ok) if checked else None,
            "probe_proxy_sources": dict(sorted(sources.items())),
        },
    }


__all__ = ["summarize_registration_funnel", "combine_registration_funnels"]
