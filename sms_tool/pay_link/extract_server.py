"""External Paylink Extraction Server (提链服务器) client adapter.

Follows the external paylink-server contract used in the reference projects
(``EXTRACT_LINK_API_BASE``, ``EXTRACT_LINK_CDK``, ``EXTRACT_LINK_TYPE``,
``EXTRACT_LINK_WORKERS``), while preserving this repository's normalized
``PaymentResult`` contract, explicit HTTP timeouts, fail-closed security gate,
and credential redaction guarantees.

Routing modes
-------------
* ``off`` (default): Never calls the external server; all extraction stays on
  the local in-process / direct_card adapters.
* ``prefer``: Routes eligible methods to the external server when configured
  and falls back to the local adapter on transport / timeout / 5xx / 429 /
  not-configured failures.
* ``only``: Strictly routes eligible methods to the external server and fails
  closed (``extract_server_not_configured``, HTTP 501 semantics) whenever
  ``api_base`` or ``cdk`` is absent or invalid.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import requests

from .base import _LOGGER, _as_bool, _config_data, _protocol_cfg, _redact_sensitive_text, safe_int

DEFAULT_EXTRACT_SERVER_ENDPOINT = "/api/v1/extract"
DEFAULT_EXTRACT_SERVER_TIMEOUT_SECONDS = 60
DEFAULT_EXTRACT_SERVER_WORKERS = 3

EXTRACT_SERVER_FALLBACK_CODES: frozenset[str] = frozenset(
    {
        "extract_server_not_configured",
        "extract_server_transport_error",
        "extract_server_timeout",
        "extract_server_unavailable",
        "extract_server_rate_limited",
    }
)

_MODE_ALIASES: dict[str, str] = {
    "off": "off",
    "local": "off",
    "disabled": "off",
    "none": "off",
    "false": "off",
    "0": "off",
    "prefer": "prefer",
    "auto": "prefer",
    "fallback": "prefer",
    "hybrid": "prefer",
    "only": "only",
    "server": "only",
    "remote": "only",
    "strict": "only",
}

_URL_KEYS: tuple[str, ...] = (
    "url",
    "pay_url",
    "payment_url",
    "checkout_url",
    "long_url",
    "approve_url",
    "provider_redirect_url",
    "link",
    "upi_uri",
)

_QR_KEYS: tuple[str, ...] = (
    "qr_data",
    "pix_qr_code",
    "qr_code",
    "qr_url",
)

_LIST_KEYS: tuple[str, ...] = ("extracted_links", "links")


@dataclass(frozen=True)
class ExtractLinkServerConfig:
    """Resolved runtime configuration for the external paylink server."""

    mode: str = "off"
    api_base: str = ""
    cdk: str = ""
    endpoint: str = DEFAULT_EXTRACT_SERVER_ENDPOINT
    default_type: str = ""
    workers: int = DEFAULT_EXTRACT_SERVER_WORKERS
    timeout_seconds: int = DEFAULT_EXTRACT_SERVER_TIMEOUT_SECONDS
    methods: tuple[str, ...] = ()
    pass_proxy: bool = True
    verify_tls: bool = True

    @property
    def active(self) -> bool:
        return self.mode in {"prefer", "only"}

    @property
    def configured(self) -> bool:
        return bool(self.api_base and self.cdk)

    def applies_to(self, payment_method: str) -> bool:
        if not self.active:
            return False
        normalized = str(payment_method or "").strip().lower()
        if not normalized:
            return False
        if self.methods and normalized not in self.methods:
            return False
        return True

    def request_url(self) -> str:
        base = self.api_base.rstrip("/")
        path = self.endpoint if self.endpoint.startswith("/") else f"/{self.endpoint}"
        return f"{base}{path}"


def _normalize_mode(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return _MODE_ALIASES.get(raw, "")


def _normalize_methods(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        items = [part.strip().lower() for part in raw.replace(";", ",").split(",")]
        return tuple(item for item in items if item)
    if isinstance(raw, (list, tuple, set)):
        items = [str(item or "").strip().lower() for item in raw]
        return tuple(item for item in items if item)
    return ()


def _validate_api_base(api_base: str) -> bool:
    try:
        parsed = urlparse(api_base)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _scrub_secret_text(text: str, *, cdk: str = "", access_token: str = "") -> str:
    cleaned = str(text or "")
    for secret in (cdk, access_token):
        if secret and secret in cleaned:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return _redact_sensitive_text(cleaned)


def resolve_extract_server_config(
    runtime_config: Mapping[str, Any] | None = None,
    *,
    options: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ExtractLinkServerConfig:
    """Resolve external paylink-server settings from options, env, and config.

    Precedence (highest to lowest):
    1. Per-call ``options`` (``extract_server_*`` overrides)
    2. Environment variables (``EXTRACT_LINK_*``)
    3. ``protocol_payments.extract_link_server`` in ``runtime_config``
    """
    env = os.environ if environ is None else environ
    opts = options if isinstance(options, Mapping) else {}
    proto = _protocol_cfg(_config_data(runtime_config) if runtime_config is not None else None)
    raw_cfg: object = proto.get("extract_link_server")
    cfg: Mapping[str, Any] = raw_cfg if isinstance(raw_cfg, Mapping) else {}

    env_base = str(env.get("EXTRACT_LINK_API_BASE") or "").strip()
    env_cdk = str(env.get("EXTRACT_LINK_CDK") or "").strip()
    env_mode = _normalize_mode(env.get("EXTRACT_LINK_MODE"))

    api_base = str(opts.get("extract_server_api_base") or env_base or cfg.get("api_base") or "").strip().rstrip("/")
    cdk = str(opts.get("extract_server_cdk") or env_cdk or cfg.get("cdk") or "").strip()
    endpoint = str(
        opts.get("extract_server_endpoint")
        or env.get("EXTRACT_LINK_ENDPOINT")
        or cfg.get("endpoint")
        or DEFAULT_EXTRACT_SERVER_ENDPOINT
    ).strip()
    if not endpoint:
        endpoint = DEFAULT_EXTRACT_SERVER_ENDPOINT
    elif not endpoint.startswith("/"):
        endpoint = f"/{endpoint}"

    default_type = (
        str(opts.get("extract_server_type") or env.get("EXTRACT_LINK_TYPE") or cfg.get("default_type") or "")
        .strip()
        .lower()
    )
    workers = max(
        1,
        safe_int(
            opts.get("extract_server_workers")
            or env.get("EXTRACT_LINK_WORKERS")
            or cfg.get("workers")
            or DEFAULT_EXTRACT_SERVER_WORKERS,
            DEFAULT_EXTRACT_SERVER_WORKERS,
        ),
    )
    timeout_seconds = max(
        5,
        safe_int(
            opts.get("extract_server_timeout")
            or env.get("EXTRACT_LINK_TIMEOUT")
            or cfg.get("timeout_seconds")
            or DEFAULT_EXTRACT_SERVER_TIMEOUT_SECONDS,
            DEFAULT_EXTRACT_SERVER_TIMEOUT_SECONDS,
        ),
    )

    methods = _normalize_methods(
        opts.get("extract_server_methods") if "extract_server_methods" in opts else cfg.get("methods")
    )
    pass_proxy_val = _as_bool(
        opts.get("extract_server_pass_proxy")
        if "extract_server_pass_proxy" in opts
        else (env.get("EXTRACT_LINK_PASS_PROXY") if "EXTRACT_LINK_PASS_PROXY" in env else cfg.get("pass_proxy"))
    )
    pass_proxy = True if pass_proxy_val is None else pass_proxy_val
    verify_tls_val = _as_bool(
        opts.get("extract_server_verify_tls")
        if "extract_server_verify_tls" in opts
        else (env.get("EXTRACT_LINK_VERIFY_TLS") if "EXTRACT_LINK_VERIFY_TLS" in env else cfg.get("verify_tls"))
    )
    verify_tls = True if verify_tls_val is None else verify_tls_val

    opt_mode = _normalize_mode(opts.get("extract_server_mode"))
    cfg_mode = _normalize_mode(cfg.get("mode"))
    cfg_enabled = _as_bool(cfg.get("enabled"))

    if opt_mode:
        mode = opt_mode
    elif env_mode:
        mode = env_mode
    elif cfg_mode:
        mode = cfg_mode
    elif cfg_enabled is False and not (env_base and env_cdk):
        mode = "off"
    else:
        mode = "only"

    return ExtractLinkServerConfig(
        mode=mode,
        api_base=api_base,
        cdk=cdk,
        endpoint=endpoint,
        default_type=default_type,
        workers=workers,
        timeout_seconds=timeout_seconds,
        methods=methods,
        pass_proxy=pass_proxy,
        verify_tls=verify_tls,
    )


def _extract_candidate_dict(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    merged = dict(payload)
    for nested_key in ("data", "result"):
        nested = payload.get(nested_key)
        if isinstance(nested, Mapping):
            for key, value in nested.items():
                if key not in merged or merged[key] in (None, "", []):
                    merged[key] = value
    return merged


def _first_nonempty_str(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_link_from_list(mapping: Mapping[str, Any]) -> tuple[str, str]:
    for key in _LIST_KEYS:
        items = mapping.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, str) and item.strip():
                return item.strip(), ""
            if isinstance(item, Mapping):
                url = _first_nonempty_str(item, _URL_KEYS)
                qr = _first_nonempty_str(item, _QR_KEYS)
                if url or qr:
                    return url, qr
    return "", ""


def _build_request_payload(
    method: str,
    access_token: str,
    *,
    cfg: ExtractLinkServerConfig,
    proxy: str,
    auth_context: Mapping[str, Any] | None,
    options: Mapping[str, Any],
) -> dict[str, Any]:
    link_type = str(options.get("extract_link_type") or method or cfg.default_type).strip().lower()
    payload: dict[str, Any] = {
        "cdk": cfg.cdk,
        "type": link_type,
        "payment_method": method,
        "access_token": access_token,
        "workers": cfg.workers,
    }
    if cfg.pass_proxy and proxy:
        payload["proxy"] = proxy
    for opt_key, body_key in (
        ("target_country", "country"),
        ("checkout_country", "billing_country"),
        ("currency", "currency"),
        ("plan_type", "plan_type"),
        ("blik_code", "blik_code"),
        ("paypal_generation_type", "paypal_generation_type"),
        ("operation_id", "operation_id"),
    ):
        value = options.get(opt_key)
        if isinstance(value, str) and value.strip():
            payload[body_key] = value.strip()
    if isinstance(auth_context, Mapping):
        email = str(auth_context.get("email") or "").strip()
        if email:
            payload["email"] = email
        account_id = str(auth_context.get("account_id") or "").strip()
        if account_id:
            payload["account_id"] = account_id
    return payload


def _http_error_result(
    method: str,
    status_code: int,
    detail: str,
    *,
    cdk: str,
    access_token: str,
) -> dict[str, Any]:
    safe_detail = _scrub_secret_text(detail, cdk=cdk, access_token=access_token)
    if status_code in {401, 403}:
        code = "extract_server_auth_failed"
        stage = "extract_server_auth"
        retryable = False
    elif status_code == 429:
        code = "extract_server_rate_limited"
        stage = "extract_server_request"
        retryable = True
    elif status_code == 501:
        code = "extract_server_not_configured"
        stage = "extract_server_config"
        retryable = False
    elif 500 <= status_code <= 599:
        code = "extract_server_unavailable"
        stage = "extract_server_request"
        retryable = True
    else:
        code = "extract_server_http_error"
        stage = "extract_server_request"
        retryable = False
    message = f"extract link server returned HTTP {status_code}"
    if safe_detail:
        message = f"{message}: {safe_detail[:400]}"
    return {
        "ok": False,
        "status": "failed",
        "payment_method": method,
        "source": "extract_link_server",
        "status_code": status_code,
        "error": message,
        "error_code": code,
        "error_stage": stage,
        "retryable": retryable,
    }


def _parse_server_body(
    method: str,
    status_code: int,
    raw_body: Any,
    *,
    cdk: str,
    access_token: str,
) -> dict[str, Any]:
    if isinstance(raw_body, Mapping):
        parsed: Any = raw_body
    elif isinstance(raw_body, str):
        try:
            parsed = json.loads(raw_body) if raw_body.strip() else {}
        except ValueError:
            parsed = {}
    else:
        parsed = {}

    candidate = _extract_candidate_dict(parsed)
    detail = str(candidate.get("error") or candidate.get("detail") or candidate.get("message") or "").strip()
    if not detail and isinstance(raw_body, str):
        detail = raw_body.strip()

    if status_code >= 400:
        return _http_error_result(method, status_code, detail, cdk=cdk, access_token=access_token)

    if not candidate:
        return {
            "ok": False,
            "status": "failed",
            "payment_method": method,
            "source": "extract_link_server",
            "status_code": status_code,
            "error": "extract link server returned an empty or non-JSON response",
            "error_code": "extract_server_invalid_response",
            "error_stage": "extract_server_response",
            "retryable": True,
        }

    explicit_ok = _as_bool(candidate.get("ok"))
    if explicit_ok is None:
        explicit_ok = _as_bool(candidate.get("success"))

    url = _first_nonempty_str(candidate, _URL_KEYS)
    qr_data = _first_nonempty_str(candidate, _QR_KEYS)
    qr_path = str(candidate.get("qr_path") or "").strip()
    if not url and not qr_data:
        list_url, list_qr = _first_link_from_list(candidate)
        url = url or list_url
        qr_data = qr_data or list_qr

    if explicit_ok is False or (explicit_ok is None and not (url or qr_data or qr_path)):
        safe_error = _scrub_secret_text(
            detail or "extract link server rejected request",
            cdk=cdk,
            access_token=access_token,
        )
        raw_code = str(candidate.get("error_code") or candidate.get("code") or "").strip()
        retryable_val = _as_bool(candidate.get("retryable"))
        return {
            "ok": False,
            "status": str(candidate.get("status") or "failed").strip() or "failed",
            "payment_method": method,
            "source": "extract_link_server",
            "status_code": status_code,
            "error": safe_error,
            "error_code": raw_code or "extract_server_rejected",
            "error_stage": "extract_server_response",
            "retryable": False if retryable_val is None else retryable_val,
        }

    result: dict[str, Any] = {
        "ok": True,
        "status": str(candidate.get("status") or "completed").strip() or "completed",
        "operation": "extract_link",
        "payment_method": method,
        "source": "extract_link_server",
        "link_type": str(candidate.get("link_type") or f"{method}_extract_server").strip(),
        "url": url,
    }
    if qr_data:
        result["qr_data"] = qr_data
    if qr_path:
        result["qr_path"] = qr_path
    for passthrough in ("currency", "amount", "cs_id", "order_id", "task_id", "target_country", "message"):
        val = candidate.get(passthrough)
        if val not in (None, ""):
            result[passthrough] = (
                _scrub_secret_text(str(val), cdk=cdk, access_token=access_token) if passthrough == "message" else val
            )
    return result


def extract_link_via_server(
    payment_method: str,
    access_token: str,
    *,
    proxy: str = "",
    auth_context: Mapping[str, Any] | None = None,
    runtime_config: Mapping[str, Any] | None = None,
    options: Mapping[str, Any] | None = None,
    config: ExtractLinkServerConfig | None = None,
    transport: Callable[..., tuple[int, Any]] | None = None,
) -> dict[str, Any]:
    """Call the external paylink extraction server and normalize its response."""
    method = str(payment_method or "").strip().lower()
    opts = options if isinstance(options, Mapping) else {}
    cfg = config if config is not None else resolve_extract_server_config(runtime_config, options=opts)
    token = str(access_token or "").strip()

    if not cfg.configured:
        return {
            "ok": False,
            "status": "failed",
            "payment_method": method,
            "source": "extract_link_server",
            "status_code": 501,
            "error": "extract link server is not configured (requires EXTRACT_LINK_API_BASE and EXTRACT_LINK_CDK)",
            "error_code": "extract_server_not_configured",
            "error_stage": "extract_server_config",
            "retryable": False,
        }
    if not _validate_api_base(cfg.api_base):
        return {
            "ok": False,
            "status": "failed",
            "payment_method": method,
            "source": "extract_link_server",
            "error": "extract link server api_base must be an http:// or https:// URL",
            "error_code": "extract_server_invalid_url",
            "error_stage": "extract_server_config",
            "retryable": False,
        }
    if not token:
        return {
            "ok": False,
            "status": "failed",
            "payment_method": method,
            "source": "extract_link_server",
            "error": "extract link server requires a non-empty access_token",
            "error_code": "missing_access_token",
            "error_stage": "extract_server_validation",
            "retryable": False,
        }

    url = cfg.request_url()
    headers = {
        "Authorization": f"Bearer {cfg.cdk}",
        "X-CDK-Token": cfg.cdk,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = _build_request_payload(
        method,
        token,
        cfg=cfg,
        proxy=str(proxy or "").strip(),
        auth_context=auth_context,
        options=opts,
    )

    try:
        if transport is not None:
            status_code, body = transport(
                url,
                payload,
                headers,
                cfg.timeout_seconds,
                cfg.verify_tls,
            )
        else:
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=cfg.timeout_seconds,
                verify=cfg.verify_tls,
            )
            status_code = int(response.status_code)
            try:
                body = response.json()
            except ValueError:
                body = response.text
    except (requests.Timeout, TimeoutError) as exc:
        safe_msg = _scrub_secret_text(str(exc), cdk=cfg.cdk, access_token=token)
        return {
            "ok": False,
            "status": "timed_out",
            "payment_method": method,
            "source": "extract_link_server",
            "error": f"extract link server timed out after {cfg.timeout_seconds}s"
            + (f": {safe_msg}" if safe_msg else ""),
            "error_code": "extract_server_timeout",
            "error_stage": "extract_server_request",
            "retryable": True,
        }
    except (requests.RequestException, OSError) as exc:
        safe_msg = _scrub_secret_text(str(exc), cdk=cfg.cdk, access_token=token)
        _LOGGER.warning("extract link server transport error for %s: %s", method, safe_msg)
        return {
            "ok": False,
            "status": "failed",
            "payment_method": method,
            "source": "extract_link_server",
            "error": f"extract link server request failed: {safe_msg or type(exc).__name__}",
            "error_code": "extract_server_transport_error",
            "error_stage": "extract_server_request",
            "retryable": True,
        }

    return _parse_server_body(method, status_code, body, cdk=cfg.cdk, access_token=token)


__all__ = [
    "DEFAULT_EXTRACT_SERVER_ENDPOINT",
    "DEFAULT_EXTRACT_SERVER_TIMEOUT_SECONDS",
    "DEFAULT_EXTRACT_SERVER_WORKERS",
    "EXTRACT_SERVER_FALLBACK_CODES",
    "ExtractLinkServerConfig",
    "extract_link_via_server",
    "resolve_extract_server_config",
]
