"""Email OTP request strategy for auth.openai.com.

The OTP seam is deliberately deeper than one endpoint call: it owns the
passwordless resend/send ordering, the "pre-sent OTP" fallback, and the JSON
request shape so registration code does not duplicate state-sensitive details.
"""

import json

from .config import CFG
from .auth_headers import auth_impersonate, openai_auth_headers
from .auth_flow import _absolute_url, _invalid_state_auth_response, _json_or_raw
from .http_client import request_with_retry


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
            "impersonate": auth_impersonate(),
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
