"""Pure, provider-neutral helpers shared by protocol-payment scripts."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable


RESULT_SCHEMA = "protocol_payment.v1"

# ──────────────────── policy-driven redaction ────────────────────
# 规则来自仓库根的 ``sensitive_policy.json``（single source）——C# 的
# ``SensitiveDataSanitizer`` 与 Python 的 ``sms_tool.sanitizer`` 都读它，
# 子进程提取器也必须读它，否则三端各养一套规则、漂移只是时间问题
# （2026-09-12 扫描：本模块此前手搓一份"镜像"规则集）。
#
# 降级保证：policy 文件缺失/损坏时退回内置 LEGACY_* 规则（即旧手搓集），
# 支付提取器绝不因 policy 缺失而漏报密或崩溃。

import re as _re
from pathlib import Path as _Path

_REDACTED = "[REDACTED]"
_POLICY_CANDIDATES = (
    _Path(__file__).resolve().parents[3] / "sensitive_policy.json",
)
_POLICY_CACHE: dict[str, Any] | None = None
_POLICY_LOADED = False

# 旧手搓集，仅作 policy 缺失时的回退（与 v2026.09.12 版逐字相同）。
LEGACY_TEXT_PATTERNS = [
    ("bearer", r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b", "[REDACTED]"),
    ("ba_token", r"\bBA-[A-Za-z0-9_.-]+\b", "[REDACTED]"),
    ("proxy_credentials", r"(?i)\b((?:https?|socks5h?)://)[^\s/@]+@", r"\1://[REDACTED]@"),
    ("stripe_key", r"\b[sr]k_(?:live|test)_[A-Za-z0-9]+", "[REDACTED]"),
    ("refresh_token", r"\brt_[A-Za-z0-9._-]{8,}\b", "[REDACTED]"),
    ("named_secret", r"(?is)(access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|oauth[_-]?refresh[_-]?token|service[_-]?token|client[_-]?secret|api[_-]?key|ba[_-]?token|totp(?:[_-]?secret)?|card(?:[_-]?(?:number|cvv|cvc|last4))?|blik[_-]?code|authorization|password|checkout[_-]?session[_-]?id|payment[_-]?intent[_-]?id)(\s*[=:]\s*)['\"]?[^\s,}\"']+", r"\1\2[REDACTED]"),
]
LEGACY_KEY_FRAGMENTS = (
    "token", "secret", "password", "authorization",
    "card_number", "cardnumber", "card_last4", "cvv", "blik_code",
)
LEGACY_SAFE_KEY_SUFFIXES = ()


def _load_policy() -> dict[str, Any] | None:
    """Load and cache sensitive_policy.json; None when unavailable."""
    global _POLICY_CACHE, _POLICY_LOADED
    if _POLICY_LOADED:
        return _POLICY_CACHE
    _POLICY_LOADED = True
    for candidate in _POLICY_CANDIDATES:
        try:
            _POLICY_CACHE = json.loads(_Path(candidate).read_text(encoding="utf-8"))
            break
        except Exception:
            continue
    return _POLICY_CACHE


def _python_replacement(value: str) -> str:
    """policy 的替换串用 .NET 语法（``$1``）；Python re 需要 ``\\g<1>``。

    与 sms_tool/sanitizer._python_replacement 同一翻译（policy 是 .NET/Python
    共用的单一事实源）。
    """
    return _re.sub(r"\$(\d+)", r"\\g<\1>", value)


def _compiled_patterns() -> list[tuple[_re.Pattern[str], str]]:
    policy = _load_policy()
    if policy:
        try:
            return [
                (_re.compile(entry["pattern"]), _python_replacement(str(entry["replacement"])))
                for entry in policy.get("text_patterns") or []
            ]
        except Exception:
            pass
    return [(_re.compile(pattern), replacement) for _name, pattern, replacement in LEGACY_TEXT_PATTERNS]


def _key_rules() -> tuple[set[str], tuple[str, ...], tuple[str, ...]]:
    """(exact sensitive keys, fragments, safe suffixes) for payload redaction."""
    policy = _load_policy()
    if policy:
        try:
            return (
                {str(k).lower() for k in policy.get("sensitive_keys") or []},
                tuple(str(f).lower() for f in policy.get("sensitive_key_fragments") or []),
                tuple(str(s).lower() for s in policy.get("safe_key_suffixes") or []),
            )
        except Exception:
            pass
    return set(), LEGACY_KEY_FRAGMENTS, LEGACY_SAFE_KEY_SUFFIXES


def sanitize_text(value: Any) -> str:
    text = str(value or "")
    for pattern, replacement in _compiled_patterns():
        text = pattern.sub(replacement, text)
    return text


def sanitize_log_text(value: Any) -> str:
    """Log-strength redaction: sanitize_text plus log_text_patterns
    (operator-facing email masking) — mirrors C# SensitiveDataSanitizer.Redact."""
    text = sanitize_text(value)
    policy = _load_policy()
    entries = (policy or {}).get("log_text_patterns") or []
    try:
        for entry in entries:
            text = _re.compile(entry["pattern"]).sub(
                _python_replacement(str(entry["replacement"])), text)
    except Exception:
        pass
    return text


