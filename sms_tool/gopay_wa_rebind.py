"""Adapted WA-channel GoPay payment rebind workflow.

The upstream byte-v-forge workflow is Temporal based. This module keeps the
same protocol boundary but adapts it to this project: state is carried in the
session JSON/SQLite row, and each call is an explicit grpcurl step.
"""

from __future__ import annotations

from typing import Any, Callable

from .grpcurl_client import call_grpcurl

GrpcCaller = Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]]


def wa_rebind_enabled(gopay_cfg: dict[str, Any]) -> bool:
    mode = str(gopay_cfg.get("one_click_mode") or "").strip().lower()
    wa_cfg = wa_config(gopay_cfg)
    return mode in {"wa", "wa_rebind", "whatsapp", "whatsapp_rebind"} or _bool_value(wa_cfg.get("enabled"), False)


def payment_phone(gopay_cfg: dict[str, Any], args: Any = None) -> str:
    wa_cfg = wa_config(gopay_cfg)
    return _first_non_empty(
        getattr(args, "gopay_wa_phone", None) if args is not None else None,
        wa_cfg.get("wa_phone"),
        gopay_cfg.get("wa_phone"),
        getattr(args, "gopay_phone", None) if args is not None else None,
        gopay_cfg.get("phone"),
        gopay_cfg.get("phone_number"),
    )


def otp_channel(gopay_cfg: dict[str, Any]) -> str:
    return "wa" if wa_rebind_enabled(gopay_cfg) else str(gopay_cfg.get("otp_channel") or "sms")


def after_completed_payment(
    *,
    email: str,
    data: dict[str, Any],
    payment_result: dict[str, Any],
    args: Any,
    gopay_cfg: dict[str, Any],
    caller: GrpcCaller | None = None,
) -> dict[str, Any]:
    if not wa_rebind_enabled(gopay_cfg):
        return payment_result
    if str(payment_result.get("paypal_status") or "") != "completed":
        return payment_result

    wa_cfg = wa_config(gopay_cfg)
    if not _bool_value(wa_cfg.get("rebind_after_payment"), True):
        payment_result["gopay_wa_rebind"] = {"status": "skipped", "reason": "rebind_after_payment=false"}
        return payment_result

    app_addr = _first_non_empty(wa_cfg.get("gopay_app_service_addr"), gopay_cfg.get("gopay_app_service_addr"))
    if not app_addr:
        return _with_rebind_state(payment_result, "app_service_missing", "gopay.wa_rebind.gopay_app_service_addr is required")

    wa_phone = payment_phone(gopay_cfg, args)
    rebind_phone = _first_non_empty(
        getattr(args, "gopay_rebind_phone", None) if args is not None else None,
        wa_cfg.get("rebind_phone"),
        wa_cfg.get("new_phone"),
    )
    if not wa_phone:
        return _with_rebind_state(payment_result, "wa_phone_missing", "wa_phone is required for WA rebind")
    if not rebind_phone:
        return _with_rebind_state(payment_result, "rebind_phone_required", "rebind_phone is required to start phone change")

    user_id = _first_non_empty(
        getattr(args, "gopay_user_id", None) if args is not None else None,
        wa_cfg.get("user_id"),
        "local",
    )
    pin = _first_non_empty(getattr(args, "gopay_pin", None) if args is not None else None, gopay_cfg.get("pin"))
    country_code = _first_non_empty(
        getattr(args, "gopay_country_code", None) if args is not None else None,
        wa_cfg.get("country_code"),
        gopay_cfg.get("country_code"),
        "62",
    )
    state = _stored_state(data, user_id)
    rpc = caller or call_gopay_app

    if not state:
        loaded = rpc("GetGoPayState", {"user_id": user_id}, gopay_cfg)
        if not _rpc_success(loaded):
            return _with_rebind_state(payment_result, "load_state_failed", _rpc_error(loaded), loaded)
        state = str(loaded.get("stateJson") or loaded.get("state_json") or "")

    auth_otp = _first_non_empty(getattr(args, "gopay_auth_otp", None) if args is not None else None, wa_cfg.get("auth_otp"))
    auth = rpc("AuthStart", {
        "phone": wa_phone,
        "country_code": country_code,
        "pin": pin,
        "otp_channel": "wa",
        "state_json": state,
    }, gopay_cfg)
    if not _rpc_success(auth):
        return _with_rebind_state(payment_result, "auth_start_failed", _rpc_error(auth), auth, state)
    state = str(auth.get("stateJson") or auth.get("state_json") or state)
    if _bool_value(auth.get("ready"), False):
        pass
    elif _bool_value(auth.get("otpSent") or auth.get("otp_sent"), False):
        if not auth_otp:
            return _with_rebind_state(payment_result, "wa_auth_otp_required", "GoPay WA login OTP is required", auth, state)
        completed = rpc("AuthComplete", {"otp": auth_otp, "pin": pin, "state_json": state}, gopay_cfg)
        if not _rpc_success(completed):
            return _with_rebind_state(payment_result, "auth_complete_failed", _rpc_error(completed), completed, state)
        state = str(completed.get("stateJson") or completed.get("state_json") or state)
        if not _bool_value(completed.get("ready"), False):
            return _with_rebind_state(payment_result, "auth_not_ready", "GoPay app state is not token-ready after auth", completed, state)
    else:
        return _with_rebind_state(payment_result, "auth_not_ready", "GoPay app auth did not become ready or request OTP", auth, state)

    change = rpc("ChangePhoneStart", {
        "pin": pin,
        "new_phone": rebind_phone,
        "country_code": country_code,
        "state_json": state,
    }, gopay_cfg)
    if not _rpc_success(change):
        return _with_rebind_state(payment_result, "change_phone_start_failed", _rpc_error(change), change, state)
    state = str(change.get("stateJson") or change.get("state_json") or state)
    rebind_otp = _first_non_empty(getattr(args, "gopay_rebind_otp", None) if args is not None else None, wa_cfg.get("rebind_otp"))
    if not rebind_otp:
        return _with_rebind_state(payment_result, "wa_rebind_otp_required", "GoPay change-phone SMS OTP is required", change, state)

    final = rpc("ChangePhoneComplete", {"otp": rebind_otp, "state_json": state}, gopay_cfg)
    if not _rpc_success(final):
        return _with_rebind_state(payment_result, "change_phone_complete_failed", _rpc_error(final), final, state)
    state = str(final.get("stateJson") or final.get("state_json") or state)
    saved = rpc("UpsertGoPayState", {"user_id": user_id, "state_json": state}, gopay_cfg)
    payment_result["gopay_wa_rebind"] = {
        "status": "completed",
        "user_id": user_id,
        "wa_phone": wa_phone,
        "rebind_phone": rebind_phone,
        "state_json": state,
        "save": saved,
    }
    payment_result["paypal_status"] = "completed"
    return payment_result


