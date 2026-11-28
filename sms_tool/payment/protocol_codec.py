"""Pure offline protocol-payment codecs, billing catalog, and Stripe classifiers.

Migrated from TeamGo's pure protocol helpers (``hosted_timing``, ``money``,
``billing``, ``countries``, ``tokenize``, ``chatgpt``, ``stripe_protocol``) with
zero network I/O, zero CAPTCHA solving, and an optional ``rust-anything`` PyO3
accelerator (``rust/protocol-payment-rs``).
"""

from __future__ import annotations

import ast
import base64
import binascii
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, unquote

from .qualification_states import load_native_module

_NATIVE_PROTOCOL_PAYMENT = load_native_module(
    "protocol-payment-rs", "protocol_payment_rs", "RUST_ANYTHING_UNIT_PROTOCOL_PAYMENT"
)
_NATIVE_ENCODE_RV_TIMESTAMP = getattr(_NATIVE_PROTOCOL_PAYMENT, "encode_rv_timestamp", None)
_NATIVE_DECODE_RV_TIMESTAMP_DICT = getattr(_NATIVE_PROTOCOL_PAYMENT, "_decode_rv_timestamp_dict", None)
_NATIVE_EXTRACT_RV_BUILD_FIELDS_DICT = getattr(_NATIVE_PROTOCOL_PAYMENT, "_extract_rv_build_fields_dict", None)
_NATIVE_RV_TIMESTAMP_FROM_SCRIPT = getattr(_NATIVE_PROTOCOL_PAYMENT, "rv_timestamp_from_script", None)
_NATIVE_QUOTE_FROM_PAYLOAD_STR = getattr(_NATIVE_PROTOCOL_PAYMENT, "_quote_from_payload_str", None)
_NATIVE_SCALAR_TYPES = (str, type(None), bool, int, float)


# ─── Hosted Checkout rv_timestamp codec & JS build-field extractor ────────────

_PRINTABLE_MIN = 32
_PRINTABLE_WIDTH = 95
_XOR_BYTE = 5
_SHIFT = 11
_MAX_OBJECT_SCAN = 8192

_JS_STRING = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`'
_JS_IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]*"
_JS_VALUE = rf"(?:{_JS_STRING}|{_JS_IDENTIFIER})"
_JS_COMMENT = r"(?:(?:/\*.*?\*/|//[^\r\n]*)\s*)*"

_RV_FIELD_RE = {
    name: re.compile(rf"(?:[\"']?{name}[\"']?)\s*:\s*{_JS_COMMENT}(?P<value>{_JS_VALUE})", re.DOTALL)
    for name in ("rvTs", "rv", "sv")
}
_JS_ASSIGNMENT_RE = re.compile(
    rf"(?:\b(?:var|let|const)\s+)?(?P<name>{_JS_IDENTIFIER})\s*=\s*{_JS_COMMENT}(?P<value>{_JS_VALUE})",
    re.DOTALL,
)
_RVTS_KEY_RE = re.compile(r"(?:[\"']rvTs[\"']|\brvTs\b)\s*:")


@dataclass(frozen=True)
class RVBuildFields:
    """Three static build constants embedded in a Stripe.js Hosted Checkout bundle."""

    rv_ts: str
    rv: str
    sv: str

    def as_dict(self) -> dict[str, str]:
        return {"rvTs": self.rv_ts, "rv": self.rv, "sv": self.sv}


def _xor5(value: str) -> str:
    return "".join(chr(ord(ch) ^ _XOR_BYTE) for ch in value)


def _shift_printable(value: str, amount: int) -> str:
    out: list[str] = []
    for ch in value:
        code = ord(ch)
        if not _PRINTABLE_MIN <= code < _PRINTABLE_MIN + _PRINTABLE_WIDTH:
            raise ValueError("rv_timestamp contains a non-printable intermediate character")
        out.append(chr((code - _PRINTABLE_MIN + amount) % _PRINTABLE_WIDTH + _PRINTABLE_MIN))
    return "".join(out)


def encode_rv_timestamp(rv_ts: str, rv: str, sv: str) -> str:
    """Encode static Stripe.js build fields into the Hosted Checkout ``rv_timestamp``."""
    if (
        _NATIVE_ENCODE_RV_TIMESTAMP is not None
        and isinstance(rv_ts, _NATIVE_SCALAR_TYPES)
        and isinstance(rv, _NATIVE_SCALAR_TYPES)
        and isinstance(sv, _NATIVE_SCALAR_TYPES)
    ):
        return str(_NATIVE_ENCODE_RV_TIMESTAMP(rv_ts, rv, sv))
    payload = json.dumps(
        {"rvTs": str(rv_ts), "rv": str(rv), "sv": str(sv)},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    padded = payload + (" " * (3 - len(payload) % 3))
    transformed = _xor5(padded).encode("latin1")
    encoded = quote(base64.b64encode(transformed).decode("ascii"), safe="~()*!.'-_")
    return _shift_printable(encoded, _SHIFT)


def _decode_rv_timestamp_dict(value: Any) -> dict[str, str] | None:
    if _NATIVE_DECODE_RV_TIMESTAMP_DICT is not None and isinstance(value, _NATIVE_SCALAR_TYPES):
        res = _NATIVE_DECODE_RV_TIMESTAMP_DICT(value)
        return dict(res) if isinstance(res, dict) else None
    if not isinstance(value, str) or not value:
        return None
    try:
        unshifted = _shift_printable(value, -_SHIFT)
        raw = base64.b64decode(unquote(unshifted), validate=True).decode("latin1")
        payload = _xor5(raw).rstrip(" ")
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            return None
        rv_ts = decoded.get("rvTs")
        rv = decoded.get("rv")
        sv = decoded.get("sv")
        if not (isinstance(rv_ts, str) and isinstance(rv, str) and isinstance(sv, str)):
            return None
        return {"rvTs": rv_ts, "rv": rv, "sv": sv}
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, binascii.Error):
        return None


def decode_rv_timestamp(value: str) -> RVBuildFields | None:
    """Decode an ``rv_timestamp`` back into :class:`RVBuildFields`, or ``None``."""
    data = _decode_rv_timestamp_dict(value)
    if not data:
        return None
    return RVBuildFields(data["rvTs"], data["rv"], data["sv"])


def _decode_js_string(token: str) -> str | None:
    token = token.strip()
    if not token:
        return None
    try:
        if token.startswith("`") and token.endswith("`"):
            body = token[1:-1]
            if "${" in body:
                return None
            return bytes(body, "utf-8").decode("unicode_escape")
        value = ast.literal_eval(token)
        return value if isinstance(value, str) else None
    except (SyntaxError, ValueError, UnicodeDecodeError):
        return None


def _assignment_values(text: str) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for match in _JS_ASSIGNMENT_RE.finditer(text):
        value = _decode_js_string(match.group("value"))
        if value is not None:
            assignments[match.group("name")] = value
    return assignments


def _resolve_js_value(token: str, assignments: dict[str, str]) -> str | None:
    token = token.strip()
    literal = _decode_js_string(token)
    if literal is not None:
        return literal
    if not re.fullmatch(_JS_IDENTIFIER, token):
        return None
    seen: set[str] = set()
    while re.fullmatch(_JS_IDENTIFIER, token):
        if token in seen or token not in assignments:
            return None
        seen.add(token)
        token = assignments[token]
    return token if token else None


def _object_end(text: str, start: int) -> int | None:
    quote_char: str | None = None
    escaped = False
    depth = 0
    end_limit = min(len(text), start + _MAX_OBJECT_SCAN)
    for pos in range(start, end_limit):
        ch = text[pos]
        if quote_char:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote_char:
                quote_char = None
            continue
        if ch in "\"'`":
            quote_char = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return pos + 1
    return None


