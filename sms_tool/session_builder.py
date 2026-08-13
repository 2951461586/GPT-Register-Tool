"""Session JSON builder — assembles the final saved session file from a registration result dict.

Responsibility: take the raw registration result dict (as produced by
``registration.run_email``) and normalize it into the canonical session JSON
shape consumed by the rest of the toolchain (CPA import, quota refresh, etc.).
"""
import time


def _extract_nested(data, *keys):
    """Walk a dict by string keys, returning the leaf as a string (or '')."""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current if isinstance(current, str) else ""


def build_session_file(data):
    """Build the canonical session-file dict from a registration result.

    ``data`` is the dict returned by ``registration.run_email`` (or its phone
    equivalents).  Keys that may live at several levels (``access_token``,
    ``refresh_token``, …) are resolved against a priority chain so callers
    don't have to normalise beforehand.
    """
    mailbox = data.get("mailbox") or {}
    response = data.get("response") or {}
    auth_session = data.get("auth_session") or response.get("auth_session") or {}
    paypal = data.get("paypal") or {}

    # --- token resolution (priority: data → response → nested → auth_session) ---
    session_token = (
        data.get("session_token")
        or response.get("session_token")
        or response.get("sessionToken")
        or _extract_nested(response, "session", "session_token")
        or auth_session.get("sessionToken")
        or auth_session.get("session_token")
    )
    access_token = (
        data.get("access_token")
        or response.get("access_token")
        or response.get("accessToken")
        or _extract_nested(response, "session", "access_token")
        or auth_session.get("accessToken")
        or auth_session.get("access_token")
    )
    id_token = (
        data.get("id_token")
        or data.get("idToken")
        or auth_session.get("idToken")
        or auth_session.get("id_token")
        or _extract_nested(auth_session, "session", "id_token")
        or _extract_nested(auth_session, "session", "idToken")
    )
    refresh_token = (
        data.get("refresh_token")
        or response.get("refresh_token")
        or response.get("refreshToken")
        or mailbox.get("refresh_token")
    )
    oauth_refresh_token = (
        data.get("oauth_refresh_token")
        or auth_session.get("refreshToken")
        or auth_session.get("refresh_token")
        or _extract_nested(auth_session, "session", "refresh_token")
        or _extract_nested(auth_session, "session", "refreshToken")
    )

    # --- derived paypal / refresh metadata ---
    paypal_status = (
        data.get("paypal_status")
        or paypal.get("paypal_status")
        or ("qr_ready" if paypal.get("ok") and (paypal.get("qr_path") or paypal.get("qr_data")) else "")
        or paypal.get("status")
        or ("pm_created" if paypal.get("ok") and str(paypal.get("pm_id") or "").startswith("pm_") else "")
        or ("link_ready" if paypal.get("url") else "")
    )
    payment_method = (
        data.get("payment_method")
        or paypal.get("payment_method")
        or paypal.get("method")
        or ("paypal" if (paypal.get("url") or paypal.get("pm_id")) else "")
    )
    refresh_token_status = data.get("refresh_token_status") or (
        "oauth_present" if oauth_refresh_token else ("legacy_present" if refresh_token else "no_rt")
    )

    purchase = {
        "source": mailbox.get("source", ""),
        "provider": mailbox.get("provider", ""),
        "email": mailbox.get("email", ""),
        "purchase_id": mailbox.get("purchase_id", ""),
        "project_name": mailbox.get("project_name", ""),
        "price": mailbox.get("price", ""),
        "total_cost": mailbox.get("purchase_total_cost", ""),
        "balance_after": mailbox.get("balance_after", ""),
    }
    purchase = {key: value for key, value in purchase.items() if value}

    return {
        "email": data.get("email") or mailbox.get("email") or "",
        "phone": data.get("phone", ""),
        "password": data.get("password", ""),
        "session_token": session_token or "",
        "access_token": access_token or "",
        "id_token": id_token or "",
        "refresh_token": refresh_token or "",
        "device_id": data.get("device_id") or response.get("device_id") or "",
        "auth_session_logging_id": data.get("auth_session_logging_id") or "",
        "cookie_header": data.get("cookie_header") or response.get("cookie_header") or "",
        "auth_session": auth_session,
        "paypal": paypal,
        "payment_method": payment_method,
        "paypal_status": paypal_status,
        "registration_mode": data.get("registration_mode", ""),
        "oauth_refresh_token": oauth_refresh_token or "",
        "refresh_token_status": refresh_token_status,
        "quota_status": data.get("quota_status", ""),
        "quota": data.get("quota") or {},
        "registration_success_basis": data.get("registration_success_basis", ""),
        "registration_warning": data.get("registration_warning", ""),
        "registration_country": data.get("registration_country", ""),
        "auth_fingerprint_profile": data.get("auth_fingerprint_profile", ""),
        "sentinel_version": data.get("sentinel_version", ""),
        "access_token_telemetry": data.get("access_token_telemetry") or {},
        "post_registration_ready": data.get("post_registration_ready"),
        "timing": data.get("timing") or {},
        "pipeline_timing": data.get("pipeline_timing") or {},
        "totp_secret": data.get("totp_secret", ""),
        "twofa_enrolled_at": data.get("twofa_enrolled_at", 0),
        "twofa_enroll_error": (
            data.get("twofa_enrollment", {}).get("error", "")
            if isinstance(data.get("twofa_enrollment"), dict)
            else ""
        ),
        "purchase": data.get("purchase") or purchase,
        "mailbox": {
            "email": mailbox.get("email", ""),
            "password": mailbox.get("password", ""),
            "refresh_token": mailbox.get("refresh_token", ""),
            "access_token": mailbox.get("access_token", ""),
            "source": mailbox.get("source", ""),
            "provider": mailbox.get("provider", ""),
            "order_no": mailbox.get("order_no", ""),
            "token": mailbox.get("token", ""),
            "purchase_id": mailbox.get("purchase_id", ""),
            "project_name": mailbox.get("project_name", ""),
            "price": mailbox.get("price", ""),
            "purchase_total_cost": mailbox.get("purchase_total_cost", ""),
            "balance_after": mailbox.get("balance_after", ""),
        } if mailbox else {},
        "created_at": int(time.time()),
    }
