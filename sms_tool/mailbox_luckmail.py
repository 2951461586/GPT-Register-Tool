from datetime import datetime

from curl_cffi import requests as curl_requests

from .config import CFG
from .mailbox_types import MailboxAccount
from .providers.luckmail_token import LuckMailTokenClient
from .mail_otp import _email_otp_candidate


def _email_cfg():
    return CFG.get("email_registration", {})


def _luckmail_headers():
    api_key = (_email_cfg().get("luckmail_api_key") or "").strip()
    if not api_key:
        raise RuntimeError("email_registration.luckmail_api_key is required")
    return {"X-API-Key": api_key, "Accept": "application/json", "Content-Type": "application/json"}


def _luckmail_url(path):
    base_url = (_email_cfg().get("luckmail_base_url") or "https://mails.luckyous.com").rstrip("/")
    return base_url + path


def _luckmail_token_client():
    return LuckMailTokenClient(
        _email_cfg().get("luckmail_base_url", "https://mails.luckyous.com"),
        _email_cfg().get("luckmail_api_key", ""),
        timeout=30,
        verify_tls=False,
    )


def _luckmail_token_code(mailbox):
    token = getattr(mailbox, "token", "")
    if not token:
        raise RuntimeError("LuckMail purchased mailbox missing token")
    return _luckmail_token_client().code(token)


def _luckmail_token_mails(mailbox):
    token = getattr(mailbox, "token", "")
    if not token:
        raise RuntimeError("LuckMail purchased mailbox missing token")
    return _luckmail_token_client().mails(token)


def _luckmail_token_alive(mailbox):
    token = getattr(mailbox, "token", "")
    if not token:
        raise RuntimeError("LuckMail purchased mailbox missing token")
    return _luckmail_token_client().alive(token)


def _luckmail_token_email(token):
    if not token:
        return ""
    return _luckmail_token_client().resolve_email(token)


def _latest_luckmail_message(data):
    data = data or {}
    latest = data.get("mail") or data.get("latest_mail") or {}
    if latest:
        return latest
    mails = data.get("mails") or []
    return mails[0] if isinstance(mails, list) and mails and isinstance(mails[0], dict) else {}


def _latest_luckmail_message_id(data):
    latest = _latest_luckmail_message(data)
    return str(latest.get("message_id") or latest.get("id") or "").strip()


def _snapshot_luckmail_token_message(mailbox):
    if getattr(mailbox, "provider", "") != "luckmail_token":
        return ""
    try:
        data = (_luckmail_token_code(mailbox).get("data") or {})
        message_id = _latest_luckmail_message_id(data)
        if not message_id:
            data = (_luckmail_token_mails(mailbox).get("data") or {})
            message_id = _latest_luckmail_message_id(data)
        mailbox.seen_message_id = message_id
        return message_id
    except Exception as e:
        print(f"[luckmail token snapshot error: {e}]")
        return ""


def _luckmail_request(method, path, **kwargs):
    method = method.upper()
    url = _luckmail_url(path)
    headers = _luckmail_headers()
    if method == "GET":
        r = curl_requests.get(url, headers=headers, impersonate="chrome", timeout=30, verify=False, **kwargs)
    elif method == "POST":
        r = curl_requests.post(url, headers=headers, impersonate="chrome", timeout=30, verify=False, **kwargs)
    else:
        raise ValueError(f"unsupported LuckMail method: {method}")
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:500]}
    if r.status_code < 200 or r.status_code >= 300:
        raise RuntimeError(f"LuckMail HTTP {r.status_code}: {body}")
    if body.get("code") not in (0, None):
        raise RuntimeError(f"LuckMail API error: {body}")
    return body.get("data")


def _create_luckmail_order():
    cfg = _email_cfg()
    payload = {
        "project_code": cfg.get("luckmail_project_code", "openai"),
        "email_type": cfg.get("luckmail_email_type", "self_built"),
    }
    for src, dest in (("luckmail_domain", "domain"), ("luckmail_specified_email", "specified_email"), ("luckmail_variant_mode", "variant_mode")):
        value = str(cfg.get(src, "") or "").strip()
        if value:
            payload[dest] = value
    data = _luckmail_request("POST", "/api/v1/openapi/order/create", json=payload)
    order_no = str((data or {}).get("order_no") or "").strip()
    email = str((data or {}).get("email_address") or "").strip().lower()
    if not order_no or not email:
        raise RuntimeError(f"LuckMail order/create returned incomplete data: {data}")
    return MailboxAccount(email=email, source="luckmail", provider="luckmail", order_no=order_no)


