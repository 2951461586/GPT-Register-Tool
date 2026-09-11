"""Machine-readable 优惠状态 (promotion state) vocabulary.

The Chinese badge text produced by ``account_promotion.promotion_status_label``
is display copy. Everything that filters, sorts, branches or persists on the
promotion status keys off the stable ASCII states defined here -- a label
reword must never change behaviour again (2026-09-12 scan: the desktop
substring-matched "可试用"+"plus" until this module existed).

Dependency-free by design: ``store/`` (boundary rule 2) imports it alongside
``account_promotion``, ``desktop_read`` and the cross-language contract tests.
"""

from __future__ import annotations

PROMOTION_STATE_TRIAL_ELIGIBLE = "trial_eligible"
PROMOTION_STATE_SUBSCRIBED = "subscribed"
PROMOTION_STATE_FREE = "free"
PROMOTION_STATE_AUTH_INVALID = "auth_invalid"
PROMOTION_STATE_PROBE_FAILED = "probe_failed"
PROMOTION_STATE_UNKNOWN = "unknown"

# Legacy display label for an auth-failed promotion probe. Kept only as a
# fallback for records written before ``promotion_state`` existed.
AUTH_INVALID_LABEL = "AT失效"


def promotion_marker_is_stale(
    promotion_status: str = "",
    promotion_state: str = "",
    at_probe_status_code: object = "",
) -> bool:
    """True when a promotion auth-failure marker predates a verified AT.

    A promotion probe that recorded ``auth_invalid`` (legacy label
    ``AT失效``) describes the access token that existed at probe time. Once a
    later liveness probe returns HTTP 200 with a replacement token, the marker
    is stale and must not surface in the 优惠状态 column. Single owner of that
    rule: ``desktop_read`` applies it at display time and
    ``account_recovery._mark_successful_relogin`` at persistence time used to
    re-encode it independently.
    """
    if str(at_probe_status_code or "").strip() != "200":
        return False
    state = str(promotion_state or "").strip().lower()
    if state:
        return state == PROMOTION_STATE_AUTH_INVALID
    return str(promotion_status or "").strip() == AUTH_INVALID_LABEL


__all__ = [
    "PROMOTION_STATE_TRIAL_ELIGIBLE",
    "PROMOTION_STATE_SUBSCRIBED",
    "PROMOTION_STATE_FREE",
    "PROMOTION_STATE_AUTH_INVALID",
    "PROMOTION_STATE_PROBE_FAILED",
    "PROMOTION_STATE_UNKNOWN",
    "AUTH_INVALID_LABEL",
    "promotion_marker_is_stale",
]