def _candidate_objects(text: str) -> list[str]:
    candidates: list[str] = []
    seen_starts: set[int] = set()
    for key_match in _RVTS_KEY_RE.finditer(text):
        start = text.rfind("{", max(0, key_match.start() - _MAX_OBJECT_SCAN), key_match.start())
        if start < 0 or start in seen_starts:
            continue
        seen_starts.add(start)
        end = _object_end(text, start)
        if end is not None:
            candidates.append(text[start:end])
    return candidates


def _extract_rv_build_fields_dict(text: Any) -> dict[str, str] | None:
    if _NATIVE_EXTRACT_RV_BUILD_FIELDS_DICT is not None and isinstance(text, _NATIVE_SCALAR_TYPES):
        res = _NATIVE_EXTRACT_RV_BUILD_FIELDS_DICT(text)
        return dict(res) if isinstance(res, dict) else None
    if not isinstance(text, str) or not text or "rvTs" not in text:
        return None
    assignments: dict[str, str] | None = None
    for candidate in _candidate_objects(text):
        raw_tokens: dict[str, str] = {}
        for name, pattern in _RV_FIELD_RE.items():
            match = pattern.search(candidate)
            if match:
                raw_tokens[name] = match.group("value")
        if len(raw_tokens) != 3:
            continue
        resolved: dict[str, str] = {}
        for name, token in raw_tokens.items():
            literal = _decode_js_string(token)
            if literal is not None:
                resolved[name] = literal
                continue
            if assignments is None:
                assignments = _assignment_values(text)
            val = _resolve_js_value(token, assignments)
            if val is None:
                resolved.clear()
                break
            resolved[name] = val
        if len(resolved) == 3:
            return {"rvTs": resolved["rvTs"], "rv": resolved["rv"], "sv": resolved["sv"]}
    return None


def extract_rv_build_fields(text: str) -> RVBuildFields | None:
    """Statically extract ``rvTs``, ``rv`` and ``sv`` from Stripe HTML/JS text."""
    data = _extract_rv_build_fields_dict(text)
    if not data:
        return None
    return RVBuildFields(data["rvTs"], data["rv"], data["sv"])


def rv_timestamp_from_script(text: str) -> str | None:
    """Extract build fields from supplied HTML/JS text and encode ``rv_timestamp``."""
    if _NATIVE_RV_TIMESTAMP_FROM_SCRIPT is not None and isinstance(text, _NATIVE_SCALAR_TYPES):
        res = _NATIVE_RV_TIMESTAMP_FROM_SCRIPT(text)
        return str(res) if isinstance(res, str) else None
    fields = extract_rv_build_fields(text)
    return encode_rv_timestamp(fields.rv_ts, fields.rv, fields.sv) if fields else None


# ─── Zero-decimal currencies, checkout quotes, minor-unit amounts ───────────