def call_gopay_app(method: str, body: dict[str, Any], gopay_cfg: dict[str, Any]) -> dict[str, Any]:
    wa_cfg = wa_config(gopay_cfg)
    return call_grpcurl(
        method,
        body,
        addr=_first_non_empty(wa_cfg.get("gopay_app_service_addr"), gopay_cfg.get("gopay_app_service_addr")),
        service=str(wa_cfg.get("gopay_app_service") or "gopay_app.GopayAppService"),
        grpcurl=str(gopay_cfg.get("grpcurl_path") or gopay_cfg.get("grpcurl") or "grpcurl"),
        proto_path=str(wa_cfg.get("gopay_app_proto_path") or "services\\gopay-app\\proto\\gopay_app.proto"),
        proto_import_path=str(wa_cfg.get("gopay_app_proto_import_path") or "services\\gopay-app\\proto"),
        timeout_seconds=int(wa_cfg.get("timeout_seconds") or gopay_cfg.get("provider_timeout_seconds") or 600),
    )


def wa_config(gopay_cfg: dict[str, Any]) -> dict[str, Any]:
    value = gopay_cfg.get("wa_rebind") if isinstance(gopay_cfg, dict) else {}
    return value if isinstance(value, dict) else {}


def _stored_state(data: dict[str, Any], user_id: str) -> str:
    rebind = data.get("gopay_wa_rebind") if isinstance(data.get("gopay_wa_rebind"), dict) else {}
    if str(rebind.get("user_id") or user_id).strip() == user_id:
        return str(rebind.get("state_json") or "").strip()
    return ""


def _with_rebind_state(
    result: dict[str, Any],
    status: str,
    message: str,
    payload: dict[str, Any] | None = None,
    state_json: str = "",
) -> dict[str, Any]:
    out = dict(result)
    out["gopay_wa_rebind"] = {
        "status": status,
        "message": message,
        "payload": payload or {},
        "state_json": state_json,
    }
    return out


def _rpc_success(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    if "success" in payload:
        return _bool_value(payload.get("success"), False)
    return not (payload.get("errorMessage") or payload.get("error_message") or payload.get("error"))


def _rpc_error(payload: dict[str, Any]) -> str:
    return str(payload.get("errorMessage") or payload.get("error_message") or payload.get("error") or payload)[:1000]


def _first_non_empty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _bool_value(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
