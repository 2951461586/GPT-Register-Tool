from .config import CFG
from .codex_sentinel import load_cached_sentinel, with_sentinel


def complete_phone_verification(session, did, current_url, proxy=None, enabled=False):
    if not enabled:
        return {
            "ok": False,
            "error": "add_phone_required",
            "message": "OpenAI requested phone verification; automatic phone handling is disabled.",
        }

    try:
        from .paypal_auto import _pick_phone_and_sms, _sms_baseline, _poll_sms_code
    except Exception as exc:
        return {"ok": False, "error": f"phone_helpers_unavailable:{exc}"}

    sms_cfg = CFG.get("paypal_auto") if isinstance(CFG.get("paypal_auto"), dict) else {}
    phone, sms_api_url = _pick_phone_and_sms(sms_cfg)
    phone = str(phone or "").strip()
    sms_api_url = str(sms_api_url or "").strip()
    if not phone or not sms_api_url:
        return {"ok": False, "error": "phone_sms_config_missing"}

    baseline = _sms_baseline(sms_api_url)
    sentinel = load_cached_sentinel()
    send_resp = session.post(
        "https://auth.openai.com/api/accounts/add-phone/send",
        headers=with_sentinel(
            _oai_headers(did, {"Referer": current_url, "content-type": "application/json"}),
            sentinel,
        ),
        json={"phone_number": phone},
        timeout=30,
        impersonate="chrome110",
    )
    if send_resp.status_code != 200:
        return {
            "ok": False,
            "error": f"phone_send_failed:{send_resp.status_code}",
            "body": send_resp.text[:300],
        }

    code = _poll_sms_code(
        sms_api_url,
        baseline,
        timeout=int(sms_cfg.get("sms_timeout", 120)),
        poll_interval=int(sms_cfg.get("sms_poll_interval", 5)),
    )
    if not code:
        return {"ok": False, "error": "phone_sms_timeout"}

    validate = session.post(
        "https://auth.openai.com/api/accounts/phone-otp/validate",
        headers=with_sentinel(
            _oai_headers(did, {"Referer": "https://auth.openai.com/phone-verification", "content-type": "application/json"}),
            sentinel,
        ),
        json={"code": code},
        timeout=30,
        impersonate="chrome110",
    )
    if validate.status_code != 200:
        return {
            "ok": False,
            "error": f"phone_validate_failed:{validate.status_code}",
            "body": validate.text[:300],
        }
    return {"ok": True, "next_url": _next_url(validate)}


def _oai_headers(did, extra=None):
    headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/110.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Google Chrome";v="110", "Chromium";v="110", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "oai-device-id": did,
    }
    if extra:
        headers.update(extra)
    return headers


def _next_url(response):
    try:
        body = response.json()
    except Exception:
        body = {}
    return body.get("continue_url") or response.headers.get("Location") or response.url