ZERO_DECIMAL_CURRENCIES: frozenset[str] = frozenset(
    {
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "JPY",
        "KMF",
        "KRW",
        "MGA",
        "PYG",
        "RWF",
        "UGX",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    }
)

_MINOR_NAMES = (
    "checkout_amount_minor",
    "amount_total",
    "amount_due",
    "unit_amount",
    "taxInclusive",
    "tax_inclusive",
)


def minor_units_from_amount(amount: Any, currency: str) -> int:
    """Convert a decimal major amount into integer minor currency units."""
    factor = Decimal(1 if str(currency or "").strip().upper() in ZERO_DECIMAL_CURRENCIES else 100)
    try:
        value = Decimal(str(amount)) * factor
    except InvalidOperation as exc:
        raise ValueError("invalid checkout amount") from exc
    if not value.is_finite() or value < 0 or value != value.to_integral_value():
        raise ValueError("checkout amount cannot be represented as exact minor units")
    return int(value)


def _as_minor_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list)):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    text = str(value).strip()
    if not text or "." in text or "," in text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _expected_amount_from_object(payload: Mapping[str, Any]) -> int | None:
    summary = payload.get("total_summary")
    if isinstance(summary, Mapping):
        for key in ("due", "amount_due", "total", "amount"):
            found = _as_minor_int(summary.get(key))
            if found is not None:
                return found
    invoice = payload.get("invoice")
    if isinstance(invoice, Mapping):
        for key in ("amount_due", "total", "amount_paid"):
            found = _as_minor_int(invoice.get(key))
            if found is not None:
                return found
    for key in ("amount_total", "amount_due", "checkout_amount_minor"):
        found = _as_minor_int(payload.get(key))
        if found is not None:
            return found
    total = payload.get("total")
    if isinstance(total, Mapping):
        nested = total.get("total")
        if isinstance(nested, Mapping):
            found = _as_minor_int(nested.get("minorUnitsAmount") or nested.get("due"))
            if found is not None:
                return found
        for key in ("due", "minorUnitsAmount", "amount", "total"):
            found = _as_minor_int(total.get(key))
            if found is not None:
                return found
    items = payload.get("line_items") or payload.get("lineItems")
    if isinstance(items, list):
        running = 0
        saw = False
        for item in items:
            if not isinstance(item, Mapping):
                continue
            found = _as_minor_int(item.get("amount") or item.get("amount_total"))
            if found is None:
                continue
            running += found
            saw = True
        if saw:
            return running
    return None


def expected_amount_minor(payload: Any) -> int | None:
    """Extract Stripe/ChatGPT payable minor-unit amount from init/tax/checkout payloads."""
    if not isinstance(payload, dict):
        return None
    for wrapper in (payload, payload.get("checkout_session"), payload.get("session")):
        if not isinstance(wrapper, dict):
            continue
        state = wrapper.get("checkout_state")
        if isinstance(state, dict) and isinstance(state.get("total"), dict):
            total = state["total"]
            bucket = total.get("total") if isinstance(total.get("total"), dict) else total
            if bucket.get("minorUnitsAmount") is not None:
                found = _as_minor_int(bucket["minorUnitsAmount"])
                if found is not None:
                    return found
    snapshot = payload.get("snapshot")
    if isinstance(snapshot, dict):
        found = _as_minor_int(snapshot.get("amount_total") or snapshot.get("due"))
        if found is not None:
            return found
    for wrapper in (
        payload,
        payload.get("session"),
        payload.get("checkout_session"),
        payload.get("payment_page"),
    ):
        if isinstance(wrapper, dict):
            found = _expected_amount_from_object(wrapper)
            if found is not None:
                return found
    return None


def _coerce_decimal(value: Any, currency: str, *, minor: bool) -> Decimal | None:
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list)):
        return None
    if isinstance(value, str):
        text = value.replace(",", "").replace(currency, "").strip()
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, int):
        if minor and currency not in ZERO_DECIMAL_CURRENCIES:
            return (Decimal(value) / Decimal(100)).quantize(Decimal("0.01"))
        return Decimal(value)
    if isinstance(value, Decimal):
        return value
    return None


def _find_named(payload: Any, name: str, *, depth: int) -> Any:
    if depth > 6 or payload is None:
        return None
    if isinstance(payload, dict):
        if name in payload and payload[name] is not None:
            return payload[name]
        for value in payload.values():
            found = _find_named(value, name, depth=depth + 1)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_named(item, name, depth=depth + 1)
            if found is not None:
                return found
    return None


def _find_string(payload: Any, names: tuple[str, ...], *, depth: int) -> str:
    if depth > 6 or payload is None:
        return ""
    if isinstance(payload, dict):
        for key in names:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            found = _find_string(value, names, depth=depth + 1)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_string(item, names, depth=depth + 1)
            if found:
                return found
    return ""


def _minor_bucket(bucket: Any, currency: str) -> Decimal | None:
    if isinstance(bucket, dict) and bucket.get("minorUnitsAmount") is not None:
        return _coerce_decimal(bucket.get("minorUnitsAmount"), currency, minor=True)
    return None


