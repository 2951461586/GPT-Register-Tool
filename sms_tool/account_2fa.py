"""2FA (TOTP) auto-enrollment for freshly registered ChatGPT accounts.

Ported & adapted from gpt-free-register (core/account_export.py).

Flow (7 steps, all protocol-level):
  1. Re-auth (reauth=password via email OTP)
  2. Exchange fresh accessToken with updated pwd_auth_time
  3. Enroll TOTP on /backend-api/accounts/mfa/enroll
  4. Activate enrollment with TOTP code
  5. Persist totp_secret into accounts table

Uses the SAME BrowserSession/cookie-jar as registration (same UA, same IP, same
device_id) — avoiding association signals.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def _trigger_password_reauth(session, csrf_token: str, email: str, did: str, base_headers: dict) -> str:
    """POST /api/auth/signin/openai?reauth=password → triggers email OTP dispatch."""
    import urllib.parse
    auth_base = "https://auth.openai.com"
    query = {
        "connection": "password",
        "login_hint": email,
        "reauth": "password",
        "max_age": "0",
        "ext-oai-did": did,
    }
    url = f"{auth_base}/api/auth/signin/openai?{urllib.parse.urlencode(query)}"
    headers = dict(base_headers)
    headers["content-type"] = "application/x-www-form-urlencoded"
    headers["origin"] = "https://chatgpt.com"
    body = urllib.parse.urlencode({
        "callbackUrl": "https://chatgpt.com/?action=enable&factor=totp",
        "csrfToken": csrf_token,
        "json": "true",
    })
    r = session.post(url, headers=headers, data=body)
    r.raise_for_status()
    data = r.json()
    auth_url = data.get("url")
    if not auth_url:
        raise RuntimeError(f"reauth signin/openai no url: {r.text[:300]}")
    return auth_url


def _follow_reauth_authorize(session, auth_url: str, base_headers: dict) -> None:
    headers = dict(base_headers)
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    headers["Referer"] = "https://chatgpt.com/"
    headers["sec-fetch-mode"] = "navigate"
    headers["sec-fetch-dest"] = "document"
    r = session.get(auth_url, headers=headers, allow_redirects=True)
    r.raise_for_status()


def _fetch_csrf_token(session, base_headers: dict) -> str:
    url = "https://chatgpt.com/api/auth/csrf"
    headers = dict(base_headers)
    headers["Accept"] = "application/json"
    r = session.get(url, headers=headers)
    r.raise_for_status()
    return r.json()["csrfToken"]


def _validate_email_otp(session, code: str, base_headers: dict) -> str:
    """POST auth.openai.com/api/accounts/email-otp/validate → continue_url"""
    url = "https://auth.openai.com/api/accounts/email-otp/validate"
    headers = dict(base_headers)
    headers["content-type"] = "application/json"
    headers["origin"] = "https://auth.openai.com"
    headers["referer"] = "https://auth.openai.com/email-verification"
    body = json.dumps({"code": code})
    r = session.post(url, headers=headers, data=body)
    r.raise_for_status()
    data = r.json()
    continue_url = data.get("continue_url")
    if not continue_url:
        raise RuntimeError(f"email-otp validate no continue_url: {data}")
    return continue_url


def _exchange_token_after_reauth(session, continue_url: str, base_headers: dict) -> str:
    """Follow continue_url → refresh session token → fetch new accessToken."""
    headers = dict(base_headers)
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    headers["Referer"] = "https://auth.openai.com/email-verification"
    headers["sec-fetch-mode"] = "navigate"
    headers["sec-fetch-dest"] = "document"
    session.get(continue_url, headers=headers, allow_redirects=True)

    # Fetch fresh session
    chat_base = "https://chatgpt.com"
    url = f"{chat_base}/api/auth/session"
    headers2 = dict(base_headers)
    headers2["Accept"] = "application/json"
    headers2["Referer"] = f"{chat_base}/"
    r = session.get(url, headers=headers2)
    r.raise_for_status()
    data = r.json()
    access_token = data.get("accessToken")
    if not access_token:
        raise RuntimeError(f"no accessToken after reauth exchange: {data}")
    return access_token


def _enroll_totp(session, access_token: str, did: str, base_headers: dict) -> tuple[str, str]:
    """POST /backend-api/accounts/mfa/enroll → {secret, session_id}"""
    url = "https://chatgpt.com/backend-api/accounts/mfa/enroll"
    headers = dict(base_headers)
    headers["authorization"] = f"Bearer {access_token}"
    headers["oai-device-id"] = did
    headers["oai-language"] = "en-US"
    headers["content-type"] = "application/json"
    headers["referer"] = "https://chatgpt.com/"
    body = json.dumps({"factor_type": "totp"})
    r = session.post(url, headers=headers, data=body)
    if r.status_code != 200:
        raise RuntimeError(f"mfa enroll HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    secret = data.get("secret")
    session_id = data.get("session_id")
    if not secret or not session_id:
        raise RuntimeError(f"mfa enroll missing fields: {data}")
    return secret, session_id


def _activate_totp(session, access_token: str, did: str, secret: str, session_id: str, base_headers: dict) -> None:
    """POST /backend-api/accounts/mfa/user/activate_enrollment with current TOTP code."""
    try:
        import pyotp
    except ImportError as e:
        raise ImportError("pyotp is required for TOTP activation. Install: pip install pyotp") from e

    url = "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment"
    headers = dict(base_headers)
    headers["authorization"] = f"Bearer {access_token}"
    headers["oai-device-id"] = did
    headers["oai-language"] = "en-US"
    headers["content-type"] = "application/json"
    headers["referer"] = "https://chatgpt.com/"
    totp_code = pyotp.TOTP(secret).now()
    body = json.dumps({"code": totp_code, "factor_type": "totp", "session_id": session_id})
    r = session.post(url, headers=headers, data=body)
    if r.status_code != 200:
        raise RuntimeError(f"mfa activate HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"mfa activate failed: {data}")


def setup_totp_2fa(
    session,
    email: str,
    access_token: str,
    did: str,
    base_headers: dict,
    poll_otp_fn,
) -> dict[str, Any]:
    """Run full 2FA enrollment. Returns result dict with totp_secret on success.

    Args:
        session:  active requests.Session (cookie jar from registration)
        email:    account email
        access_token:  fresh account access token
        did:      device id (oai-did)
        base_headers:  headers template for chatgpt.com
        poll_otp_fn(email, issued_after_unix, timeout, excluded) -> OTP str
            Callback to poll email OTP. Re-uses existing mailbox polling.

    Returns:
        {"ok": True, "totp_secret": "JBSWY3DPEHPK3PXP", ...}
        or
        {"ok": False, "error": "..."}
    """
    try:
        import pyotp  # noqa: F401 — early import for clear error message
    except ImportError as e:
        return {"ok": False, "error": "pyotp_missing", "detail": str(e)}

    result: dict[str, Any] = {"ok": False, "factor": "totp", "email": email}

    try:
        logger.info("=" * 60)
        logger.info(f"[2FA] Starting TOTP enrollment for {email}")
        logger.info("=" * 60)

        # --- Step 1: Re-auth trigger ---
        logger.info("[2FA] Triggering password reauth...")
        csrf_token = _fetch_csrf_token(session, base_headers)
        time.sleep(1)
        auth_url = _trigger_password_reauth(session, csrf_token, email, did, base_headers)
        time.sleep(1)

        # --- Step 2: Follow authorize URL (triggers OTP email) ---
        logger.info("[2FA] Following auth URL (triggers re-auth OTP email)...")
        _follow_reauth_authorize(session, auth_url, base_headers)
        time.sleep(2)

        # --- Step 3: Poll OTP ---
        logger.info("[2FA] Polling re-auth OTP from mailbox...")
        reauth_otp_after_ts = time.time()
        otp_code = poll_otp_fn(
            email,
            issued_after_unix=reauth_otp_after_ts,
            timeout=120,
        )
        if not otp_code:
            raise RuntimeError("reauth_otp_poll_timeout")
        logger.info(f"[2FA] Got OTP: {otp_code}")

        # --- Step 4: Validate OTP → continue_url ---
        logger.info("[2FA] Validating email OTP...")
        continue_url = _validate_email_otp(session, otp_code, base_headers)
        time.sleep(0.5)

        # --- Step 5: Exchange for fresh token ---
        logger.info("[2FA] Exchanging token after reauth...")
        access_token = _exchange_token_after_reauth(session, continue_url, base_headers)
        time.sleep(0.5)

        # --- Step 6: Enroll TOTP ---
        logger.info("[2FA] Enrolling TOTP...")
        secret, session_id = _enroll_totp(session, access_token, did, base_headers)
        time.sleep(0.5)

        # --- Step 7: Activate ---
        logger.info("[2FA] Activating TOTP enrollment...")
        _activate_totp(session, access_token, did, secret, session_id, base_headers)
        time.sleep(0.3)

        logger.info("=" * 60)
        logger.info(f"[2FA] ✅ TOTP enrolled successfully for {email}")
        logger.info("=" * 60)

        result["ok"] = True
        result["totp_secret"] = secret
        result["access_token"] = access_token  # return the refreshed token
    except Exception as e:
        logger.error(f"[2FA] ❌ Failed: {e}")
        result["error"] = str(e)
    return result


def totp_now(secret: str) -> str:
    """Generate current 6-digit TOTP code from secret."""
    import pyotp
    return pyotp.TOTP(secret).now()
