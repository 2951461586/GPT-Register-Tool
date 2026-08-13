"""Email OTP request strategy for auth.openai.com.

The OTP seam is deliberately deeper than one endpoint call: it owns the
passwordless resend/send ordering, the "pre-sent OTP" fallback, and the JSON
request shape so registration code does not duplicate state-sensitive details.
"""

import json
from collections.abc import Mapping

from .config import CFG, current_config_data
from .auth_headers import AUTH_IMPERSONATE, openai_auth_headers
from .auth_flow import _absolute_url, _invalid_state_auth_response, _json_or_raw
from .http_client import request_with_retry
from .mailbox import _poll_email_otp
from .registration_progress import registration_stage


class SyntheticResponse:
    def __init__(self, status_code=204, body=None, url=""):
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body, ensure_ascii=False)
        self.url = url
        self.headers = {}

    def json(self):
        return self._body


def otp_fallback_send_enabled():
    cfg = CFG.get("email_registration") if isinstance(CFG.get("email_registration"), dict) else {}
    value = cfg.get("otp_fallback_send_on_resend_failure", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def send_registration_email_otp(session, auth_base, base_headers, current_url="", mode="passwordless"):
    referer = current_url if str(current_url or "").startswith(auth_base) else f"{auth_base}/email-verification"
    did = str((base_headers or {}).get("oai-device-id") or (base_headers or {}).get("Oai-Device-Id") or "").strip()
    headers = {
        **(base_headers or {}),
        **openai_auth_headers(did, referer=referer, origin=auth_base, accept="*/*"),
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if mode == "passwordless":
        endpoints = [("/api/accounts/email-otp/resend", {})]
        if otp_fallback_send_enabled():
            endpoints.extend([
                ("/api/accounts/passwordless/send-otp", {}),
                ("/api/accounts/email-otp/send", {}),
            ])
    else:
        endpoints = [
            ("/api/accounts/email-otp/send", {}),
            ("/api/accounts/email-otp/resend", {}),
        ]
    last = None
    for endpoint, payload in endpoints:
        kwargs = {
            "headers": headers,
            # Registration preflight validates that the configured profile is
            # supported before this stage consumes a mailbox.
            "impersonate": AUTH_IMPERSONATE,
        }
        if payload is not None:
            kwargs["json"] = payload
            kwargs["headers"] = {**headers, "Content-Type": "application/json"}
        response = request_with_retry(
            session,
            "post",
            _absolute_url(auth_base, endpoint),
            label=f"Email OTP {endpoint}",
            **kwargs,
        )
        print(f"  Email OTP {endpoint}: {response.status_code}")
        last = response
        if response.status_code in (200, 202, 204):
            return response
        body = _json_or_raw(response, limit=500)
        print(f"    Response: {json.dumps(body, ensure_ascii=False)[:500]}")
        if mode == "passwordless" and _invalid_state_auth_response(body):
            return response
        if mode == "passwordless" and endpoint.endswith("/resend") and response.status_code in (400, 404, 405):
            if otp_fallback_send_enabled():
                print("    Resend was not accepted; trying opt-in fallback OTP send")
                continue
            print("    Resend was not accepted; preserving current auth state and polling for pre-sent OTP")
            return SyntheticResponse(
                204,
                {"assumed_pre_sent": True, "resend_status": response.status_code, "resend_body": body},
                url=_absolute_url(auth_base, endpoint),
            )
        if response.status_code not in (404, 405):
            return response
    return last


def _poll_registration_email_otp(
    mailbox,
    *,
    subject_keyword,
    timeout,
    issued_after_unix,
    proxy=None,
    excluded_otps=None,
    resend_callback=None,
    resend_after_seconds=None,
    poll_otp_fn=None,
):
    poll_otp_fn = poll_otp_fn or _poll_email_otp
    total_timeout = max(0, int(timeout or 0))
    provider = str(getattr(mailbox, "provider", "") or "").strip().lower()
    if provider != "remail" or resend_callback is None:
        return poll_otp_fn(
            mailbox,
            subject_keyword=subject_keyword,
            timeout=total_timeout,
            issued_after_unix=issued_after_unix,
            proxy=proxy,
            excluded_otps=excluded_otps,
        )
    if resend_after_seconds is None:
        value = current_config_data().get("email_registration")
        email_cfg = value if isinstance(value, Mapping) else {}
        resend_after_seconds = email_cfg.get("remail_otp_resend_after_seconds", 30)
    try:
        first_window = max(0, int(resend_after_seconds or 0))
    except (TypeError, ValueError):
        first_window = 30
    if first_window <= 0 or first_window >= total_timeout:
        return _poll_email_otp(
            mailbox,
            subject_keyword=subject_keyword,
            timeout=total_timeout,
            issued_after_unix=issued_after_unix,
            proxy=proxy,
            excluded_otps=excluded_otps,
        )
    code = poll_otp_fn(
        mailbox,
        subject_keyword=subject_keyword,
        timeout=first_window,
        issued_after_unix=issued_after_unix,
        proxy=proxy,
        excluded_otps=excluded_otps,
    )
    if code:
        return code
    registration_stage("email_otp_resend")
    try:
        response = resend_callback()
        status = int(getattr(response, "status_code", 0) or 0)
        if status not in (200, 202, 204, 409):
            print(f"  ReMail OTP resend was not accepted: {status}")
    except Exception as exc:
        print(f"  ReMail OTP resend warning: {exc}")
    registration_stage("email_otp_wait")
    return poll_otp_fn(
        mailbox,
        subject_keyword=subject_keyword,
        timeout=total_timeout - first_window,
        issued_after_unix=issued_after_unix,
        proxy=proxy,
        excluded_otps=excluded_otps,
    )