def _checkout_state_amount(state: dict[str, Any], currency: str) -> Decimal | None:
    total = state.get("total")
    if isinstance(total, dict):
        for key in ("total", "subtotal", "amount"):
            parsed = _minor_bucket(total.get(key), currency)
            if parsed is not None:
                return parsed
        parsed = _coerce_decimal(total.get("minorUnitsAmount"), currency, minor=True)
        if parsed is not None:
            return parsed
    items = state.get("lineItems")
    if isinstance(items, list) and items:
        first = items[0]
        if isinstance(first, dict):
            for key in ("subtotal", "total", "amount"):
                parsed = _minor_bucket(first.get(key), currency)
                if parsed is not None:
                    return parsed
    return None


def _quote_from_payload_str(payload: Any, default_currency: str = "") -> tuple[str | None, str]:
    if _NATIVE_QUOTE_FROM_PAYLOAD_STR is not None and isinstance(default_currency, _NATIVE_SCALAR_TYPES):
        try:
            amount_str, cur = _NATIVE_QUOTE_FROM_PAYLOAD_STR(payload, default_currency)
            return (str(amount_str) if amount_str is not None else None), str(cur)
        except TypeError:
            pass
    currency = (
        (_find_string(payload, ("currency", "currency_code"), depth=0) or str(default_currency or "")).strip().upper()
    )
    if isinstance(payload, dict):
        top = payload.get("amount_total")
        if top is not None:
            parsed = _coerce_decimal(top, currency, minor=True)
            if parsed is not None:
                return str(parsed), currency
        state = payload.get("checkout_state")
        if isinstance(state, dict):
            parsed = _checkout_state_amount(state, currency)
            if parsed is not None:
                return str(parsed), currency
    for name in _MINOR_NAMES:
        found = _find_named(payload, name, depth=0)
        if found is not None:
            parsed = _coerce_decimal(found, currency, minor=True)
            if parsed is not None:
                return str(parsed), currency
    total = _find_named(payload, "total", depth=0)
    if isinstance(total, dict):
        for name in ("taxInclusive", "tax_inclusive", "total", "amount"):
            if name not in total:
                continue
            value = total[name]
            parsed = _coerce_decimal(value, currency, minor=isinstance(value, int))
            if parsed is not None:
                return str(parsed), currency
    for name in ("total_amount", "display_amount", "amount", "total"):
        found = _find_named(payload, name, depth=0)
        if found is None or isinstance(found, (dict, list)):
            continue
        parsed = _coerce_decimal(found, currency, minor=False)
        if parsed is not None:
            return str(parsed), currency
    return None, currency


def quote_from_payload(payload: Any, default_currency: str = "") -> tuple[Decimal | None, str]:
    """Extract ``(Decimal | None, CURRENCY)`` from a ChatGPT/Stripe checkout payload."""
    amount_str, currency = _quote_from_payload_str(payload, default_currency)
    if amount_str is None:
        return None, currency
    try:
        return Decimal(amount_str), currency
    except InvalidOperation:
        return None, currency


# ─── Card validation & secret scrubbing ─────────────────────────────────────

_CARD_SECRET_KEYS = ("number", "cvc", "exp_month", "exp_year")


def normalize_card(card: Mapping[str, Any] | None) -> dict[str, str]:
    """Validate card fields strictly; error messages never echo PAN or CVC."""
    if not isinstance(card, Mapping):
        raise ValueError("missing card payload")
    number = str(card.get("number") or "").replace(" ", "").replace("-", "")
    cvc = str(card.get("cvc") or "").strip()
    month = str(card.get("exp_month") or "").strip()
    year = str(card.get("exp_year") or "").strip()
    if not number.isascii() or not number.isdigit() or not 12 <= len(number) <= 19:
        raise ValueError("invalid card number format")
    if not cvc.isascii() or not cvc.isdigit() or not 3 <= len(cvc) <= 4:
        raise ValueError("invalid card CVC format")
    if not month.isascii() or not month.isdigit() or not 1 <= int(month) <= 12:
        raise ValueError("invalid card expiry month")
    if not year.isascii() or not year.isdigit() or len(year) not in (2, 4):
        raise ValueError("invalid card expiry year")
    if len(year) == 2:
        year = "20" + year
    return {"number": number, "cvc": cvc, "exp_month": str(int(month)), "exp_year": year}


def scrub_card_secrets(message: Any, card: Mapping[str, Any] | None = None) -> str:
    """Remove any card secret substrings from an upstream message before logging."""
    text = str(message or "")
    if isinstance(card, Mapping):
        for key in _CARD_SECRET_KEYS:
            val = str(card.get(key) or "")
            if len(val) >= 3 and val in text:
                text = text.replace(val, "[REDACTED]")
    return text[:400]


# ─── 25-region billing catalog (countries + addresses + timezone/locale) ────

