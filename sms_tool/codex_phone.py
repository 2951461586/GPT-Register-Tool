"""Phone verification integration for ChatGPT registration.

Every number comes from a provider pool built by
:func:`phone_reuse.create_phone_pool`. The single-phone fallback that read
``paypal_auto.phone_number`` / ``sms_api_url`` was removed on 2026-09-22 along
with the rest of the static phone-pool mode: it bypassed the rental lifecycle,
so nothing ever completed or cancelled an activation, and the number kept
billing after the registration had moved on.
"""

from .codex_sentinel import load_cached_sentinel


def complete_phone_verification(session, did, current_url, proxy=None, enabled=False, phone_pool=None):
    """Complete phone verification during registration.

    ``phone_pool`` is a ``PhonePool`` from :mod:`phone_reuse`. When it is absent
    while ``enabled`` is set, the caller asked for phone handling without giving
    it a number source -- a configuration error, reported as one rather than
    silently degrading to a no-op.
    """
    if phone_pool:
        return _verify_with_reuse_pool(session, did, current_url, phone_pool, proxy=proxy)

    if not enabled:
        return {
            "ok": False,
            "error": "add_phone_required",
            "message": "OpenAI requested phone verification; automatic phone handling is disabled.",
        }

    return {
        "ok": False,
        "error": "phone_pool_unavailable",
        "message": (
            "Phone verification is enabled but no provider pool was built. "
            "Set phone_reuse.source and that provider's api_key, e.g. "
            "phone_reuse.smsbower.api_key or SMSBOWER_API_KEY."
        ),
    }


def _verify_with_reuse_pool(session, did, current_url, phone_pool, proxy=None):
    """Phone verification using the reuse pool from phone_reuse.py."""
    from .phone_reuse import complete_phone_verification_with_reuse

    sentinel = load_cached_sentinel()
    result = complete_phone_verification_with_reuse(
        session=session,
        did=did,
        current_url=current_url,
        phone_pool=phone_pool,
        sentinel=sentinel,
        proxy=proxy,
    )

    if result.get("ok"):
        return {
            "ok": True,
            "next_url": result.get("next_url", ""),
            "phone": result.get("phone", ""),
            "provider": result.get("provider", ""),
            "activation_id": result.get("activation_id", ""),
            "reuse_count": result.get("reuse_count", 0),
            "max_reuse_count": result.get("max_reuse_count", 0),
            "remaining": result.get("remaining", 0),
        }

    return {
        "ok": False,
        "error": result.get("error", "phone_verification_failed"),
        "phone": result.get("phone", ""),
        "body": result.get("body", ""),
        "message": result.get("message", ""),
    }
