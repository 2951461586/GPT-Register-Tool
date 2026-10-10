"""TOTP challenge completion after email-OTP verification."""

from __future__ import annotations

from . import deps, steps


def _complete_existing_login_totp(
    session,
    auth_base,
    base_headers,
    payload,
    *,
    did,
    sentinel_token="",
    sentinel_so_token="",
    totp_secret="",
):
    """Complete a saved TOTP challenge after email OTP verification."""
    if not _is_mfa_challenge_payload(payload):
        return {"ok": True, "data": payload}
    secret = str(totp_secret or "").strip()
    if not secret:
        return {"ok": False, "error": "existing_login_totp_secret_missing"}
    factor_id = _totp_factor_id(payload)
    if not factor_id:
        return {"ok": False, "error": "existing_login_totp_factor_missing"}
    try:
        import pyotp

        code = pyotp.TOTP(secret).now()
    except Exception:
        return {"ok": False, "error": "existing_login_totp_code_failed"}

    referer = _response_next_url_from_data(payload, auth_base) or f"{auth_base}/mfa-challenge/{factor_id}"
    headers = steps._auth_request_headers(
        base_headers,
        did=did,
        referer=referer,
        origin=auth_base,
        sentinel_token=sentinel_token,
        sentinel_so_token=sentinel_so_token,
        extra={"Content-Type": "application/json"},
    )
    issue = deps.request_with_retry(
        session,
        "post",
        f"{auth_base}/api/accounts/mfa/issue_challenge",
        label="Existing account TOTP challenge",
        json={"type": "totp", "id": factor_id, "force_fresh_challenge": False},
        headers=headers,
        impersonate=deps.auth_impersonate(),
    )
    if issue.status_code not in (200, 201, 202, 204):
        return {"ok": False, "error": f"existing_login_totp_issue_failed:{issue.status_code}"}
    verify = deps.request_with_retry(
        session,
        "post",
        f"{auth_base}/api/accounts/mfa/verify",
        label="Existing account TOTP verify",
        json={"type": "totp", "id": factor_id, "code": code},
        headers=headers,
        impersonate=deps.auth_impersonate(),
    )
    verify_data = deps._json_or_raw(verify, limit=1000)
    if verify.status_code != 200:
        return {"ok": False, "error": f"existing_login_totp_verify_failed:{verify.status_code}"}
    return {"ok": True, "data": verify_data}


def _is_mfa_challenge_payload(payload):
    if not isinstance(payload, dict):
        return False
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    if str(page.get("type") or "").strip().lower() == "mfa_challenge":
        return True
    return "/mfa-challenge/" in str(_response_next_url_from_data(payload, "") or "").lower()


def _totp_factor_id(payload):
    auth_session = payload.get("oai-client-auth-session") if isinstance(payload, dict) else {}
    if not isinstance(auth_session, dict):
        return ""
    factors = []
    for key in ("mfa_challenge_factors", "mfa_factors"):
        values = auth_session.get(key)
        if isinstance(values, list):
            factors.extend(item for item in values if isinstance(item, dict))
    for factor in factors:
        if str(factor.get("factor_type") or "").strip().lower() == "totp":
            factor_id = str(factor.get("id") or "").strip()
            if factor_id:
                return factor_id
    return ""


def _response_next_url_from_data(payload, auth_base):
    if not isinstance(payload, dict):
        return ""
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    page_payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
    value = str(payload.get("continue_url") or page_payload.get("url") or "").strip()
    return deps._absolute_url(auth_base, value) if value and auth_base else value