BILLING_REGIONS: tuple[tuple[str, str, str, str, str], ...] = (
    ("US", "美国", "USD", "en-US", "America/New_York"),
    ("PH", "菲律宾", "PHP", "en-PH", "Asia/Manila"),
    ("CL", "智利", "CLP", "es-CL", "America/Santiago"),
    ("EG", "埃及", "EGP", "ar-EG", "Africa/Cairo"),
    ("JP", "日本", "JPY", "ja-JP", "Asia/Tokyo"),
    ("GB", "英国", "GBP", "en-GB", "Europe/London"),
    ("TR", "土耳其", "TRY", "tr-TR", "Europe/Istanbul"),
    ("BR", "巴西", "BRL", "pt-BR", "America/Sao_Paulo"),
    ("MX", "墨西哥", "MXN", "es-MX", "America/Mexico_City"),
    ("IN", "印度", "INR", "en-IN", "Asia/Kolkata"),
    ("ID", "印尼", "IDR", "id-ID", "Asia/Jakarta"),
    ("VN", "越南", "VND", "vi-VN", "Asia/Ho_Chi_Minh"),
    ("TH", "泰国", "THB", "th-TH", "Asia/Bangkok"),
    ("MY", "马来西亚", "MYR", "en-MY", "Asia/Kuala_Lumpur"),
    ("SG", "新加坡", "SGD", "en-SG", "Asia/Singapore"),
    ("KR", "韩国", "KRW", "ko-KR", "Asia/Seoul"),
    ("AU", "澳大利亚", "AUD", "en-AU", "Australia/Sydney"),
    ("CA", "加拿大", "CAD", "en-CA", "America/Toronto"),
    ("DE", "德国", "EUR", "de-DE", "Europe/Berlin"),
    ("NL", "荷兰", "EUR", "nl-NL", "Europe/Amsterdam"),
    ("PL", "波兰", "PLN", "pl-PL", "Europe/Warsaw"),
    ("AR", "阿根廷", "ARS", "es-AR", "America/Argentina/Buenos_Aires"),
    ("SA", "沙特", "SAR", "ar-SA", "Asia/Riyadh"),
    ("AE", "阿联酋", "AED", "en-AE", "Asia/Dubai"),
    ("ZA", "南非", "ZAR", "en-ZA", "Africa/Johannesburg"),
)

_REGION_BY_CODE = {row[0]: row for row in BILLING_REGIONS}

_FIRST_NAMES = ("James", "David", "Michael", "Daniel", "Emma", "Olivia", "Sofia", "Maria")
_LAST_NAMES = ("Miller", "Johnson", "Garcia", "Santos", "Kim", "Tanaka", "Silva", "Nguyen")

_BILLING_PLACES: dict[str, tuple[dict[str, Any], ...]] = {
    "US": (
        {"city": "New York", "state": "NY", "postal_code": "10001", "streets": ("West 34th Street", "Broadway")},
        {
            "city": "Los Angeles",
            "state": "CA",
            "postal_code": "90012",
            "streets": ("South Spring Street", "West 5th Street"),
        },
        {
            "city": "Chicago",
            "state": "IL",
            "postal_code": "60601",
            "streets": ("North Michigan Avenue", "East Wacker Drive"),
        },
    ),
    "PH": (
        {"city": "Makati", "state": "NCR", "postal_code": "1226", "streets": ("Ayala Avenue", "Paseo de Roxas")},
        {"city": "Quezon City", "state": "NCR", "postal_code": "1100", "streets": ("EDSA", "Quezon Avenue")},
    ),
    "CL": ({"city": "Santiago", "state": "RM", "postal_code": "8320000", "streets": ("Avenida Providencia",)},),
    "EG": (
        {"city": "Cairo", "state": "C", "postal_code": "11511", "streets": ("Tahrir Square", "Kasr El Nil Street")},
    ),
    "JP": (
        {"city": "Shibuya", "state": "Tokyo", "postal_code": "150-0002", "streets": ("Dogenzaka", "Jingumae")},
        {"city": "Chuo", "state": "Osaka", "postal_code": "541-0041", "streets": ("Bingo-machi", "Kitahama")},
    ),
    "GB": (
        {"city": "London", "state": "ENG", "postal_code": "SW1A 1AA", "streets": ("Baker Street", "Oxford Street")},
    ),
    "TR": (
        {"city": "Istanbul", "state": "34", "postal_code": "34433", "streets": ("Istiklal Caddesi", "Bagdat Caddesi")},
    ),
    "BR": (
        {
            "city": "Sao Paulo",
            "state": "SP",
            "postal_code": "01310-100",
            "streets": ("Avenida Paulista", "Rua Augusta"),
        },
    ),
    "MX": ({"city": "Ciudad de Mexico", "state": "CMX", "postal_code": "06600", "streets": ("Paseo de la Reforma",)},),
    "IN": ({"city": "Mumbai", "state": "MH", "postal_code": "400001", "streets": ("Marine Drive",)},),
    "ID": (
        {"city": "Jakarta", "state": "JK", "postal_code": "12190", "streets": ("Jl. Jend. Sudirman", "Jl. MH Thamrin")},
    ),
    "VN": (
        {"city": "Ho Chi Minh City", "state": "SG", "postal_code": "700000", "streets": ("Dong Khoi", "Nguyen Hue")},
    ),
    "TH": ({"city": "Bangkok", "state": "10", "postal_code": "10330", "streets": ("Sukhumvit Road", "Silom Road")},),
    "MY": ({"city": "Kuala Lumpur", "state": "KL", "postal_code": "50088", "streets": ("Jalan Ampang",)},),
    "SG": (
        {"city": "Singapore", "state": "SG", "postal_code": "018956", "streets": ("Raffles Place", "Orchard Road")},
    ),
    "KR": ({"city": "Seoul", "state": "11", "postal_code": "04524", "streets": ("Jongno", "Gangnam-daero")},),
    "AU": ({"city": "Sydney", "state": "NSW", "postal_code": "2000", "streets": ("George Street", "Pitt Street")},),
    "CA": ({"city": "Toronto", "state": "ON", "postal_code": "M5V 2T6", "streets": ("King Street West",)},),
    "DE": ({"city": "Berlin", "state": "BE", "postal_code": "10115", "streets": ("Unter den Linden",)},),
    "NL": ({"city": "Amsterdam", "state": "NH", "postal_code": "1012 JS", "streets": ("Damrak", "Kalverstraat")},),
    "PL": ({"city": "Warsaw", "state": "MZ", "postal_code": "00-001", "streets": ("Nowy Swiat", "Marszalkowska")},),
    "AR": ({"city": "Buenos Aires", "state": "C", "postal_code": "C1002", "streets": ("Calle Florida",)},),
    "SA": ({"city": "Riyadh", "state": "01", "postal_code": "12211", "streets": ("King Fahd Road", "Olaya Street")},),
    "AE": ({"city": "Dubai", "state": "DU", "postal_code": "00000", "streets": ("Sheikh Zayed Road", "Al Wasl Road")},),
    "ZA": ({"city": "Cape Town", "state": "WC", "postal_code": "8001", "streets": ("Long Street", "Adderley Street")},),
}


