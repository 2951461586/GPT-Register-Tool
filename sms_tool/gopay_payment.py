"""GoPay payment runner for the one-click payment entry point.

The local PayPal path owns PayPal no-card agreement automation. GoPay can be
handled in two ways here:

* link mode: generate a Stripe/ChatGPT GoPay redirect and open it for manual
  confirmation.
* provider mode: call the byte-v-forge GoPay PaymentService through grpcurl
  when a compatible provider is already running.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Any

from .gen_pp_link import DEFAULT_CONFIG_PATH, generate_payment_link
from .grpcurl_client import call_grpcurl
from .gopay_wa_rebind import after_completed_payment, otp_channel as wa_otp_channel, payment_phone, wa_rebind_enabled
from .storage import get_account_record, list_paypal_accounts, mark_paypal_status, upsert_account


def one_click_pay_batch(args) -> None:
    emails = _emails_from_args(args)
    if not emails:
        print("[gopay-pay] no accounts to pay")
        return

    cfg = _load_config()
    print(f"[gopay-pay] {len(emails)} account(s) queued mode={_one_click_mode(cfg)}", flush=True)
    startup_error = _ensure_provider_started(cfg)
    if startup_error:
        print(f"[gopay-pay] provider startup failed: {startup_error}", flush=True)
        raise SystemExit(3)
    ready_count = 0
    completed_count = 0
    fail_count = 0
    for index, email in enumerate(emails, 1):
        print(f"\n[gopay-pay] === {index}/{len(emails)}: {email} ===", flush=True)
        result = one_click_pay(email, proxy=getattr(args, "proxy", None), args=args, cfg=cfg)
        if result.get("ok"):
            status = str(result.get("paypal_status") or "").strip()
            if status == "completed":
                completed_count += 1
                mark_paypal_status(email, "completed")
            else:
                ready_count += 1
            detail = result.get("paypal_url") or result.get("deeplink_url") or result.get("qr_code_url") or result.get("flow_id") or status
            print(f"[gopay-pay] {status or 'ok'}: {detail}", flush=True)
        else:
            fail_count += 1
            print(f"[gopay-pay] failed: {result.get('error')}", flush=True)

    print(
        f"\n[gopay-pay] done: completed={completed_count} ready={ready_count} "
        f"failed={fail_count} total={len(emails)}"
    )
    if fail_count:
        raise SystemExit(3)


def one_click_pay(email: str, proxy: str | None = None, args: Any = None, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg if isinstance(cfg, dict) else _load_config()
    row = _account_row(email)
    data = _session_data(row)
    access_token = str(row.get("access_token") or "").strip()
    if not access_token:
        access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        return {"ok": False, "email": email, "error": "missing_access_token"}

    if _should_use_provider(cfg):
        return _provider_one_click_pay(email, row, data, proxy=proxy, args=args, cfg=cfg)
    return _link_one_click_pay(email, row, data, access_token, proxy=proxy, cfg=cfg)


def _link_one_click_pay(
    email: str,
    row: dict[str, Any],
    data: dict[str, Any],
    access_token: str,
    proxy: str | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    link = generate_payment_link(access_token, proxy=proxy, payment_method="gopay")
    if not (link.get("ok") and link.get("url")):
        return {"ok": False, "email": email, "error": link.get("error", "missing_gopay_url"), "link": link}

    data["email"] = email
    data["access_token"] = access_token
    data["success"] = bool(data.get("success", True))
    data["payment_method"] = "gopay"
    data["paypal"] = link
    data["paypal_status"] = "link_ready"
    data["paypal_updated_at"] = int(time.time())

    json_path = str(row.get("json_path") or "").strip()
    if json_path:
        try:
            Path(json_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"[gopay-pay] warning: failed to update session JSON: {exc}", flush=True)
    upsert_account(data, json_path=json_path)

    url = str(link.get("url") or "").strip()
    if url and _bool_value((_gopay_cfg(cfg or {}).get("open_link")), True):
        webbrowser.open(url)
    return {"ok": True, "email": email, "paypal_url": url, "payment_method": "gopay", "json_path": json_path}


def _provider_one_click_pay(
    email: str,
    row: dict[str, Any],
    data: dict[str, Any],
    proxy: str | None = None,
    args: Any = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gopay_cfg = _gopay_cfg(cfg or {})
    credential = _credential(data, row)
    session_token = str(credential.get("sessionToken") or "").strip()
    access_token = str(credential.get("accessToken") or "").strip()
    if not session_token:
        return {"ok": False, "email": email, "error": "missing_gopay_provider_session_token"}

    pin = _arg_or_cfg(args, gopay_cfg, "gopay_pin", "pin")
    otp = _arg_or_cfg(args, gopay_cfg, "gopay_otp", "otp")
    saved_flow_id = _saved_flow_id(data, args, gopay_cfg)
    if otp and saved_flow_id:
        complete_body = _provider_complete_body(gopay_cfg, flow_id=saved_flow_id, otp=str(otp or ""), pin=str(pin or gopay_cfg.get("pin") or ""))
        complete = _call_payment_service("CompleteGoPay", complete_body, gopay_cfg)
        if not _rpc_success(complete):
            return _provider_error(email, "complete_gopay_failed", complete)
        result = _completed_result(email, saved_flow_id, complete)
        if wa_rebind_enabled(gopay_cfg):
            result = after_completed_payment(email=email, data=data, payment_result=result, args=args, gopay_cfg=gopay_cfg)
        _persist_provider_state(row, data, result)
        return result

    phone = payment_phone(gopay_cfg, args)
    country_code = _arg_or_cfg(args, gopay_cfg, "gopay_country_code", "country_code") or "62"
    auto_smsbower = _gopay_otp_source(gopay_cfg) == "smsbower"
    if not phone and not auto_smsbower:
        return {"ok": False, "email": email, "error": "gopay_phone is required for provider mode"}

    start_body = _provider_start_body(
        gopay_cfg,
        session_token=session_token,
        access_token=access_token,
        phone=str(phone),
        country_code=str(country_code),
        pin=str(pin or gopay_cfg.get("pin") or ""),
        proxy=str(proxy or gopay_cfg.get("proxy_url") or gopay_cfg.get("proxy") or ""),
    )
    start = _call_payment_service("StartGoPay", start_body, gopay_cfg)
    if not _rpc_success(start):
        return _provider_error(email, "start_gopay_failed", start)

    flow_id = str(start.get("flowId") or start.get("flow_id") or "")
    if auto_smsbower:
        complete_body = _provider_complete_body(gopay_cfg, flow_id=flow_id, otp="", pin=str(pin or gopay_cfg.get("pin") or ""))
        complete = _call_payment_service("CompleteGoPay", complete_body, gopay_cfg)
        if not _rpc_success(complete):
            return _provider_error(email, "complete_gopay_failed", complete)
        result = _completed_result(email, flow_id, complete)
        result["gopay_phone"] = start.get("gopayPhone") or start.get("gopay_phone") or phone or ""
        _persist_provider_state(row, data, result)
        return result
    if not otp:
        result = {
            "ok": True,
            "email": email,
            "flow_id": flow_id,
            "payment_method": "gopay",
            "paypal_status": "otp_required",
            "issued_after_unix": start.get("issuedAfterUnix") or start.get("issued_after_unix"),
            "expires_at_unix": start.get("expiresAtUnix") or start.get("expires_at_unix"),
        }
        _persist_provider_state(row, data, result)
        return result

    complete_body = _provider_complete_body(gopay_cfg, flow_id=flow_id, otp=str(otp or ""), pin=str(pin or gopay_cfg.get("pin") or ""))
    complete = _call_payment_service("CompleteGoPay", complete_body, gopay_cfg)
    if not _rpc_success(complete):
        return _provider_error(email, "complete_gopay_failed", complete)

    if _bool_value(complete.get("awaitingManualConfirmation", complete.get("awaiting_manual_confirmation")), False):
        result = {
            "ok": True,
            "email": email,
            "flow_id": flow_id,
            "payment_method": "gopay",
            "paypal_status": "manual_confirmation_required",
            **_provider_public_urls(complete),
        }
        _open_provider_url(result, gopay_cfg)
        _persist_provider_state(row, data, result)
        if not _bool_value(gopay_cfg.get("confirm_after_manual"), False):
            return result
        confirmed = _call_payment_service("ConfirmGoPayPayment", {"flowId": flow_id}, gopay_cfg)
        if not _rpc_success(confirmed):
            return _provider_error(email, "confirm_gopay_failed", confirmed)
        complete = confirmed

    result = _completed_result(email, flow_id, complete)
    if wa_rebind_enabled(gopay_cfg):
        result = after_completed_payment(email=email, data=data, payment_result=result, args=args, gopay_cfg=gopay_cfg)
    _persist_provider_state(row, data, result)
    return result


def _completed_result(email: str, flow_id: str, complete: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "email": email,
        "flow_id": flow_id,
        "payment_method": "gopay",
        "paypal_status": "completed",
        "charge_ref": complete.get("chargeRef") or complete.get("charge_ref"),
        "snap_token": complete.get("snapToken") or complete.get("snap_token"),
        **_provider_public_urls(complete),
    }


def _call_payment_service(method: str, body: dict[str, Any], gopay_cfg: dict[str, Any]) -> dict[str, Any]:
    addr = str(gopay_cfg.get("payment_service_addr") or gopay_cfg.get("grpc_addr") or "127.0.0.1:50051").strip()
    if not addr:
        return {"success": False, "errorMessage": "gopay.payment_service_addr is required"}
    return call_grpcurl(
        method,
        body,
        addr=addr,
        service=str(gopay_cfg.get("payment_service") or "payment.PaymentService"),
        grpcurl=str(gopay_cfg.get("grpcurl_path") or gopay_cfg.get("grpcurl") or "grpcurl"),
        proto_path=str(gopay_cfg.get("proto_path") or "services\\gopay-flow\\proto\\payment.proto"),
        proto_import_path=str(gopay_cfg.get("proto_import_path") or "services\\gopay-flow\\proto"),
        timeout_seconds=int(gopay_cfg.get("provider_timeout_seconds") or gopay_cfg.get("timeout_seconds") or 600),
    )


def _ensure_provider_started(cfg: dict[str, Any]) -> str:
    if not _should_use_provider(cfg):
        return ""
    gopay_cfg = _gopay_cfg(cfg)
    if not _bool_value(gopay_cfg.get("auto_start_provider"), True):
        return ""
    script = Path(DEFAULT_CONFIG_PATH).resolve().parent / "scripts" / "start_gopay_provider.ps1"
    if not script.exists():
        return f"missing provider startup script: {script}"
    command = [
        "powershell",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-ConfigPath",
        str(Path(DEFAULT_CONFIG_PATH).name),
    ]
    try:
        proc = subprocess.run(
            command,
            cwd=str(Path(DEFAULT_CONFIG_PATH).resolve().parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
        )
    except Exception as exc:
        return str(exc)
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return (stderr or stdout or f"exit {proc.returncode}")[:500]
    try:
        payload = json.loads(stdout)
    except Exception:
        return ""
    if isinstance(payload, dict) and payload.get("payment_service_listening") is False:
        return f"PaymentService not listening at {payload.get('payment_service_addr') or gopay_cfg.get('payment_service_addr')}"
    return ""


def _provider_start_body(
    gopay_cfg: dict[str, Any],
    *,
    session_token: str,
    access_token: str,
    phone: str,
    country_code: str,
    pin: str,
    proxy: str,
) -> dict[str, Any]:
    if _provider_api(gopay_cfg) == "legacy":
        return {
            "session_token": session_token,
            "country_code": country_code,
            "phone_number": phone,
            "pin": pin,
            "proxy_url": proxy,
        }
    return {
        "credential": {
            "session_token": session_token,
            "access_token": access_token,
        },
        "use_account_token": _bool_value(gopay_cfg.get("use_account_token"), True),
        "tokenization": str(gopay_cfg.get("tokenization") or "qris"),
        "gopay_phone": str(phone or ""),
        "otp_channel": wa_otp_channel(gopay_cfg),
        "gopay_country_code": country_code,
        "proxy_url": proxy,
    }


def _provider_complete_body(gopay_cfg: dict[str, Any], *, flow_id: str, otp: str, pin: str) -> dict[str, Any]:
    body = {"flow_id": flow_id, "otp": otp}
    if _provider_api(gopay_cfg) != "legacy":
        body["pin"] = pin
    return body


def _saved_flow_id(data: dict[str, Any], args: Any, gopay_cfg: dict[str, Any]) -> str:
    paypal = data.get("paypal") if isinstance(data.get("paypal"), dict) else {}
    return _first_non_empty(
        getattr(args, "gopay_flow_id", None) if args is not None else None,
        gopay_cfg.get("flow_id"),
        data.get("flow_id"),
        paypal.get("flow_id"),
    )


def _persist_provider_state(row: dict[str, Any], data: dict[str, Any], result: dict[str, Any]) -> None:
    paypal = data.get("paypal") if isinstance(data.get("paypal"), dict) else {}
    paypal.update({
        "ok": bool(result.get("ok")),
        "payment_method": "gopay",
        "method": "gopay",
        "status": result.get("paypal_status") or "",
        "flow_id": result.get("flow_id") or "",
        "charge_ref": result.get("charge_ref") or "",
        "snap_token": result.get("snap_token") or "",
        "url": result.get("paypal_url") or result.get("deeplink_url") or result.get("qr_code_url") or "",
    })
    if result.get("gopay_wa_rebind"):
        paypal["wa_rebind"] = result.get("gopay_wa_rebind")
    data["paypal"] = paypal
    data["payment_method"] = "gopay"
    data["paypal_status"] = result.get("paypal_status") or ""
    if result.get("gopay_wa_rebind"):
        data["gopay_wa_rebind"] = result.get("gopay_wa_rebind")
    data["paypal_updated_at"] = int(time.time())
    if result.get("paypal_status") == "completed":
        data["paypal_completed_at"] = int(time.time())
    json_path = str(row.get("json_path") or data.get("json_path") or "").strip()
    if json_path:
        try:
            Path(json_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"[gopay-pay] warning: failed to update session JSON: {exc}", flush=True)
    upsert_account(data, json_path=json_path)


def _provider_failure_class(code: str, payload: dict[str, Any]) -> dict[str, str]:
    try:
        raw = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        raw = str(payload)
    text = f"{code} {raw}".lower()
    if any(marker in text for marker in ("otp timeout", "otp not received", "smsbower otp", "sms timeout", "timeout after")):
        return {
            "gopay_failure_class": "otp_timeout",
            "recommended_action": "change_number",
            "retry_policy": "retry_with_new_smsbower_number",
        }
    if any(marker in text for marker in ("phone already registered", "already registered", "phone_already_taken", "user:already_exists")):
        return {
            "gopay_failure_class": "registration_phone_rejected",
            "recommended_action": "change_number",
            "retry_policy": "retry_with_new_smsbower_number",
        }
    if any(marker in text for marker in ("waf block", "tencent cloud waf", " http 403", "status 403", "403 forbidden", "customer_signup failed: http 403")):
        return {
            "gopay_failure_class": "proxy_blocked",
            "recommended_action": "change_proxy",
            "retry_policy": "retry_with_new_proxy",
        }
    if any(marker in text for marker in ("fraud_status", "fraud denied", "transaction_status\": \"deny", "transaction_status='deny", "midtrans fraud denied")):
        return {
            "gopay_failure_class": "terminal_fraud_deny",
            "recommended_action": "terminal_fail",
            "retry_policy": "do_not_retry_same_payment",
        }
    if "payment-switch failed" in text or "gopay_wallet" in text:
        return {
            "gopay_failure_class": "payment_switch_rejected",
            "recommended_action": "change_number_or_proxy",
            "retry_policy": "retry_with_new_gopay_number_then_proxy",
        }
    if "balance insufficient" in text or "required>=" in text:
        return {
            "gopay_failure_class": "balance_insufficient",
            "recommended_action": "fund_or_wait",
            "retry_policy": "wait_welcome_or_claim_envelope",
        }
    if "pin rejected" in text:
        return {
            "gopay_failure_class": "terminal_pin_rejected",
            "recommended_action": "check_pin",
            "retry_policy": "do_not_retry_until_pin_fixed",
        }
    if "pin rate-limited" in text or "cool down" in text:
        return {
            "gopay_failure_class": "pin_rate_limited",
            "recommended_action": "wait_cooldown",
            "retry_policy": "retry_after_cooldown",
        }
    if "rate limit" in text or "rate_limited" in text or "429" in text:
        return {
            "gopay_failure_class": "rate_limited",
            "recommended_action": "change_proxy_or_wait",
            "retry_policy": "retry_later_or_new_proxy",
        }
    if "registration failed" in text or "signup" in text or "bootstrap" in text:
        return {
            "gopay_failure_class": "registration_failed",
            "recommended_action": "change_number",
            "retry_policy": "retry_with_new_smsbower_number",
        }
    return {
        "gopay_failure_class": "unknown",
        "recommended_action": "inspect_provider_error",
        "retry_policy": "manual_triage",
    }


def _provider_error(email: str, code: str, payload: dict[str, Any]) -> dict[str, Any]:
    failure = _provider_failure_class(code, payload)
    return {
        "ok": False,
        "email": email,
        "error_code": code,
        "error": str(payload.get("errorMessage") or payload.get("error_message") or payload.get("error") or payload)[:1000],
        **failure,
        "provider": payload,
    }


def _provider_public_urls(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "deeplink_url": str(payload.get("deeplinkUrl") or payload.get("deeplink_url") or ""),
        "qr_code_url": str(payload.get("qrCodeUrl") or payload.get("qr_code_url") or ""),
        "qr_string": str(payload.get("qrString") or payload.get("qr_string") or ""),
        "finish_redirect_url": str(payload.get("finishRedirectUrl") or payload.get("finish_redirect_url") or ""),
    }


def _open_provider_url(result: dict[str, Any], gopay_cfg: dict[str, Any]) -> None:
    if not _bool_value(gopay_cfg.get("open_link"), True):
        return
    url = result.get("deeplink_url") or result.get("qr_code_url")
    if url:
        webbrowser.open(str(url))


def _emails_from_args(args) -> list[str]:
    if getattr(args, "email", None):
        return [str(args.email).strip()]
    if getattr(args, "email_file", None):
        with open(args.email_file, "r", encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip() and not line.startswith("#")]
    return [
        str(row.get("email") or "").strip()
        for row in list_paypal_accounts()
        if str(row.get("paypal_status") or "").strip().lower() != "completed"
    ]


def _account_row(email: str) -> dict[str, Any]:
    row = get_account_record(email)
    if row:
        return row
    rows = list_paypal_accounts(email)
    return rows[0] if rows else {"email": email}


def _session_data(row: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    raw_json = row.get("raw_json")
    if raw_json:
        try:
            parsed = json.loads(str(raw_json))
            if isinstance(parsed, dict):
                data.update(parsed)
        except Exception:
            pass
    json_path = str(row.get("json_path") or "").strip()
    if json_path:
        try:
            parsed = json.loads(Path(json_path).read_text(encoding="utf-8-sig"))
            if isinstance(parsed, dict):
                data = {**parsed, **data}
        except Exception:
            pass
    data.setdefault("email", row.get("email", ""))
    data.setdefault("access_token", row.get("access_token", ""))
    return data


def _load_config() -> dict[str, Any]:
    path = Path(DEFAULT_CONFIG_PATH)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _resolve_path(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    path = Path(value)
    if not path.is_absolute():
        path = Path(DEFAULT_CONFIG_PATH).resolve().parent / path
    return str(path)


def _gopay_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    value = cfg.get("gopay") if isinstance(cfg, dict) else {}
    return value if isinstance(value, dict) else {}


def _one_click_mode(cfg: dict[str, Any]) -> str:
    mode = str(_gopay_cfg(cfg).get("one_click_mode") or "protocol").strip().lower()
    if mode in {"wa", "wa_rebind", "whatsapp", "whatsapp_rebind"}:
        return "wa_rebind"
    if mode in {"provider", "protocol", "grpc"}:
        return "provider"
    if mode == "auto" and str(_gopay_cfg(cfg).get("payment_service_addr") or "").strip():
        return "provider"
    return "link"


def _should_use_provider(cfg: dict[str, Any]) -> bool:
    return _one_click_mode(cfg) in {"provider", "wa_rebind"}


def _provider_api(gopay_cfg: dict[str, Any]) -> str:
    value = str(
        gopay_cfg.get("provider_api")
        or gopay_cfg.get("payment_service_api")
        or gopay_cfg.get("provider_interface")
        or "byte-v-forge"
    ).strip().lower()
    if value in {"legacy", "python", "flat", "local-python"}:
        return "legacy"
    return "byte-v-forge"


def _gopay_otp_source(gopay_cfg: dict[str, Any]) -> str:
    otp_cfg = gopay_cfg.get("otp") if isinstance(gopay_cfg.get("otp"), dict) else {}
    value = str(
        gopay_cfg.get("otp_source")
        or otp_cfg.get("source")
        or otp_cfg.get("type")
        or ""
    ).strip().lower()
    if value in {"sms_bower", "sms-bower"}:
        return "smsbower"
    return value


def _first_non_empty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _credential(data: dict[str, Any], row: dict[str, Any]) -> dict[str, str]:
    auth_session = data.get("auth_session") if isinstance(data.get("auth_session"), dict) else {}
    session_token = (
        str(data.get("session_token") or "").strip()
        or str(auth_session.get("session_token") or "").strip()
        or str(auth_session.get("sessionToken") or "").strip()
        or _cookie_value(data, "__Secure-next-auth.session-token")
        or _cookie_value(row, "__Secure-next-auth.session-token")
    )
    access_token = str(data.get("access_token") or row.get("access_token") or "").strip()
    return {"sessionToken": session_token, "accessToken": access_token}


def _cookie_value(source: dict[str, Any], name: str) -> str:
    cookie_header = str(source.get("cookie_header") or source.get("cookies") or "").strip()
    if not cookie_header:
        return ""
    pattern = rf"(?:^|;\s*){re.escape(name)}=([^;]+)"
    match = re.search(pattern, cookie_header)
    if not match:
        return ""
    return match.group(1).strip()


def _arg_or_cfg(args: Any, cfg: dict[str, Any], arg_name: str, cfg_name: str) -> Any:
    value = getattr(args, arg_name, None) if args is not None else None
    if value is not None and str(value).strip() != "":
        return value
    return cfg.get(cfg_name)


def _rpc_success(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    if "success" in payload:
        return _bool_value(payload.get("success"), False)
    if "ok" in payload:
        return _bool_value(payload.get("ok"), False)
    return not (payload.get("errorMessage") or payload.get("error_message") or payload.get("error"))


def _bool_value(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