def _key_is_sensitive(key: str, path: str, exact: set[str], fragments: tuple[str, ...], safe_suffixes: tuple[str, ...]) -> bool:
    lowered = str(key).lower()
    full_path = f"{path}.{lowered}" if path else lowered
    if any(full_path == safe or full_path.endswith("." + safe) for safe in _SAFE_KEY_PATHS):
        return False
    if lowered in exact:
        return True
    if lowered.endswith(tuple(safe_suffixes)):
        return False
    return any(fragment in lowered for fragment in fragments)


# safe_key_paths 不随 policy 缺失而失效：proxy_affinity.session_id 是既有契约。
_SAFE_KEY_PATHS = ("proxy_affinity.session_id",)


def sanitize_payload(value: Any, *, _path: str = "") -> Any:
    if isinstance(value, dict):
        exact, fragments, safe_suffixes = _key_rules()
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if _key_is_sensitive(str(key), _path, exact, fragments, safe_suffixes):
                cleaned[key] = _REDACTED
            else:
                cleaned[key] = sanitize_payload(item, _path=f"{_path}.{str(key).lower()}" if _path else str(key).lower())
        return cleaned
    if isinstance(value, list):
        return [sanitize_payload(item, _path=_path) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


@dataclass(frozen=True)
class ProtocolResult:
    payment_method: str
    ok: bool
    status: str
    operation: str = "extract_link"
    url: str = ""
    link_type: str = ""
    message: str = ""
    error: str = ""
    error_code: str = ""
    error_stage: str = ""
    retryable: bool = False
    side_effect_started: bool = False
    requires_reconciliation: bool = False
    schema: str = RESULT_SCHEMA
    # Correlation with the launching CLI task (sms_tool.telemetry vocabulary).
    # The desktop sets SMS_TOOL_COMMAND_ID for backend tasks; payment
    # subprocesses inherit the environment, so the terminal report carries the
    # same correlation ID the manager's IPC envelopes use.
    command_id: str = ""  # dataclass default_factory below

    def __post_init__(self) -> None:
        if not self.command_id:
            object.__setattr__(self, "command_id", os.environ.get("SMS_TOOL_COMMAND_ID", "").strip())

    def to_json(self) -> str:
        return json.dumps(sanitize_payload(asdict(self)), ensure_ascii=False, separators=(",", ":"))


class ProtocolResultReporter:
    """Emit exactly one terminal ``protocol_payment.v1`` result.

    Extractors keep provider decisions local while this module owns the result
    schema, redaction, emitted-once invariant, and missing-output fallback.
    ``payment_method`` may be a callable for compatibility scripts whose method
    is selected from the environment at runtime.
    """

    def __init__(
        self,
        payment_method: str | Callable[[], str],
        link_type: str | Callable[[], str] = "",
        *,
        writer: Callable[[str], Any] = print,
    ) -> None:
        self._payment_method = payment_method
        self._link_type = link_type
        self._writer = writer
        self._emitted = False

    @property
    def emitted(self) -> bool:
        return self._emitted

    def _resolve(self, value: str | Callable[[], str]) -> str:
        return str(value() if callable(value) else value or "").strip()

    def _emit(self, result: ProtocolResult, *, prefix: str = "") -> bool:
        if self._emitted:
            return False
        self._emitted = True
        self._writer(f"{prefix}{result.to_json()}")
        return True

    def success(
        self,
        url: str,
        *,
        operation: str = "extract_link",
        link_type: str = "",
        message: str = "",
        side_effect_started: bool = False,
        prefix: str = "",
    ) -> bool:
        method = self._resolve(self._payment_method)
        resolved_link_type = link_type or self._resolve(self._link_type) or f"{method}_protocol"
        return self._emit(
            ProtocolResult(
                payment_method=method,
                ok=True,
                status="completed",
                operation=operation,
                url=str(url or ""),
                link_type=resolved_link_type,
                message=str(message or ""),
                side_effect_started=bool(side_effect_started),
            ),
            prefix=prefix,
        )

    def failure(
        self,
        error: Any,
        *,
        status: str = "failed",
        error_code: str = "extractor_failed",
        error_stage: str = "",
        retryable: bool = False,
        side_effect_started: bool = False,
        requires_reconciliation: bool = False,
    ) -> bool:
        method = self._resolve(self._payment_method)
        link_type = self._resolve(self._link_type) or f"{method}_protocol"
        return self._emit(ProtocolResult(
            payment_method=method,
            ok=False,
            status=status,
            url="",
            link_type=link_type,
            error=str(error or "extraction failed")[:600],
            error_code=error_code,
            error_stage=error_stage,
            retryable=bool(retryable),
            side_effect_started=bool(side_effect_started),
            requires_reconciliation=bool(requires_reconciliation),
        ))

    def already_paid(self) -> bool:
        return self.failure(
            "User is already paid",
            status="already_paid",
            error_code="account_already_paid",
            retryable=False,
        )

    def ensure_terminal(self, exit_code: int) -> bool:
        return self.failure(
            f"extractor exited {exit_code} without a structured result",
            error_code="extractor_output_missing",
        )


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return max(minimum, default)
    try:
        return max(minimum, int(raw))
    except ValueError:
        return max(minimum, default)


def collect_strings(payload: Any, result: list[str] | None = None) -> list[str]:
    values = result if result is not None else []
    if isinstance(payload, str):
        values.append(payload)
    elif isinstance(payload, dict):
        for value in payload.values():
            collect_strings(value, values)
    elif isinstance(payload, list):
        for item in payload:
            collect_strings(item, values)
    return values


def amount_from_payload(payload: Any) -> int:
    if isinstance(payload, dict):
        total_summary = payload.get("total_summary")
        if isinstance(total_summary, dict) and total_summary.get("due") is not None:
            return int(total_summary.get("due") or 0)
        invoice = payload.get("invoice")
        if isinstance(invoice, dict) and invoice.get("amount_due") is not None:
            return int(invoice.get("amount_due") or 0)
        line_items = payload.get("line_items")
        if isinstance(line_items, list):
            amounts = [
                int(item.get("amount") or 0)
                for item in line_items
                if isinstance(item, dict) and item.get("amount") is not None
            ]
            if amounts:
                return sum(amounts)
    text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    for pattern in (
        r'"total"\s*:\s*(\d+)',
        r'"amount_total"\s*:\s*(\d+)',
        r'"checkout_amount"\s*:\s*(\d+)',
        r'"amount"\s*:\s*(\d+)',
    ):
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return 0


def collect_urls(payload: Any, urls: list[str] | None = None) -> list[str]:
    found = urls if urls is not None else []
    if isinstance(payload, str):
        for match in re.findall(r"https?://[^\s\"'<>]+", payload):
            found.append(match.rstrip("),.;]"))
        for match in re.findall(r"data:image/(?:png|svg\+xml|jpeg);base64,[A-Za-z0-9+/=]+", payload):
            found.append(match)
    elif isinstance(payload, dict):
        for value in payload.values():
            collect_urls(value, found)
    elif isinstance(payload, list):
        for item in payload:
            collect_urls(item, found)
    return found


def find_submission_attempt(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        value = payload.get("submission_attempt")
        if isinstance(value, dict):
            return value
        for item in payload.values():
            nested = find_submission_attempt(item)
            if nested:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = find_submission_attempt(item)
            if nested:
                return nested
    return {}


def extract_redirect_url(
    payload: Any,
    is_redirect_like: Callable[[Any, bool], bool],
) -> str:
    if isinstance(payload, dict):
        next_action = payload.get("next_action")
        if isinstance(next_action, dict):
            redirect = next_action.get("redirect_to_url")
            if isinstance(redirect, dict):
                url = str(redirect.get("url") or "").strip()
                if is_redirect_like(url, True):
                    return url
            for key in ("url", "redirect_url", "redirect_to_url", "hosted_url"):
                value = next_action.get(key)
                if is_redirect_like(value, True):
                    return str(value)
        for key in ("redirect_url", "redirect_to_url", "authorization_url", "authentication_url"):
            value = payload.get(key)
            if is_redirect_like(value, True):
                return str(value)
        for value in payload.values():
            nested = extract_redirect_url(value, is_redirect_like)
            if nested:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = extract_redirect_url(item, is_redirect_like)
            if nested:
                return nested
    return ""


def first_value_by_key(payload: Any, key: str) -> Any:
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = first_value_by_key(value, key)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = first_value_by_key(item, key)
            if found not in (None, "", [], {}):
                return found
    return None
