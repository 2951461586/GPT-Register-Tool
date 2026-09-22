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


# ---------------------------------------------------------------------------
# Payment-eligibility badge (2026-09-21)
#
# The desktop 优惠状态 column shows the promotion label with the account's
# available payment rails appended: ``可试用Plus-100% · card/upi/momo``.  The
# formatting rule lives here rather than in ``accounts/account_payment_eligibility``
# because this module is dependency-free and is already imported by both
# ``store/`` and ``desktop_read`` -- the eligibility module pulls the payment
# catalog in at import time, which the per-row desktop read path must not do.
# ---------------------------------------------------------------------------

# The badge is ~230px wide and truncates with an ellipsis (the full text is in
# the cell tooltip).  Cap the method list so a long Stripe list cannot push the
# promotion label out of view; the complete list stays in raw_json.
MAX_ELIGIBILITY_LABEL_TOKENS = 8

# Marker for "the probe ran and produced nothing".  Deliberately *not* blank:
# a blank suffix is indistinguishable from "never probed", and the failure mode
# here is a platform-side block (HTTP 400 "unusual activity" on
# ``/backend-api/payments/checkout``), so reading it as an account attribute is
# a wrong conclusion rather than a missing one.  See
# ``docs/audits/scan-2026-09-21-payment-eligibility-in-promotion-column.md`` §9.6.
PAYMENT_ELIGIBILITY_UNKNOWN_LABEL = "支付资格未知"


def payment_method_tokens(result: object) -> tuple[str, ...]:
    """Normalized method tokens in Stripe's own display order."""
    if not isinstance(result, dict):
        return ()
    methods = result.get("methods")
    if not isinstance(methods, (list, tuple)):
        methods = (
            result.get("ordered_payment_method_types")
            or result.get("payment_method_types")
            or result.get("custom_payment_methods")
            or ()
        )
    if not isinstance(methods, (list, tuple)):
        return ()
    tokens = [str(item or "").strip().lower() for item in methods]
    return tuple(token for token in dict.fromkeys(tokens) if token)


def payment_eligibility_is_unknown(result: object) -> bool:
    """True when a probe record exists but enumerated no payment method.

    Three states have to stay distinguishable in the 优惠状态 column:

    * **never probed** -- no ``payment_capability`` record at all, or the empty
      mapping ``safe_snapshot()`` writes for accounts the probe has not reached
      (``account_models`` always emits the key, so ``{}`` is the common case and
      must NOT be reported as a failure).
    * **probed, methods found** -- ``payment_method_tokens()`` is non-empty.
    * **probed, nothing found** -- a populated record whose method lists are
      empty.  That is this predicate, and it is rendered as
      :data:`PAYMENT_ELIGIBILITY_UNKNOWN_LABEL`.
    """
    if not isinstance(result, dict) or not result:
        return False
    return not payment_method_tokens(result)


def payment_eligibility_label(
    result: object,
    *,
    limit: int = MAX_ELIGIBILITY_LABEL_TOKENS,
) -> str:
    """Compact payment-rail badge text, e.g. ``card/upi/momo``.

    Returns an empty string when there is nothing to say -- i.e. when the
    account was never probed -- so callers can join unconditionally without
    producing a dangling separator.  A probe that ran but found nothing returns
    :data:`PAYMENT_ELIGIBILITY_UNKNOWN_LABEL` instead of an empty string: the
    column has to say "unknown" rather than silently look unprobed.
    """
    tokens = payment_method_tokens(result)
    if not tokens:
        return PAYMENT_ELIGIBILITY_UNKNOWN_LABEL if payment_eligibility_is_unknown(result) else ""
    cap = max(1, int(limit or MAX_ELIGIBILITY_LABEL_TOKENS))
    if len(tokens) <= cap:
        return "/".join(tokens)
    return "/".join(tokens[:cap]) + f"+{len(tokens) - cap}"


def promotion_status_with_eligibility(
    promotion_status: str,
    payment_eligibility: str,
    *,
    separator: str = " · ",
) -> str:
    """Join the promotion badge and the payment-rail badge for one column.

    Single owner of the composition rule so the desktop grid, the detail panel
    and the CLI summary cannot drift apart.
    """
    promotion = str(promotion_status or "").strip()
    eligibility = str(payment_eligibility or "").strip()
    if promotion and eligibility:
        return f"{promotion}{separator}{eligibility}"
    return promotion or eligibility


__all__ = [
    "PROMOTION_STATE_TRIAL_ELIGIBLE",
    "PROMOTION_STATE_SUBSCRIBED",
    "PROMOTION_STATE_FREE",
    "PROMOTION_STATE_AUTH_INVALID",
    "PROMOTION_STATE_PROBE_FAILED",
    "PROMOTION_STATE_UNKNOWN",
    "AUTH_INVALID_LABEL",
    "MAX_ELIGIBILITY_LABEL_TOKENS",
    "PAYMENT_ELIGIBILITY_UNKNOWN_LABEL",
    "payment_eligibility_is_unknown",
    "payment_eligibility_label",
    "payment_method_tokens",
    "promotion_marker_is_stale",
    "promotion_status_with_eligibility",
]
