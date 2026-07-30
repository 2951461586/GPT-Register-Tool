"""Unified state machine for protocol payment-link extraction.

Native PayPal/UPI flows stay in :mod:`sms_tool.gen_pp_link`.  GoPay uses the
project-local PaymentService, while iDEAL/PIX/Kakao Pay/BLIK/TWINT run the
vendored protocol extractors under ``services/protocol-payment``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import CFG
from .paths import project_path, runtime_file


@dataclass(frozen=True)
class PaymentMethodSpec:
    key: str
    label: str
    country: str
    currency: str
    adapter: str
    script: str = ""


PAYMENT_METHODS: dict[str, PaymentMethodSpec] = {
    "paypal": PaymentMethodSpec("paypal", "PayPal", "US", "USD", "native"),
    "gopay": PaymentMethodSpec("gopay", "GoPay", "ID", "IDR", "gopay"),
    "upi": PaymentMethodSpec("upi", "UPI", "IN", "INR", "native"),
    "ideal": PaymentMethodSpec("ideal", "iDEAL", "NL", "EUR", "script", "ideal/ideal_qr_extract.py"),
    "pix": PaymentMethodSpec("pix", "PIX", "BR", "BRL", "pix", "pix/run_pix.py"),
    "kakao": PaymentMethodSpec("kakao", "Kakao Pay", "KR", "KRW", "script", "kakao/kakao_extract.py"),
    "blik": PaymentMethodSpec("blik", "BLIK", "PL", "PLN", "script", "blik/blik_qr_extract.py"),
    "twint": PaymentMethodSpec("twint", "TWINT", "CH", "CHF", "script", "twint/twint_extract.py"),
    "direct_card": PaymentMethodSpec("direct_card", "直卡 Checkout", "PH", "PHP", "direct_card", "direct_card/direct_card_extract.py"),
    "momo": PaymentMethodSpec("momo", "MoMo", "VN", "VND", "momo", "momo/run_momo.py"),
}

_ALIASES = {
    "go_pay": "gopay",
    "go-pay": "gopay",
    "upiqr": "upi",
    "upi_qr": "upi",
    "upi-qr": "upi",
    "kakao_pay": "kakao",
    "kakao-pay": "kakao",
    "direct-card": "direct_card",
    "directcard": "direct_card",
    "direct": "direct_card",
    "zhika": "direct_card",
    "card": "direct_card",
    "checkout": "direct_card",
    "momo_qr": "momo",
    "momo-qr": "momo",
    "momoqr": "momo",
}

_TRANSITIONS = {
    "created": {"validating", "failed"},
    "validating": {"preparing_proxy", "failed"},
    "preparing_proxy": {"running", "failed"},
    "running": {"extracting", "failed"},
    "extracting": {"completed", "failed"},
    "completed": set(),
    "failed": set(),
}

_STATE_LOCK = threading.Lock()
_URL_RE = re.compile(r"(?:https?://|upi://)[^\s\"'<>]+", re.IGNORECASE)
_RESULT_URL_RE = re.compile(
    r"(?im)^(?:iDEAL 最终扫码/授权 URL|Kakao/Nicepay 最终跳转 URL|"
    r"TWINT 最终支付 URL|BLIK 支付页 URL):\s*(?:\r?\n)?"
    r"((?:https?://|upi://)[^\s\"'<>]+)"
)
_BLIK_RESULT_RE = re.compile(r"BLIK_RESULT:(\{.*\})")
_BA_TOKEN_RE = re.compile(r"BA-[A-Za-z0-9_.-]+")
_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b")
_PROXY_AUTH_RE = re.compile(r"(?i)\b(https?|socks5h?)://[^\s/@]+@")
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(\b(?:access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|"
    r"client[_-]?secret|password|blik[_-]?code)\b[\"']?\s*[:=]\s*[\"']?)([^\s\"'&,}]+)"
)


class PaymentLinkRun:
    def __init__(self, method: str):
        self.run_id = uuid.uuid4().hex
        self.method = method
        self.state = "created"
        self.history: list[dict[str, Any]] = []
        self._record("created", "任务已创建")

    def move(self, state: str, message: str = "") -> None:
        allowed = _TRANSITIONS.get(self.state, set())
        if state not in allowed:
            raise RuntimeError(f"invalid payment state transition: {self.state} -> {state}")
        self.state = state
        self._record(state, message)

    def fail(self, message: str) -> None:
        if self.state not in {"completed", "failed"}:
            self.state = "failed"
            self._record("failed", message)

    def _record(self, state: str, message: str) -> None:
        self.history.append({"state": state, "at": int(time.time()), "message": message})


def normalize_payment_method(value: Any) -> str:
    method = str(value or "paypal").strip().lower().replace(" ", "_")
    method = _ALIASES.get(method, method)
    return method if method in PAYMENT_METHODS else ""


def payment_method_label(value: Any) -> str:
    method = normalize_payment_method(value)
    return PAYMENT_METHODS[method].label if method else str(value or "")


def supported_payment_methods() -> list[dict[str, Any]]:
    root = _reference_root()
    output = []
    for spec in PAYMENT_METHODS.values():
        available = spec.adapter in {"native", "gopay"} or (root / spec.script).is_file()
        output.append({
            "key": spec.key,
            "label": spec.label,
            "country": spec.country,
            "currency": spec.currency,
            "adapter": spec.adapter,
            "available": available,
        })
    return output


def generate_payment_link(
    access_token: str,
    proxy: Any = None,
    payment_method: Any = "paypal",
    auth_context: dict[str, Any] | None = None,
    paypal_generation_type: str | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    method = normalize_payment_method(payment_method)
    run = PaymentLinkRun(method or str(payment_method or ""))

    def move(state: str, message: str) -> None:
        run.move(state, message)
        if progress:
            progress(dict(run.history[-1], run_id=run.run_id, method=run.method))

    try:
        move("validating", "校验支付方式和 Access Token")
        if not method:
            raise ValueError(f"unsupported payment method: {payment_method}")
        if not str(access_token or "").strip():
            raise ValueError("access_token is required")
        spec = PAYMENT_METHODS[method]
        enabled = _enabled_methods()
        if method not in enabled:
            raise ValueError(f"payment method disabled by protocol_payments.enabled_methods: {method}")
        move("preparing_proxy", "加载分段代理和协议适配器")
        move("running", f"执行 {spec.label} 协议提链")

        if method == "paypal":
            from .gen_pp_link import generate_pp_link
            native_kwargs = _select_kwargs(kwargs, {
                "checkout_proxy", "provider_proxy", "stripe_init_proxy", "payment_method_proxy",
                "confirm_proxy", "approve_proxy", "promotion_proxy", "target_country",
                "checkout_country", "require_zero", "require_ba_token", "stage_proxy_countries",
            })
            result = generate_pp_link(
                access_token=access_token,
                proxy=proxy,
                auth_context=auth_context,
                paypal_generation_type=paypal_generation_type,
                **native_kwargs,
            )
        elif method == "upi":
            from .gen_pp_link import generate_upi_qr_link
            native_kwargs = _select_kwargs(kwargs, {
                "checkout_proxy", "provider_proxy", "approve_proxy", "target_country",
                "checkout_country", "payment_country", "require_zero", "qr_path",
            })
            result = generate_upi_qr_link(
                access_token=access_token,
                proxy=proxy,
                auth_context=auth_context,
                **native_kwargs,
            )
        elif method == "gopay":
            result = _generate_gopay_link(access_token, proxy=proxy, **kwargs)
        elif method == "direct_card":
            result = _run_direct_card(spec, access_token, proxy=proxy, **kwargs)
        elif method == "momo":
            result = _run_momo(spec, access_token, proxy=proxy, **kwargs)
        else:
            result = _run_protocol_script(spec, access_token, proxy=proxy, **kwargs)

        move("extracting", "归一化链接、二维码和协议结果")
        normalized = _normalize_result(spec, result)
        if not normalized.get("ok"):
            run.fail(str(normalized.get("error") or f"{spec.label} extraction failed"))
            normalized.update({
                "run_id": run.run_id,
                "manager_state": run.state,
                "state_history": run.history,
            })
            _safe_persist_run(normalized)
            return normalized
        completion_message = (
            "BLIK 协议支付已完成"
            if normalized.get("operation") == "execute_payment"
            else "协议支付链接提取完成"
        )
        run.move("completed", completion_message)
        normalized.update({
            "run_id": run.run_id,
            "manager_state": run.state,
            "state_history": run.history,
        })
        _safe_persist_run(normalized)
        return normalized
    except Exception as exc:
        run.fail(str(exc))
        failed = {
            "ok": False,
            "error": str(exc),
            "error_code": "payment_link_manager_failed",
            "payment_method": method or str(payment_method or ""),
            "run_id": run.run_id,
            "manager_state": run.state,
            "state_history": run.history,
            "url": "",
        }
        _safe_persist_run(failed)
        return failed


def _generate_gopay_link(access_token: str, proxy: Any = None, **kwargs: Any) -> dict[str, Any]:
    from .grpcurl_client import call_grpcurl

    cfg = CFG.get("gopay") if isinstance(CFG.get("gopay"), dict) else {}
    phone = str(kwargs.get("phone") or cfg.get("phone") or cfg.get("phone_number") or "").strip()
    country_code = str(kwargs.get("country_code") or cfg.get("country_code") or "+62").strip()
    otp_channel = str(kwargs.get("otp_channel") or cfg.get("otp_channel") or "sms").strip()
    body: dict[str, Any] = {
        "credential": {"accessToken": access_token},
        "useAccountToken": True,
        "tokenization": str(kwargs.get("tokenization") or cfg.get("tokenization") or "one_time").strip(),
        "gopayPhone": phone,
        "otpChannel": otp_channel,
        "gopayCountryCode": country_code,
        "proxyUrl": str(kwargs.get("provider_proxy") or kwargs.get("checkout_proxy") or proxy or "").strip(),
    }
    rpc = "StartGoPay" if phone else "CreateCheckoutLink"
    if rpc == "CreateCheckoutLink":
        body = {"credential": body["credential"]}
    response = call_grpcurl(
        rpc,
        body,
        addr=str(cfg.get("payment_service_addr") or "127.0.0.1:50051"),
        service=str(cfg.get("payment_service") or "payment.PaymentService"),
        grpcurl=str(cfg.get("grpcurl_path") or "grpcurl"),
        proto_path=str(cfg.get("proto_path") or "services/gopay-flow/proto/payment.proto"),
        proto_import_path=str(cfg.get("proto_import_path") or "services/gopay-flow/proto"),
        timeout_seconds=int(cfg.get("provider_timeout_seconds") or 600),
    )
    if not bool(response.get("success")):
        return {"ok": False, "error": response.get("errorMessage") or response.get("error_message") or "GoPay provider failed"}
    url = str(response.get("checkoutUrl") or response.get("checkout_url") or "").strip()
    return {
        "ok": bool(url),
        "url": url,
        "checkout_url": url,
        "cs_id": response.get("checkoutSessionId") or response.get("checkout_session_id") or "",
        "flow_id": response.get("flowId") or response.get("flow_id") or "",
        "otp_required": bool(response.get("otpRequired") or response.get("otp_required")),
        "link_type": "gopay_protocol_checkout",
        "warning": "未配置 GoPay 手机号，仅生成 checkout 链接" if not phone else "",
    }


def _run_extractor_subprocess(
    spec: PaymentMethodSpec,
    command: list[str],
    *,
    env: dict[str, str],
    cwd: str,
    timeout: int,
    cleanup_paths: tuple[str, ...] = (),
) -> tuple[subprocess.CompletedProcess[str] | None, str, dict[str, Any] | None]:
    """Run an extractor CLI, returning ``(proc, combined_output, timeout_error)``.

    Centralizes the run + ``TimeoutExpired`` handling + temp-file cleanup shared by
    the script/direct_card/momo adapters. On timeout returns ``(None, "", err_dict)``;
    otherwise ``(proc, stdout+stderr, None)``. ``cleanup_paths`` are always removed.
    """
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        return proc, output, None
    except subprocess.TimeoutExpired:
        return None, "", {"ok": False, "error": f"{spec.label} extractor timed out after {timeout}s"}
    finally:
        for path in cleanup_paths:
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception:
                    pass


def _run_protocol_script(spec: PaymentMethodSpec, access_token: str, proxy: Any = None, **kwargs: Any) -> dict[str, Any]:
    root = _reference_root()
    script = root / spec.script
    if not script.is_file():
        return {"ok": False, "error": f"protocol extractor not found: {script}"}

    cfg = _protocol_cfg()
    method_cfg = cfg.get("methods", {}).get(spec.key, {}) if isinstance(cfg.get("methods"), dict) else {}
    if not isinstance(method_cfg, dict):
        method_cfg = {}
    timeout = int(method_cfg.get("timeout_seconds") or cfg.get("timeout_seconds") or 900)
    seed_proxy = str(
        kwargs.get("seed_proxy")
        or proxy
        or kwargs.get("provider_proxy")
        or kwargs.get("checkout_proxy")
        or method_cfg.get("proxy")
        or ""
    ).strip()
    if not seed_proxy:
        return {"ok": False, "error": f"{spec.label} requires a proxy seed"}
    blik_code = str(kwargs.get("blik_code") or "").strip() if spec.key == "blik" else ""
    if spec.key == "blik" and not re.fullmatch(r"\d{6}", blik_code):
        return {"ok": False, "error": "BLIK requires an explicit 6-digit code for this run"}

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    command = [sys.executable, str(script)]
    proxy_file = ""
    if spec.key == "pix":
        env["OPENAI_ACCESS_TOKEN"] = access_token
        command.extend(["--quiet", "--proxy", seed_proxy])
        provider_proxy = str(kwargs.get("provider_proxy") or "").strip()
        promotion_proxy = str(kwargs.get("promotion_proxy") or "").strip()
        if provider_proxy:
            command.extend(["--br-proxy", provider_proxy])
        if promotion_proxy:
            command.extend(["--vn-proxy", promotion_proxy])
    else:
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False)
        with handle:
            handle.write(seed_proxy + "\n")
        proxy_file = handle.name
        if spec.key == "ideal":
            env.update({"PP_TOKEN": access_token, "IDEAL_PROXY_SEED_FILE": proxy_file, "IDEAL_FLOW_MODE": "single"})
        elif spec.key == "kakao":
            env.update({"KAKAO_TOKEN": access_token, "KAKAO_PROXY_SEED_FILE": proxy_file})
            countries = kwargs.get("stage_proxy_countries") if isinstance(kwargs.get("stage_proxy_countries"), dict) else {}
            checkout_country = str(countries.get("checkout") or kwargs.get("checkout_country") or "KR").strip().upper()
            promotion_country = str(countries.get("promotion") or "VN").strip().upper()
            provider_country = str(countries.get("provider") or kwargs.get("target_country") or "KR").strip().upper()
            env.update({
                "KAKAO_BOOTSTRAP_COUNTRY": checkout_country,
                "KAKAO_PROMOTION_COUNTRY": promotion_country,
                "KAKAO_PROVIDER_COUNTRY": provider_country,
            })
        elif spec.key == "blik":
            env.update({"PP_TOKEN": access_token, "IDEAL_PROXY_SEED_FILE": proxy_file, "IDEAL_FLOW_MODE": "single", "IDEAL_BLIK_CODE": blik_code})
        elif spec.key == "twint":
            env.update({"PP_TOKEN": access_token, "TWINT_PROXY_SEED_FILE": proxy_file, "TWINT_FLOW_MODE": "single"})

    proc, output, timeout_err = _run_extractor_subprocess(
        spec, command, env=env, cwd=str(script.parent), timeout=timeout, cleanup_paths=(proxy_file,),
    )
    if timeout_err:
        return timeout_err
    parsed = _last_json_object(proc.stdout or "") if spec.key in {"pix", "kakao"} else {}
    if parsed and spec.key == "kakao":
        parsed.setdefault("payment_method", "kakao")
        parsed.setdefault("url", parsed.get("provider_redirect_url") or "")
        return parsed
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": _redact_sensitive_text(_tail(output)) or f"extractor exited {proc.returncode}",
            "exit_code": proc.returncode,
        }
    parsed = _last_json_object(proc.stdout or "") if spec.key == "pix" else {}
    if parsed:
        parsed["ok"] = bool(parsed.get("long_url") or parsed.get("provider_redirect_url") or parsed.get("pix_qr_code"))
        parsed["url"] = parsed.get("long_url") or parsed.get("provider_redirect_url") or parsed.get("pix_hosted_instructions_url") or ""
        parsed["qr_data"] = parsed.get("pix_qr_code") or ""
        return parsed
    if spec.key == "blik":
        # BLIK 自动提交模式完成支付后没有可分享 URL，成功信号是提取器打印的
        # ``BLIK_RESULT:{...}`` 完成哨兵（status=completed）。不要再从截断日志抓 URL。
        completion = _blik_completion(proc.stdout or "")
        if completion:
            return {
                "ok": True,
                "url": "",
                "status": "completed",
                "operation": "execute_payment",
                "link_type": "blik_protocol_completed",
                "message": completion.get("message") or "BLIK 自动提交完成",
            }
    url = _last_payment_url(output)
    if not url:
        return {"ok": False, "error": _redact_sensitive_text(_tail(output)) or "extractor returned no payment URL"}
    return {"ok": True, "url": url, "link_type": f"{spec.key}_protocol"}


_DIRECT_CARD_CURRENCY = {
    "PH": "PHP", "US": "USD", "GB": "GBP", "JP": "JPY", "DE": "EUR", "FR": "EUR",
    "IE": "EUR", "NL": "EUR", "AU": "AUD", "CA": "CAD", "SG": "SGD", "IN": "INR",
    "TR": "TRY", "BR": "BRL", "KR": "KRW", "PL": "PLN", "CH": "CHF", "VN": "VND",
    "NZ": "NZD",
}


def _write_token_file(access_token: str) -> str:
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False)
    with handle:
        handle.write(str(access_token or "").strip() + "\n")
    return handle.name


def _run_direct_card(spec: PaymentMethodSpec, access_token: str, proxy: Any = None, **kwargs: Any) -> dict[str, Any]:
    """直卡 checkout short-link extractor adapter.

    Drives ``direct_card/direct_card_extract.py`` (a self-contained CLI) through a
    US checkout / promo-update / zero-amount-verify flow and returns its
    ``chatgpt.com/checkout/<entity>/<cs_id>`` long link. The access token is passed
    via a temp ``--credential-file`` so it never reaches the process argv.
    """
    root = _reference_root()
    script = root / spec.script
    if not script.is_file():
        return {"ok": False, "error": f"protocol extractor not found: {script}"}

    cfg = _protocol_cfg()
    method_cfg = cfg.get("methods", {}).get(spec.key, {}) if isinstance(cfg.get("methods"), dict) else {}
    if not isinstance(method_cfg, dict):
        method_cfg = {}
    timeout = int(method_cfg.get("timeout_seconds") or cfg.get("timeout_seconds") or 900)

    checkout_proxy = str(
        kwargs.get("checkout_proxy") or proxy or kwargs.get("provider_proxy") or method_cfg.get("proxy") or ""
    ).strip()
    if not checkout_proxy:
        return {"ok": False, "error": f"{spec.label} requires a checkout proxy seed"}
    update_proxy = str(
        kwargs.get("promotion_proxy") or kwargs.get("approve_proxy") or checkout_proxy or ""
    ).strip()

    country = str(kwargs.get("target_country") or kwargs.get("checkout_country") or spec.country or "PH").strip().upper()
    currency = str(
        method_cfg.get("currency")
        or (spec.currency if country == spec.country else _DIRECT_CARD_CURRENCY.get(country, spec.currency))
    ).strip().upper()
    countries = kwargs.get("stage_proxy_countries") if isinstance(kwargs.get("stage_proxy_countries"), dict) else {}

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    token_file = _write_token_file(access_token)
    command = [
        sys.executable, str(script),
        "--credential-file", token_file,
        "--billing-country", country,
        "--currency", currency,
        "--checkout-proxy", checkout_proxy,
        "--update-proxy", update_proxy,
        "--skip-proxy-check",
    ]
    checkout_cc = str(countries.get("checkout") or "").strip().upper()
    update_cc = str(countries.get("promotion") or countries.get("update") or "").strip().upper()
    if checkout_cc:
        command.extend(["--checkout-proxy-country", checkout_cc])
    if update_cc:
        command.extend(["--update-proxy-country", update_cc])
    promo = str(method_cfg.get("promo_campaign_id") or "").strip()
    if promo:
        command.extend(["--promo-campaign-id", promo])

    proc, output, timeout_err = _run_extractor_subprocess(
        spec, command, env=env, cwd=str(script.parent), timeout=timeout, cleanup_paths=(token_file,),
    )
    if timeout_err:
        return timeout_err
    parsed = _last_json_object(proc.stdout or "")
    if not parsed:
        return {
            "ok": False,
            "error": _redact_sensitive_text(_tail(output)) or f"extractor exited {proc.returncode}",
            "exit_code": proc.returncode,
        }
    if not parsed.get("ok"):
        return {
            "ok": False,
            "error": _redact_sensitive_text(str(parsed.get("error") or "direct_card extraction failed")),
            "error_code": parsed.get("error_type") or "direct_card_failed",
        }
    long_url = str(parsed.get("long_url") or "").strip()
    if not long_url:
        return {"ok": False, "error": "direct_card extractor returned no checkout URL"}
    return {
        "ok": True,
        "url": long_url,
        "long_url": long_url,
        "cs_id": parsed.get("cs_id") or "",
        "processor_entity": parsed.get("processor_entity") or "",
        "amount": parsed.get("amount_minor"),
        "amount_verification": parsed.get("amount_verification") or "",
        "currency": parsed.get("amount_currency") or currency,
        "target_country": parsed.get("billing_country") or country,
        "link_type": "direct_card_protocol",
    }


def _run_momo(spec: PaymentMethodSpec, access_token: str, proxy: Any = None, **kwargs: Any) -> dict[str, Any]:
    """MoMo scannable-QR extractor adapter.

    Drives ``momo/run_momo.py``, which wraps the VN checkout → Stripe init →
    force ₫0 → MoMo PM → confirm → ChatGPT approve → follow-redirect flow and emits
    a single normalized JSON object (``ok``/``url``/``qr_data``/``qr_path``/...). A
    ``data:image`` QR is decoded to a PNG under ``runtime/momo_qr`` by the runner.
    """
    root = _reference_root()
    script = root / spec.script
    if not script.is_file():
        return {"ok": False, "error": f"protocol extractor not found: {script}"}

    cfg = _protocol_cfg()
    method_cfg = cfg.get("methods", {}).get(spec.key, {}) if isinstance(cfg.get("methods"), dict) else {}
    if not isinstance(method_cfg, dict):
        method_cfg = {}
    timeout = int(method_cfg.get("timeout_seconds") or cfg.get("timeout_seconds") or 900)
    request_timeout = int(method_cfg.get("request_timeout_seconds") or 25)
    fallback_proxy = str(
        kwargs.get("checkout_proxy") or proxy or kwargs.get("provider_proxy") or method_cfg.get("proxy") or ""
    ).strip()
    stage_proxies = {
        "checkout": str(kwargs.get("checkout_proxy") or fallback_proxy).strip(),
        "promotion": str(kwargs.get("promotion_proxy") or fallback_proxy).strip(),
        "provider": str(
            kwargs.get("provider_proxy") or kwargs.get("stripe_init_proxy") or fallback_proxy
        ).strip(),
        "approve": str(kwargs.get("approve_proxy") or fallback_proxy).strip(),
        "redirect": str(kwargs.get("redirect_proxy") or fallback_proxy).strip(),
    }
    pre_proxy = str(method_cfg.get("pre_proxy") or "off").strip() or "off"
    qr_dir = runtime_file(CFG, "momo_qr")

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    token_file = _write_token_file(access_token)
    command = [
        sys.executable, str(script),
        "--token-file", token_file,
        "--pre-proxy", pre_proxy,
        "--timeout", str(max(8, request_timeout)),
        "--qr-out-dir", str(qr_dir),
    ]
    if fallback_proxy:
        env["MOMO_PROXY"] = fallback_proxy
    for stage, value in stage_proxies.items():
        if value:
            env[f"MOMO_{stage.upper()}_PROXY"] = value
    strategy = str(kwargs.get("strategy") or method_cfg.get("strategy") or "custom_promo").strip()
    if strategy:
        command.extend(["--strategy", strategy])
    if kwargs.get("probe_only"):
        command.append("--probe-only")
    stripe_profile = method_cfg.get("stripe_profile") if isinstance(method_cfg.get("stripe_profile"), dict) else {}
    for env_key, config_key in {
        "MOMO_STRIPE_RUNTIME_VERSION": "runtime_version",
        "MOMO_STRIPE_API_VERSION": "api_version",
        "MOMO_STRIPE_CLIENT_BETAS": "client_betas",
        "MOMO_STRIPE_CONFIRM_FIELDS": "confirm_fields",
    }.items():
        value = stripe_profile.get(config_key)
        if value not in (None, ""):
            env[env_key] = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value)
    max_proxies = int(method_cfg.get("max_proxies") or 1)
    if max_proxies > 1:
        command.extend(["--max-proxies", str(max_proxies)])

    proc, output, timeout_err = _run_extractor_subprocess(
        spec, command, env=env, cwd=str(script.parent), timeout=timeout, cleanup_paths=(token_file,),
    )
    if timeout_err:
        return timeout_err
    parsed = _last_json_object(proc.stdout or "")
    if not parsed:
        return {
            "ok": False,
            "error": _redact_sensitive_text(_tail(output)) or f"extractor exited {proc.returncode}",
            "exit_code": proc.returncode,
        }
    if not parsed.get("ok") and not parsed.get("error"):
        parsed["error"] = parsed.get("qr_error") or parsed.get("decision_text") or "momo QR extraction failed"
    return parsed


def _normalize_result(spec: PaymentMethodSpec, result: Any) -> dict[str, Any]:
    data = dict(result) if isinstance(result, dict) else {"ok": False, "error": str(result)}
    data.setdefault("payment_method", spec.key)
    data.setdefault("method", spec.key)
    data.setdefault("target_country", spec.country)
    data.setdefault("currency", spec.currency)
    data.setdefault("link_type", f"{spec.key}_protocol")
    if not data.get("url"):
        data["url"] = data.get("long_url") or data.get("provider_redirect_url") or data.get("checkout_url") or data.get("upi_uri") or ""
    data.setdefault("operation", "extract_link")
    completed_payment = (
        spec.key == "blik"
        and str(data.get("status") or "").lower() == "completed"
        and data.get("operation") == "execute_payment"
        and data.get("link_type") == "blik_protocol_completed"
    )
    if data.get("ok") and not completed_payment and not (data.get("url") or data.get("qr_data") or data.get("qr_path")):
        data["ok"] = False
        data["error"] = f"{spec.label} extractor returned no link or QR data"
    return data


def _select_kwargs(values: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key in allowed and value is not None}


def _protocol_cfg() -> dict[str, Any]:
    value = CFG.get("protocol_payments")
    return value if isinstance(value, dict) else {}


def _enabled_methods() -> set[str]:
    raw = _protocol_cfg().get("enabled_methods")
    if isinstance(raw, str):
        values = re.split(r"[,;\s]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        return set(PAYMENT_METHODS)
    return {method for value in values if (method := normalize_payment_method(value))}


def _reference_root() -> Path:
    configured = _protocol_cfg().get("reference_root") or "services/protocol-payment"
    return project_path(configured)


def _state_path() -> Path:
    configured = str(_protocol_cfg().get("state_file") or "").strip()
    return project_path(configured) if configured else runtime_file(CFG, "payment_link_runs.jsonl")


def _persist_run(result: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {}
    for key, value in result.items():
        lowered = key.lower()
        if lowered in {"raw_output", "raw_output_tail"} or "token" in lowered or "proxy" in lowered:
            continue
        record[key] = _redact_sensitive_values(value)
    with _STATE_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _safe_persist_run(result: dict[str, Any]) -> None:
    try:
        _persist_run(result)
    except Exception as exc:
        result["persistence_warning"] = f"payment run state was not persisted: {type(exc).__name__}"


def _last_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index in reversed([i for i, char in enumerate(text) if char == "{"]):
        try:
            value, end = decoder.raw_decode(text[index:])
        except Exception:
            continue
        if isinstance(value, dict) and not text[index + end :].strip():
            return value
    return {}


def _last_payment_url(text: str) -> str:
    labeled = [match.group(1).rstrip(".,);]") for match in _RESULT_URL_RE.finditer(text or "")]
    if labeled:
        return labeled[-1]
    urls = [match.group(0).rstrip(".,);]") for match in _URL_RE.finditer(text or "")]
    ignored = ("api.stripe.com", "chatgpt.com/backend-api", "ipinfo.io", "ip-api.com")
    candidates = [url for url in urls if not any(marker in url.lower() for marker in ignored)]
    return candidates[-1] if candidates else ""


def _tail(text: str, limit: int = 1200) -> str:
    value = str(text or "").strip()
    return value[-limit:]


def _blik_completion(stdout: str) -> dict[str, Any]:
    """Parse the BLIK auto-submit completion sentinel from stdout.

    BLIK 自动提交模式完成支付后没有可分享 URL，成功信号是 ``print_result_url`` 打印的
    ``BLIK_RESULT:{...}`` 结构化行（status=completed）。返回最后一个完成哨兵，否则空 dict。
    """
    for raw in reversed(_BLIK_RESULT_RE.findall(stdout or "")):
        try:
            value = json.loads(raw)
        except Exception:
            continue
        if (
            isinstance(value, dict)
            and value.get("ok") is True
            and str(value.get("payment_method") or "").lower() == "blik"
            and str(value.get("status") or "").lower() == "completed"
            and value.get("link_type") == "blik_protocol_completed"
        ):
            return value
    return {}


def _mask_ba_token(token: str) -> str:
    return f"{token[:6]}...{token[-4:]}" if len(token) > 12 else "BA-***"


def _redact_sensitive_text(value: str) -> str:
    text = _BA_TOKEN_RE.sub(lambda match: _mask_ba_token(match.group(0)), value)
    text = _BEARER_RE.sub(r"\1***", text)
    text = _JWT_RE.sub("***JWT***", text)
    text = _PROXY_AUTH_RE.sub(lambda match: f"{match.group(1)}://***:***@", text)
    return _SENSITIVE_VALUE_RE.sub(r"\1***", text)


def _redact_sensitive_values(value: Any) -> Any:
    """Mask credentials anywhere inside a persisted payment-run value.

    ``ba_token`` 键本身已被 :func:`_persist_run` 的键名过滤丢弃，但 approve URL
    （如 ``.../agreements/approve?ba_token=BA-...``）会以 ``url``/``fallback_url`` 字段
    保留，需按值脱敏后再落盘。日志和错误文本还可能包含 Bearer/JWT、代理认证或
    其他命名凭据，因此统一递归清洗。仅影响持久化记录，不改动返回给调用方的结果。
    """
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    if isinstance(value, dict):
        return {key: _redact_sensitive_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive_values(item) for item in value]
    return value