def billing_currency_for(country: str) -> str:
    row = _REGION_BY_CODE.get(str(country or "").strip().upper())
    return row[2] if row else "USD"


def billing_locale_for(country: str) -> str:
    row = _REGION_BY_CODE.get(str(country or "").strip().upper())
    return row[3] if row else "en-US"


def billing_timezone_for(country: str) -> str:
    row = _REGION_BY_CODE.get(str(country or "").strip().upper())
    return row[4] if row else "America/New_York"


def billing_address_for(country: str) -> dict[str, str]:
    """Generate a synthetic region-consistent Stripe billing address."""
    code = str(country or "").strip().upper() or "US"
    places = _BILLING_PLACES.get(code) or _BILLING_PLACES["US"]
    place = secrets.choice(places)
    street = secrets.choice(place["streets"])
    number = secrets.randbelow(180) + 12
    name = f"{secrets.choice(_FIRST_NAMES)} {secrets.choice(_LAST_NAMES)}"
    return {
        "name": name,
        "line1": f"{number} {street}",
        "city": str(place["city"]),
        "state": str(place["state"]),
        "postal_code": str(place["postal_code"]),
        "country": code if code in _BILLING_PLACES else "US",
    }


def stripe_billing_details(address: Mapping[str, str]) -> dict[str, Any]:
    return {
        "name": address.get("name") or "",
        "address": {
            "line1": address.get("line1") or "",
            "city": address.get("city") or "",
            "state": address.get("state") or "",
            "postal_code": address.get("postal_code") or "",
            "country": address.get("country") or "",
        },
    }


# ─── Stripe / ChatGPT ID parsers & outcome classification ───────────────────

_CS_ID_RE = re.compile(r"cs_(?:live|test)_[A-Za-z0-9_]+")
_OAICS_ID_RE = re.compile(r"oaics_[A-Za-z0-9_]+")
_CONFIRM_BLOCKED = frozenset({"blocked", "failed", "rejected", "not_approved", "declined"})
_FAILURE_KEYS = ("decline_code", "code", "message", "reason", "failure_reason", "failure_code", "type")

_DECLINE_ADVICE: dict[str, str] = {
    "do_not_honor": "发卡行未说明原因。请联系银行确认该卡已开通境外/网上/订阅扣款、额度够用，并核对账单地址与卡登记地址一致；先别连续重试，可换一张卡。",
    "generic_decline": "发卡行未说明原因。请联系银行确认该卡可用于境外网上扣款，再重试或换卡。",
    "insufficient_funds": "余额或额度不足，请充值后重试或换卡。",
    "card_velocity_exceeded": "短时间内尝试次数或金额超限，请稍后再试，或联系银行放宽限额。",
    "lost_card": "发卡行标记该卡已挂失，请换卡。",
    "stolen_card": "发卡行标记该卡不可用，请换卡。",
    "expired_card": "卡已过期，请换卡。",
    "incorrect_cvc": "安全码（CVC）不正确，请重新输入。",
    "incorrect_number": "卡号不正确，请重新输入。",
    "invalid_cvc": "安全码（CVC）不正确，请重新输入。",
    "invalid_expiry_month": "有效期月份不正确，请重新输入。",
    "invalid_expiry_year": "有效期年份不正确，请重新输入。",
    "authentication_required": "发卡行要求 3DS 验证，请在银行 App 或短信里完成验证后重试。",
    "card_not_supported": "这张卡不支持该类扣款，请换卡。",
    "currency_not_supported": "这张卡不支持当前币种，请联系银行开通或换卡。",
    "transaction_not_allowed": "发卡行不允许这类交易，请联系银行或换卡。",
    "pickup_card": "发卡行要求不再使用该卡，请换卡。",
    "fraudulent": "发卡行判定为风险交易，请联系银行确认后再试。",
}


def public_checkout_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("http"):
        text = text.rstrip("/").split("/")[-1]
    return text.split("?")[0].split("#")[0]


def stripe_checkout_id(value: str) -> str:
    match = _CS_ID_RE.search(str(value or ""))
    return match.group(0) if match else ""