def _create_luckmail_purchase(args=None):
    cfg = _email_cfg()
    project_code = getattr(args, "luckmail_purchase_project", None) or cfg.get("luckmail_purchase_project_code") or cfg.get("luckmail_project_code") or "openai"
    email_type = getattr(args, "luckmail_purchase_email_type", None) or cfg.get("luckmail_purchase_email_type") or "ms_imap"
    domain = getattr(args, "luckmail_purchase_domain", None) or cfg.get("luckmail_purchase_domain") or "outlook.com"
    quantity = max(1, int(getattr(args, "count", None) or 1))
    payload = {"project_code": project_code, "email_type": email_type, "quantity": quantity}
    if domain:
        payload["domain"] = domain
    print(f"[*] LuckMail purchase: project={project_code} type={email_type} domain={domain or '*'} quantity={quantity}")
    data = _luckmail_request("POST", "/api/v1/openapi/email/purchase", json=payload)
    purchases = (data or {}).get("purchases") or []
    if not purchases:
        raise RuntimeError(f"LuckMail email/purchase returned no purchases: {data}")
    accounts = []
    for item in purchases:
        email = str(item.get("email_address") or "").strip().lower()
        token = str(item.get("token") or "").strip()
        if not email or not token:
            raise RuntimeError(f"LuckMail purchase item incomplete: {item}")
        accounts.append(MailboxAccount(
            email=email, source="luckmail_purchase", provider="luckmail_token", token=token,
            purchase_id=str(item.get("id") or ""), project_name=str(item.get("project_name") or item.get("project") or ""),
            price=str(item.get("price") or ""), purchase_total_cost=str((data or {}).get("total_cost") or ""),
            balance_after=str((data or {}).get("balance_after") or ""),
        ))
        print(f"[*] Purchased mailbox: {email} token={token} price={item.get('price')}")
    if (data or {}).get("balance_after") is not None:
        print(f"[*] LuckMail balance after purchase: {data.get('balance_after')}")
    return accounts


def _poll_luckmail_otp(mailbox, timeout=300, poll_interval=2):
    import time
    deadline = time.time() + timeout
    interval = max(1.0, float(poll_interval or 2))
    order_no = getattr(mailbox, "order_no", "")
    if not order_no:
        raise RuntimeError("LuckMail mailbox missing order_no")
    while time.time() < deadline:
        try:
            data = _luckmail_request("GET", f"/api/v1/openapi/order/{order_no}/code")
            status = str((data or {}).get("status") or "").lower()
            code = str((data or {}).get("verification_code") or "").strip()
            if status == "success" and code:
                print(f" code:{code}!")
                return code
            if status in {"timeout", "cancelled", "canceled"}:
                print(f" [{status}]")
                return None
        except Exception as e:
            print(f"[luckmail poll error: {e}]")
        print(".", end="", flush=True)
        time.sleep(interval)
    print(" timeout")
    return None


def _luckmail_mail_time(mail):
    value = str((mail or {}).get("received_at") or "").strip()
    if not value:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return int(datetime.strptime(value, fmt).timestamp())
        except Exception:
            pass
    return 0


def _poll_luckmail_token_otp(mailbox, timeout=300, issued_after_unix=0, poll_interval=2):
    import time
    deadline = time.time() + timeout
    interval = max(1.0, float(poll_interval or 2))
    while time.time() < deadline:
        try:
            data = (_luckmail_token_code(mailbox).get("data") or {})
            latest = _latest_luckmail_message(data)
            if latest and _luckmail_mail_time(latest) >= int(issued_after_unix or 0):
                candidate = _email_otp_candidate(mailbox, {
                    "id": latest.get("message_id") or latest.get("id") or "",
                    "subject": latest.get("subject") or "",
                    "bodyPreview": latest.get("body_preview") or latest.get("body") or latest.get("content") or "",
                    "body": {"content": latest.get("body") or latest.get("content") or ""},
                    "receivedDateTime": latest.get("received_at") or "",
                }, keyword="", issued_after_unix=issued_after_unix)
                if candidate:
                    print(f" code:{candidate['otp']}!")
                    return candidate["otp"]
        except Exception as e:
            print(f"[luckmail token poll error: {e}]")
        print(".", end="", flush=True)
        time.sleep(interval)
    print(" timeout")
    return None