def openai_internal_checkout_id(value: str) -> str:
    match = _OAICS_ID_RE.search(str(value or ""))
    return match.group(0) if match else ""


def processor_from_checkout_url(url: str) -> str:
    parts = [part for part in str(url or "").split("/") if part]
    if "checkout" not in parts:
        return ""
    index = parts.index("checkout")
    if index + 1 >= len(parts):
        return ""
    candidate = parts[index + 1]
    if candidate in {"verify", ""} or candidate.startswith("oaics_"):
        return ""
    return candidate


def intent_id_from_secret(secret: str) -> str:
    text = str(secret or "").strip()
    if "_secret_" not in text:
        return ""
    prefix = text.split("_secret_", 1)[0]
    return prefix if prefix.startswith(("pi_", "seti_")) else ""


def checkout_confirm_blocked(payload: Mapping[str, Any]) -> bool:
    status = str(payload.get("status") or payload.get("result") or "").strip().lower()
    return status in _CONFIRM_BLOCKED


def checkout_confirm_message(payload: Mapping[str, Any]) -> str:
    for key in ("message", "detail", "reason", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip() and value.strip().lower() not in _CONFIRM_BLOCKED:
            return value.strip()
        if isinstance(value, Mapping):
            nested = value.get("message") or value.get("code") or value.get("reason")
            if nested:
                return str(nested)
    return ""


def _intent_object(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for key in ("payment_intent", "setup_intent"):
        value = payload.get(key)
        if isinstance(value, dict) and (value.get("id") or value.get("client_secret") or value.get("status")):
            return value
    for holder_key in ("submission_attempt", "elements_session"):
        holder = payload.get(holder_key)
        if isinstance(holder, dict):
            nested = _intent_object(holder)
            if nested:
                return nested
    return {}


def payment_intent_from_confirm(payload: Mapping[str, Any]) -> tuple[str, str, str]:
    nested = _intent_object(dict(payload) if not isinstance(payload, dict) else payload)
    secret = str(payload.get("client_secret") or nested.get("client_secret") or "")
    intent_id = str(nested.get("id") or "")
    raw = payload.get("payment_intent")
    if not intent_id and isinstance(raw, str) and raw.startswith(("pi_", "seti_")):
        intent_id = raw
    if intent_id.startswith("cs_"):
        intent_id = ""
    intent_id = intent_id or intent_id_from_secret(secret)
    return_url = str(payload.get("confirm_return_url") or payload.get("return_url") or "")
    return intent_id, secret, return_url


def submission_state(payload: Mapping[str, Any]) -> str:
    attempt = payload.get("submission_attempt")
    if not isinstance(attempt, Mapping):
        return ""
    return str(attempt.get("state") or attempt.get("status") or "").strip().lower()


def nested_intent_status(payload: Mapping[str, Any]) -> str:
    nested = _intent_object(dict(payload) if not isinstance(payload, dict) else payload)
    return str(nested.get("status") or "").strip().lower()


def payment_page_settled(payload: Mapping[str, Any]) -> bool:
    status = str(payload.get("status") or payload.get("payment_object_status") or "").strip().lower()
    state = submission_state(payload)
    pi_status = nested_intent_status(payload)
    if status in {"complete", "completed", "expired", "succeeded", "requires_action"}:
        return True
    if state in {"succeeded", "complete", "completed", "failed", "declined", "canceled", "cancelled"}:
        return True
    return pi_status in {
        "succeeded",
        "processing",
        "requires_action",
        "requires_payment_method",
        "canceled",
        "cancelled",
    }


def _failure_parts(source: Any) -> list[str]:
    if isinstance(source, str):
        return [source.strip()] if source.strip() else []
    if not isinstance(source, dict):
        return []
    parts: list[str] = []
    for key in _FAILURE_KEYS:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}={value.strip()}")
    return parts


def submission_failure_detail(payload: Any) -> str:
    """Extract deduplicated failure reasons from nested Stripe confirm/poll payloads."""
    if not isinstance(payload, dict):
        return ""
    raw_attempt: object = payload.get("submission_attempt")
    attempt: dict[str, Any] = raw_attempt if isinstance(raw_attempt, dict) else {}
    nested = _intent_object(payload)
    sources = (
        attempt.get("error"),
        attempt.get("last_error"),
        attempt.get("failure"),
        attempt,
        payload.get("last_payment_error"),
        nested.get("last_payment_error"),
        payload.get("error"),
    )
    seen: list[str] = []
    for source in sources:
        for part in _failure_parts(source):
            if part not in seen:
                seen.append(part)
    return "; ".join(seen[:6])


def decline_message(detail: Any) -> str:
    """Map a Stripe decline code to operator-friendly Chinese advice while preserving the code."""
    code = str(detail or "").strip()
    advice = _DECLINE_ADVICE.get(code.lower())
    return f"发卡行拒绝: {code}。{advice}" if advice else f"发卡行拒绝: {code}"


_UNKNOWN_CONFIRM_CODES = frozenset({"payment_outcome_unknown", "stripe_transport_error"})
_GATEWAY_UNKNOWN_HTTP = frozenset({408, 500, 502, 503, 504})


def classify_stripe_outcome(confirm: Any, poll: Any = None) -> dict[str, Any]:
    """Classify a Stripe Hosted/Custom Checkout confirm + optional poll response."""
    out: dict[str, Any] = {
        "state": "unknown",
        "code": "",
        "decline_code": "",
        "message": "",
        "intent_status": "",
        "charge_id": "",
        "payment_method_id": "",
        "challenge_type": "",
    }

    def scan(obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        top_err = obj.get("last_payment_error") or obj.get("last_setup_error")
        if isinstance(top_err, dict):
            out["code"] = out["code"] or str(top_err.get("code") or "")
            out["decline_code"] = out["decline_code"] or str(top_err.get("decline_code") or "")
            out["message"] = out["message"] or str(top_err.get("message") or "")[:200]
            if top_err.get("charge"):
                out["charge_id"] = out["charge_id"] or str(top_err["charge"])
            pm = top_err.get("payment_method")
            if isinstance(pm, dict) and pm.get("id"):
                out["payment_method_id"] = out["payment_method_id"] or str(pm["id"])
        if obj.get("status") and not out["intent_status"]:
            out["intent_status"] = str(obj["status"])
        for holder in ("payment_intent", "setup_intent"):
            sub = obj.get(holder)
            if isinstance(sub, dict):
                if sub.get("status"):
                    out["intent_status"] = str(sub["status"])
                na = sub.get("next_action")
                if isinstance(na, dict) and not out["challenge_type"]:
                    sdk = na.get("use_stripe_sdk")
                    if isinstance(sdk, dict) and sdk.get("type"):
                        out["challenge_type"] = str(sdk["type"])
                    elif na.get("type"):
                        out["challenge_type"] = str(na["type"])
                err = sub.get("last_payment_error") or sub.get("last_setup_error")
                if isinstance(err, dict):
                    out["code"] = str(err.get("code") or "") or out["code"]
                    out["decline_code"] = str(err.get("decline_code") or "") or out["decline_code"]
                    if err.get("message"):
                        out["message"] = str(err["message"])[:200]
                    if err.get("charge"):
                        out["charge_id"] = str(err["charge"])
                    pm = err.get("payment_method")
                    if isinstance(pm, dict) and pm.get("id"):
                        out["payment_method_id"] = str(pm["id"])
        err = obj.get("error")
        if isinstance(err, dict):
            out["code"] = out["code"] or str(err.get("code") or "")
            out["decline_code"] = out["decline_code"] or str(err.get("decline_code") or "")
            out["message"] = out["message"] or str(err.get("message") or "")[:200]
            if err.get("charge"):
                out["charge_id"] = out["charge_id"] or str(err["charge"])
            pm = err.get("payment_method")
            if isinstance(pm, dict) and pm.get("id"):
                out["payment_method_id"] = out["payment_method_id"] or str(pm["id"])

    confirm_dict = confirm if isinstance(confirm, dict) else {}
    poll_dict = poll if isinstance(poll, dict) else {}
    scan(confirm_dict.get("body") if "body" in confirm_dict else confirm_dict)
    scan(poll_dict)

    status = out["intent_status"].lower()
    raw_http = confirm_dict.get("status")
    confirm_http = raw_http if isinstance(raw_http, int) and not isinstance(raw_http, bool) else 0
    poll_state = str(poll_dict.get("state") or poll_dict.get("payment_object_status") or "").lower()
    err_code = out["code"].lower()
    gateway_unknown = not out["decline_code"] and (
        confirm_http in _GATEWAY_UNKNOWN_HTTP or err_code.startswith("stripe_http_5") or err_code == "stripe_http_408"
    )
    if status in {"succeeded", "requires_capture"} or poll_state == "succeeded":
        out["state"] = "paid"
    elif err_code in _UNKNOWN_CONFIRM_CODES or gateway_unknown:
        out["state"] = "unknown"
    elif (
        err_code == "checkout_confirm_error"
        or confirm_http in {400, 429}
        or err_code in {"stripe_http_400", "stripe_http_429"}
    ):
        out["state"] = "confirm_error"
    elif out["decline_code"] or out["code"]:
        out["state"] = "challenge_required" if "captcha" in out["message"].lower() else "card_declined"
    elif status == "requires_action":
        out["state"] = "challenge_required"
    elif poll_state in {"failed", "canceled", "expired"}:
        out["state"] = "card_declined"
    elif status in {"processing", "pending"} or poll_state == "processing":
        out["state"] = "pending"
    return out


__all__ = [
    "BILLING_REGIONS",
    "RVBuildFields",
    "ZERO_DECIMAL_CURRENCIES",
    "billing_address_for",
    "billing_currency_for",
    "billing_locale_for",
    "billing_timezone_for",
    "checkout_confirm_blocked",
    "checkout_confirm_message",
    "classify_stripe_outcome",
    "decode_rv_timestamp",
    "decline_message",
    "encode_rv_timestamp",
    "expected_amount_minor",
    "extract_rv_build_fields",
    "intent_id_from_secret",
    "minor_units_from_amount",
    "nested_intent_status",
    "normalize_card",
    "openai_internal_checkout_id",
    "payment_intent_from_confirm",
    "payment_page_settled",
    "processor_from_checkout_url",
    "public_checkout_id",
    "quote_from_payload",
    "rv_timestamp_from_script",
    "scrub_card_secrets",
    "stripe_billing_details",
    "stripe_checkout_id",
    "submission_failure_detail",
    "submission_state",
]
