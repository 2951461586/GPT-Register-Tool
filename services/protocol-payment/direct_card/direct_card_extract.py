#!/usr/bin/env python3
"""Standalone ChatGPT checkout short-link extractor.

Author: Simon

Default flow:

* checkout billing: PH / PHP
* checkout proxy country: US
* promotion update proxy country: TR
* payment locale: English
* proxy provider: kookeey password-template routing (``<base>-<CC>[-<session>[-<ttl>]]``)
* output: https://chatgpt.com/checkout/<processor_entity>/<oaics_id>

The script creates the checkout without a promotion, applies the promotion to
the same checkout through ``checkout/update``, and verifies the payable amount.
An explicit non-zero amount is rejected. If neither the update response nor the
checkout page exposes an amount, the result is returned as ``pending`` so a
payment client can verify it before payment.

Only ``curl_cffi`` is required::

    python -m pip install "curl_cffi>=0.15.0"

Recommended usage keeps credentials out of the process list::

    export DIRECT_CARD_CHECKOUT_PROXY='http://user:pass-CC-session-ttl@gate.kookeey.info:1000'
    export DIRECT_CARD_UPDATE_PROXY='http://user:pass-CC-session-ttl@gate.kookeey.info:1000'
    python direct_card_extract.py --credential-file session.json --pretty

Use ``--credential-file -`` to read a token or session JSON from stdin.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import random
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from html import unescape as html_unescape
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlsplit, urlunsplit

try:
    from curl_cffi.requests import Session as CurlSession
except ImportError:  # pragma: no cover - exercised by the CLI error path
    CurlSession = None  # type: ignore[assignment]


VERSION = "1.0.0"
CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
UPDATE_PATH = "/backend-api/payments/checkout/update"
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
DEFAULT_CLIENT_VERSION = "prod-db390ebea64862bf1899c420a4c736e0cf639747"
DEFAULT_CLIENT_BUILD = "7904904"
PAYMENT_COOKIE_NAMES = frozenset(
    {
        "oai-did",
        "oai-hlib",
        "oai-sc",
        "oaicom-stable-id",
        "_account",
        "_account_is_fedramp",
        "__Secure-oai-is",
        "__Secure-next-auth.session-token",
        "__cf_bm",
        "__cflb",
        "_cfuvid",
        "__oailb",
        "cf_clearance",
    }
)
RETRYABLE_STATUSES = frozenset({403, 429, 500, 502, 503, 504})


class ExtractorError(RuntimeError):
    """Base error safe to expose in the CLI JSON response."""


class UpstreamError(ExtractorError):
    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        retryable: bool = False,
        cloudflare: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.cloudflare = cloudflare


class InvalidPromotionError(ExtractorError):
    pass


class NonZeroAmountError(ExtractorError):
    pass


@dataclass(frozen=True)
class Credentials:
    access_token: str
    session_cookie: str = ""


@dataclass(frozen=True)
class ExtractorConfig:
    billing_country: str = "PH"
    currency: str = "PHP"
    payment_locale: str = "en"
    checkout_proxy_country: str = "US"
    update_proxy_country: str = "TR"
    checkout_proxy: str = ""
    update_proxy: str = ""
    plan_name: str = "chatgptplusplan"
    promo_campaign_id: str = "plus-1-month-free"
    checkout_ui_mode: str = "custom"
    timeout: float = 45.0
    checkout_attempts: int = 10
    update_attempts: int = 15
    full_attempts: int = 3
    cf_same_identity_attempts: int = 5
    cf_retry_delay: float = 3.0
    verify_proxy_country: bool = True
    user_agent: str = DEFAULT_USER_AGENT

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Z]{2}", self.billing_country):
            raise ExtractorError("billing country must be a two-letter code")
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ExtractorError("currency must be a three-letter code")
        for field_name, value in (
            ("checkout proxy country", self.checkout_proxy_country),
            ("update proxy country", self.update_proxy_country),
        ):
            if not re.fullmatch(r"[A-Z]{2}", value):
                raise ExtractorError(f"{field_name} must be a two-letter code")
        for field_name, value in (
            ("checkout attempts", self.checkout_attempts),
            ("update attempts", self.update_attempts),
            ("full attempts", self.full_attempts),
            ("CF same-identity attempts", self.cf_same_identity_attempts),
        ):
            if value < 1:
                raise ExtractorError(f"{field_name} must be at least 1")


@dataclass(frozen=True)
class CheckoutResult:
    ok: bool
    long_url: str
    cs_id: str
    processor_entity: str
    billing_country: str
    currency: str
    payment_locale: str
    amount_verification: str
    amount_minor: int | None
    amount_currency: str


def find_value(payload: Any, keys: tuple[str, ...]) -> str:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            found = find_value(value, keys)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_value(value, keys)
            if found:
                return found
    return ""


def parse_credentials(raw: str, explicit_session_cookie: str = "") -> Credentials:
    raw = str(raw or "").strip()
    if not raw:
        raise ExtractorError("access token or session JSON is required")
    payload: Any = None
    if raw.startswith(("{", "[")):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ExtractorError(f"credential JSON is invalid: {exc.msg}") from exc
        access_token = find_value(payload, ("accessToken", "access_token", "token"))
    else:
        access_token = raw
    if not access_token:
        raise ExtractorError("credential payload does not contain an access token")
    session_cookie = str(explicit_session_cookie or "").strip()
    if not session_cookie and payload is not None:
        session_cookie = find_value(
            payload,
            (
                "chatgpt_session_cookie",
                "chatgptSessionCookie",
                "session_cookie",
                "sessionCookie",
                "sessionToken",
                "__Secure-next-auth.session-token",
            ),
        )
    return Credentials(access_token=access_token, session_cookie=session_cookie)


def cookie_header_value(cookie_header: str, name: str) -> str:
    for item in str(cookie_header or "").split(";"):
        key, separator, value = item.strip().partition("=")
        if separator and key == name:
            return value.strip()
    return ""


def filter_payment_cookie_header(cookie_header: str) -> str:
    values: dict[str, str] = {}
    for item in str(cookie_header or "").split(";"):
        name, separator, value = item.strip().partition("=")
        if separator and name in PAYMENT_COOKIE_NAMES:
            values[name] = value
    return "; ".join(f"{name}={value}" for name, value in values.items())


def replace_cookie_value(cookie_header: str, name: str, value: str) -> str:
    values: list[str] = []
    replaced = False
    for item in str(cookie_header or "").split(";"):
        key, separator, _ = item.strip().partition("=")
        if not separator:
            continue
        if key == name:
            if not replaced:
                values.append(f"{name}={value}")
                replaced = True
        else:
            values.append(item.strip())
    if not replaced:
        values.insert(0, f"{name}={value}")
    return "; ".join(values)


def initial_cookie_header(session_cookie: str, device_id: str) -> str:
    raw_cookie = str(session_cookie or "").strip()
    if raw_cookie and "=" not in raw_cookie.split(";", 1)[0]:
        return f"oai-did={device_id}; __Secure-next-auth.session-token={raw_cookie}"
    filtered = filter_payment_cookie_header(raw_cookie)
    if not filtered:
        return f"oai-did={device_id}"
    return replace_cookie_value(filtered, "oai-did", device_id)


def merged_cookie_header(cookie_header: str, cookies: Any) -> str:
    values: dict[str, str] = {}
    for item in str(cookie_header or "").split(";"):
        name, separator, value = item.strip().partition("=")
        if separator and name:
            values[name] = value
    try:
        for name, value in cookies.items():
            if name:
                values[str(name)] = str(value)
    except Exception:
        pass
    return "; ".join(f"{name}={value}" for name, value in values.items())


def refresh_cookie_header(session: Any, fallback: str = "") -> None:
    headers = getattr(session, "headers", None)
    if headers is None:
        return
    cookie_header = filter_payment_cookie_header(
        merged_cookie_header(
            fallback or str(headers.get("Cookie") or ""),
            getattr(session, "cookies", None),
        )
    )
    if cookie_header:
        headers["Cookie"] = cookie_header


def sync_cookies(target: Any, source: Any) -> None:
    if target is source:
        refresh_cookie_header(target)
        return
    try:
        target.cookies.update(source.cookies)
    except Exception:
        pass
    refresh_cookie_header(target, str(getattr(source, "headers", {}).get("Cookie") or ""))


def normalize_proxy_url(proxy: str) -> str:
    proxy = str(proxy or "").strip()
    if not proxy:
        return ""
    if "://" in proxy:
        return proxy
    parts = proxy.split(":", 3)
    if len(parts) == 4 and parts[1].isdigit() and "@" not in proxy:
        host, port, username, password = parts
        return (
            f"http://{quote(username, safe='-._~')}:"
            f"{quote(password, safe='-._~')}@{host}:{port}"
        )
    return f"http://{proxy}"


def new_proxy_session_id(length: int = 8) -> str:
    """Random numeric session id preserving the original id length (min 1)."""
    size = max(1, int(length or 8))
    return "".join(str(random.randint(0, 9)) for _ in range(size))


def rotate_proxy_session(proxy: str, country: str) -> str:
    """Rotate a kookeey sticky proxy to a fresh session for ``country``.

    Password template: ``<base>-<CC>-<session>-<ttl>`` (TTL like ``5m``/``30s``/
    ``1h``/``1d``), mirroring ``sms_tool.proxy_entry.rotate_session``: the country
    code is rewritten, the session id replaced with a same-length numeric id,
    and the TTL preserved. Non-sticky or TTL-less passwords are returned
    unchanged so rotation stays a no-op instead of corrupting credentials.
    """
    proxy = normalize_proxy_url(proxy)
    if not proxy:
        return proxy
    parsed = urlsplit(proxy)
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    hostname = parsed.hostname or ""
    if not password or not hostname:
        return proxy
    match = re.fullmatch(
        r"(.+?)-([A-Za-z]{2})-([A-Za-z0-9]+)-(\d+[smhd])",
        password,
    )
    if not match:
        return proxy
    base, _cc, session, ttl = match.groups()
    replacement = new_proxy_session_id(len(session)) if session.isdigit() else session
    rotated_password = f"{base}-{country.upper()}-{replacement}-{ttl}"
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    netloc = (
        f"{quote(username, safe='-._~')}:"
        f"{quote(rotated_password, safe='-._~')}@{host}"
    )
    return urlunsplit((parsed.scheme or "http", netloc, parsed.path, parsed.query, parsed.fragment))


def set_proxy(session: Any, proxy: str) -> None:
    proxy = normalize_proxy_url(proxy)
    session.proxies = {"http": proxy, "https": proxy} if proxy else {}


def safe_close(session: Any) -> None:
    if session is None:
        return
    try:
        session.close()
    except Exception:
        pass


def money_minor_units(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, dict):
        for key in ("minorUnitsAmount", "minor_units_amount", "amount"):
            if value.get(key) is not None:
                return money_minor_units(value.get(key))
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    return None


def nested_value(payload: Any, path: tuple[str, ...]) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def checkout_amount_minor(data: Any) -> int | None:
    wrapper_keys = (
        "checkout_session",
        "checkoutSession",
        "session",
        "checkout",
        "data",
        "result",
        "payload",
        "response",
        "checkout_state",
        "checkoutState",
        "checkout_snapshot",
        "checkoutSnapshot",
    )
    amount_paths = (
        ("checkout_amount_minor",),
        ("total_summary", "due"),
        ("totalSummary", "due"),
        ("invoice", "amount_due"),
        ("invoice", "amountDue"),
        ("amount_due",),
        ("amountDue",),
        ("amount_total",),
        ("amountTotal",),
        ("total", "total"),
        ("total", "due"),
        ("total", "taxInclusive"),
        ("total", "taxInclusiveAmount"),
    )
    visited: set[int] = set()

    def find(payload: Any) -> int | None:
        if not isinstance(payload, dict) or id(payload) in visited:
            return None
        visited.add(id(payload))
        for path in amount_paths:
            amount = money_minor_units(nested_value(payload, path))
            if amount is not None:
                return amount
        line_items = payload.get("lineItems") or payload.get("line_items")
        if isinstance(line_items, list):
            total = 0
            found = False
            for item in line_items:
                if not isinstance(item, dict):
                    continue
                for key in ("total", "subtotal", "unitAmount", "unit_amount"):
                    amount = money_minor_units(item.get(key))
                    if amount is not None:
                        total += amount
                        found = True
                        break
            if found:
                return total
        for key in wrapper_keys:
            amount = find(payload.get(key))
            if amount is not None:
                return amount
        return None

    return find(data)


def checkout_currency(data: Any) -> str:
    visited: set[int] = set()

    def find(payload: Any) -> str:
        if not isinstance(payload, dict) or id(payload) in visited:
            return ""
        visited.add(id(payload))
        for key in ("currency", "currency_code", "currencyCode"):
            value = payload.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{3}", value.strip()):
                return value.strip().upper()
        for key in (
            "checkout_state",
            "checkoutState",
            "checkout_snapshot",
            "checkoutSnapshot",
            "checkout_session",
            "checkoutSession",
            "session",
            "checkout",
            "data",
            "result",
            "payload",
            "response",
            "total",
            "total_summary",
            "totalSummary",
        ):
            value = find(payload.get(key))
            if value:
                return value
        return ""

    return find(data)


def decode_react_router_payload(payload: Any) -> Any:
    if not isinstance(payload, list):
        return None
    resolved: dict[int, Any] = {}

    def resolve(reference: Any) -> Any:
        if not isinstance(reference, int):
            return reference
        if reference < 0 or reference >= len(payload):
            return None
        if reference in resolved:
            return resolved[reference]
        value = payload[reference]
        if isinstance(value, dict):
            output: dict[str, Any] = {}
            resolved[reference] = output
            for encoded_key, encoded_value in value.items():
                key_text = str(encoded_key)
                if not key_text.startswith("_") or not key_text[1:].isdigit():
                    continue
                key = resolve(int(key_text[1:]))
                if key is not None:
                    output[str(key)] = resolve(encoded_value)
            return output
        if isinstance(value, list):
            output_list: list[Any] = []
            resolved[reference] = output_list
            output_list.extend(resolve(item) for item in value)
            return output_list
        resolved[reference] = value
        return value

    return resolve(0)


def checkout_state_from_html(html: str) -> dict[str, Any]:
    def find_state(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            for key in ("checkout_state", "checkoutState"):
                state = payload.get(key)
                if isinstance(state, dict):
                    return state
            for value in payload.values():
                state = find_state(value)
                if state:
                    return state
            if checkout_amount_minor(payload) is not None and any(
                key in payload
                for key in ("total", "total_summary", "totalSummary", "lineItems", "line_items")
            ):
                return payload
        elif isinstance(payload, list):
            for value in payload:
                state = find_state(value)
                if state:
                    return state
        return {}

    def state_from_serialized(serialized: str) -> dict[str, Any]:
        try:
            decoded: Any = json.loads(serialized)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        if isinstance(decoded, list):
            state = find_state(decode_react_router_payload(decoded))
            if state:
                return state
        return find_state(decoded)

    enqueue_pattern = re.compile(
        r'window\.__reactRouterContext\.streamController\.enqueue\(("(?:\\.|[^"\\])*")\)',
        re.DOTALL,
    )
    chunks: list[str] = []
    for match in enqueue_pattern.finditer(str(html or "")):
        try:
            raw_payload = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(raw_payload, str):
            continue
        chunks.append(raw_payload)
        state = state_from_serialized(raw_payload)
        if state:
            return state
    if len(chunks) > 1:
        state = state_from_serialized("".join(chunks))
        if state:
            return state
    application_json_pattern = re.compile(
        r"<script\b[^>]*\btype=(['\"])application/json\1[^>]*>(.*?)</script>",
        re.IGNORECASE | re.DOTALL,
    )
    for match in application_json_pattern.finditer(str(html or "")):
        state = state_from_serialized(html_unescape(match.group(2)).strip())
        if state:
            return state
    return {}


def payload_has_invalid_promotion(payload: Any) -> bool:
    if isinstance(payload, dict):
        return any(payload_has_invalid_promotion(value) for value in payload.values())
    if isinstance(payload, list):
        return any(payload_has_invalid_promotion(value) for value in payload)
    return isinstance(payload, str) and payload.strip().lower() == "invalid_promotion"


def extract_processor_entity(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    direct = payload.get("processor_entity") or payload.get("processorEntity")
    if direct:
        return str(direct).strip()
    for key in ("checkout_session", "checkoutSession", "session", "checkout", "data"):
        found = extract_processor_entity(payload.get(key))
        if found:
            return found
    return ""


def processor_entity_for_country(country: str, processor_entity: str = "") -> str:
    if str(processor_entity or "").strip():
        return str(processor_entity).strip()
    return "openai_llc" if str(country or "").upper() == "US" else "openai_ie"


def checkout_short_url(cs_id: str, country: str, processor_entity: str = "") -> str:
    entity = processor_entity_for_country(country, processor_entity)
    return f"https://chatgpt.com/checkout/{entity}/{cs_id}"


def is_retryable_network_error(exc: Exception) -> bool:
    retryable_names = {"ReadTimeout", "ConnectTimeout", "ConnectionError", "Timeout", "SSLError"}
    if any(item.__name__ in retryable_names for item in type(exc).mro()):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "read timed out",
            "connect timed out",
            "connection aborted",
            "proxy connect aborted",
            "curl: (56)",
            "unexpected_eof",
            "eof occurred in violation of protocol",
            "max retries exceeded",
        )
    )


def response_error(response: Any, prefix: str) -> UpstreamError:
    status = int(getattr(response, "status_code", 0) or 0)
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("content-type") or "")
    server = str(headers.get("server") or "")
    body = str(getattr(response, "text", "") or "")
    lowered = body[:4000].lower()
    is_html = "html" in content_type.lower() or body.lstrip().lower().startswith(("<!doctype", "<html"))
    cloudflare = status == 403 and (
        "challenge-platform" in lowered
        or "__cf$cv$params" in lowered
        or "cloudflare" in server.lower()
        or is_html
    )
    if is_html:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
        detail = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else "HTML verification page"
    else:
        detail = re.sub(r"\s+", " ", body).strip()[:300] or "empty response"
    return UpstreamError(
        status,
        f"{prefix}: HTTP {status}; {detail}",
        retryable=status in RETRYABLE_STATUSES,
        cloudflare=cloudflare,
    )


class CheckoutExtractor:
    def __init__(
        self,
        credentials: Credentials,
        config: ExtractorConfig | None = None,
        *,
        session_factory: Callable[[], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.credentials = credentials
        self.config = config or ExtractorConfig()
        self.config.validate()
        if session_factory is None:
            if CurlSession is None:
                raise ExtractorError(
                    'curl_cffi is required; install it with: python -m pip install "curl_cffi>=0.15.0"'
                )
            session_factory = lambda: CurlSession(impersonate="chrome136")
        self.session_factory = session_factory
        self.sleep = sleeper
        self.log = logger or (lambda message: print(message, file=sys.stderr, flush=True))
        self.device_id = cookie_header_value(credentials.session_cookie, "oai-did") or str(uuid.uuid4())
        self.chatgpt_session_id = str(uuid.uuid4())

    def _new_identity_session(self, proxy: str) -> Any:
        session = self.session_factory()
        if hasattr(session, "trust_env"):
            session.trust_env = False
        session.headers.update(
            {
                "User-Agent": self.config.user_agent,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Authorization": f"Bearer {self.credentials.access_token}",
                "Origin": "https://chatgpt.com",
                "Referer": "https://chatgpt.com/",
                "Content-Type": "application/json",
                "oai-device-id": self.device_id,
                "oai-language": "en-US",
                "oai-session-id": self.chatgpt_session_id,
                "oai-client-version": DEFAULT_CLIENT_VERSION,
                "oai-client-build-number": DEFAULT_CLIENT_BUILD,
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "sec-ch-ua": '"Google Chrome";v="136", "Not.A/Brand";v="8", "Chromium";v="136"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "Cookie": initial_cookie_header(self.credentials.session_cookie, self.device_id),
            }
        )
        set_proxy(session, proxy)
        return session

    def _clone_session(self, source: Any, proxy: str) -> Any:
        session = self.session_factory()
        if hasattr(session, "trust_env"):
            session.trust_env = False
        session.headers.update(dict(getattr(source, "headers", {})))
        try:
            session.cookies.update(source.cookies)
        except Exception:
            pass
        refresh_cookie_header(session, str(getattr(source, "headers", {}).get("Cookie") or ""))
        set_proxy(session, proxy)
        return session

    def _stage_proxies(self) -> tuple[str, str]:
        checkout_proxy = normalize_proxy_url(self.config.checkout_proxy)
        update_proxy = normalize_proxy_url(self.config.update_proxy)
        if not checkout_proxy or not update_proxy:
            raise ExtractorError(
                "checkout and update proxies are required; pass --checkout-proxy/--update-proxy "
                "or set DIRECT_CARD_CHECKOUT_PROXY/DIRECT_CARD_UPDATE_PROXY"
            )
        return checkout_proxy, update_proxy

    def _probe_country(self, proxy: str, expected_country: str) -> None:
        session = self.session_factory()
        try:
            set_proxy(session, proxy)
            response = session.get(TRACE_URL, timeout=min(self.config.timeout, 12.0))
            if int(getattr(response, "status_code", 0) or 0) >= 400:
                raise response_error(response, "proxy country probe")
            fields = dict(
                line.split("=", 1)
                for line in str(getattr(response, "text", "") or "").splitlines()
                if "=" in line
            )
            observed = str(fields.get("loc") or "").upper()
            if observed != expected_country:
                raise ExtractorError(
                    f"proxy country mismatch: expected {expected_country}, observed {observed or 'unknown'}"
                )
        finally:
            safe_close(session)

    def _preflight(self, checkout_proxy: str, update_proxy: str) -> None:
        if not self.config.verify_proxy_country:
            return
        with ThreadPoolExecutor(max_workers=2) as executor:
            checkout_future = executor.submit(
                self._probe_country, checkout_proxy, self.config.checkout_proxy_country
            )
            update_future = executor.submit(
                self._probe_country, update_proxy, self.config.update_proxy_country
            )
            checkout_future.result()
            update_future.result()
        self.log(
            f"Proxy check passed: checkout={self.config.checkout_proxy_country}, "
            f"update={self.config.update_proxy_country}."
        )

    def _create_checkout(self, session: Any) -> dict[str, Any]:
        refresh_cookie_header(session)
        body = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": self.config.plan_name,
            "billing_details": {
                "country": self.config.billing_country,
                "currency": self.config.currency,
            },
            "checkout_ui_mode": self.config.checkout_ui_mode,
        }
        response = session.post(
            CHECKOUT_URL,
            json=body,
            headers={
                "Referer": "https://chatgpt.com/",
                "x-openai-target-path": "/backend-api/payments/checkout",
                "x-openai-target-route": "/backend-api/payments/checkout",
            },
            timeout=self.config.timeout,
        )
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            try:
                error_payload = response.json() or {}
            except Exception:
                error_payload = {}
            if str(error_payload.get("detail") or "") == "User is already paid":
                raise UpstreamError(400, "the account is already paid and cannot create a Plus checkout")
            raise response_error(response, "checkout create failed")
        try:
            payload = response.json() or {}
        except Exception as exc:
            raise UpstreamError(502, "checkout create returned invalid JSON", retryable=True) from exc
        cs_id = payload.get("checkout_session_id") or payload.get("session_id") or payload.get("id")
        cs_id = str(cs_id or "").strip()
        if not cs_id.startswith(("oaics_", "cs_")):
            raise UpstreamError(502, "checkout response did not contain a supported session id")
        return {
            "cs_id": cs_id,
            "processor_entity": extract_processor_entity(payload),
            "billing_country": self.config.billing_country,
            "currency": self.config.currency,
            "checkout_amount_minor": checkout_amount_minor(payload),
        }

    def _create_checkout_with_retry(self, checkout_proxy: str) -> tuple[Any, dict[str, Any]]:
        session = self._new_identity_session(checkout_proxy)
        current_proxy = checkout_proxy
        cloudflare_failures = 0
        exit_rotated = False
        last_error: Exception | None = None
        for attempt in range(1, self.config.checkout_attempts + 1):
            self.log(f"Checkout attempt {attempt}/{self.config.checkout_attempts}.")
            try:
                return session, self._create_checkout(session)
            except UpstreamError as exc:
                last_error = exc
                if not exc.retryable or attempt >= self.config.checkout_attempts:
                    safe_close(session)
                    raise
                if exc.cloudflare:
                    cloudflare_failures += 1
                    if cloudflare_failures >= self.config.cf_same_identity_attempts and not exit_rotated:
                        rotated = rotate_proxy_session(current_proxy, self.config.checkout_proxy_country)
                        if rotated != current_proxy:
                            current_proxy = rotated
                            set_proxy(session, current_proxy)
                            cloudflare_failures = 0
                            exit_rotated = True
                            self.log("Checkout CF retries exhausted; rotated the proxy exit once.")
                    if self.config.cf_retry_delay > 0:
                        self.sleep(self.config.cf_retry_delay)
            except Exception as exc:
                last_error = exc
                if not is_retryable_network_error(exc) or attempt >= self.config.checkout_attempts:
                    safe_close(session)
                    raise ExtractorError(f"checkout network error: {exc}") from exc
        safe_close(session)
        raise ExtractorError(f"checkout retries exhausted: {last_error}")

    def _update_promotion(
        self,
        checkout_session: Any,
        checkout: dict[str, Any],
        checkout_proxy: str,
        update_proxy: str,
    ) -> dict[str, Any]:
        processor_entity = processor_entity_for_country(
            checkout["billing_country"], checkout.get("processor_entity", "")
        )
        body = {
            "checkout_session_id": checkout["cs_id"],
            "processor_entity": processor_entity,
            "plan_name": self.config.plan_name,
            "price_interval": "month",
            "seat_quantity": 1,
            "promo_campaign": {
                "promo_campaign_id": self.config.promo_campaign_id,
                "is_coupon_from_query_param": False,
            },
        }
        headers = {
            "Referer": checkout_short_url(
                checkout["cs_id"], checkout["billing_country"], processor_entity
            ),
            "x-openai-target-path": UPDATE_PATH,
            "x-openai-target-route": UPDATE_PATH,
        }
        same_session = normalize_proxy_url(checkout_proxy) == normalize_proxy_url(update_proxy)
        session = checkout_session if same_session else self._clone_session(checkout_session, update_proxy)
        owns_session = not same_session
        current_proxy = update_proxy
        cloudflare_failures = 0
        exit_rotated = False
        try:
            for attempt in range(1, self.config.update_attempts + 1):
                self.log(f"Promotion update attempt {attempt}/{self.config.update_attempts}.")
                try:
                    refresh_cookie_header(session)
                    response = session.post(
                        f"https://chatgpt.com{UPDATE_PATH}",
                        json=body,
                        headers=headers,
                        timeout=self.config.timeout,
                    )
                except Exception as exc:
                    if not is_retryable_network_error(exc) or attempt >= self.config.update_attempts:
                        raise ExtractorError(f"promotion update network error: {exc}") from exc
                    current_proxy = rotate_proxy_session(
                        current_proxy, self.config.update_proxy_country
                    )
                    if owns_session:
                        safe_close(session)
                    session = self._clone_session(checkout_session, current_proxy)
                    owns_session = True
                    continue
                if int(getattr(response, "status_code", 0) or 0) >= 400:
                    error = response_error(response, "checkout/update failed")
                    if not error.retryable or attempt >= self.config.update_attempts:
                        raise error
                    if error.cloudflare:
                        cloudflare_failures += 1
                        if cloudflare_failures >= self.config.cf_same_identity_attempts and not exit_rotated:
                            rotated = rotate_proxy_session(
                                current_proxy, self.config.update_proxy_country
                            )
                            if rotated != current_proxy:
                                current_proxy = rotated
                                set_proxy(session, current_proxy)
                                cloudflare_failures = 0
                                exit_rotated = True
                                self.log("Update CF retries exhausted; rotated the proxy exit once.")
                        if self.config.cf_retry_delay > 0:
                            self.sleep(self.config.cf_retry_delay)
                    else:
                        current_proxy = rotate_proxy_session(
                            current_proxy, self.config.update_proxy_country
                        )
                        if owns_session:
                            safe_close(session)
                        session = self._clone_session(checkout_session, current_proxy)
                        owns_session = True
                    continue
                try:
                    payload = response.json() or {}
                except Exception as exc:
                    raise UpstreamError(502, "checkout/update returned invalid JSON") from exc
                if payload_has_invalid_promotion(payload):
                    raise InvalidPromotionError("checkout/update returned invalid_promotion")
                if payload.get("success") is False:
                    raise UpstreamError(502, "checkout/update explicitly rejected the promotion")
                sync_cookies(checkout_session, session)
                return payload
            raise ExtractorError("promotion update retries exhausted")
        finally:
            if owns_session:
                safe_close(session)

    def _page_amount(self, session: Any, checkout: dict[str, Any]) -> tuple[int | None, str]:
        refresh_cookie_header(session)
        response = session.get(
            checkout_short_url(
                checkout["cs_id"],
                checkout["billing_country"],
                checkout.get("processor_entity", ""),
            ),
            headers={"Referer": "https://chatgpt.com/"},
            timeout=self.config.timeout,
        )
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            raise response_error(response, "checkout page amount query failed")
        state = checkout_state_from_html(str(getattr(response, "text", "") or ""))
        return checkout_amount_minor(state), checkout_currency(state) or self.config.currency

    def _verify_amount(
        self,
        checkout_session: Any,
        checkout: dict[str, Any],
        update_payload: dict[str, Any],
    ) -> tuple[str, int | None, str]:
        amount = checkout_amount_minor(update_payload)
        currency = checkout_currency(update_payload) or self.config.currency
        if amount is None:
            try:
                amount, currency = self._page_amount(checkout_session, checkout)
            except UpstreamError as exc:
                self.log(f"Amount page probe was unavailable ({exc.status_code}); returning pending.")
                amount = None
        if amount is None:
            return "pending", None, currency
        if amount != 0:
            raise NonZeroAmountError(
                f"promotion did not produce a zero checkout: amount_minor={amount} {currency}"
            )
        return "verified_zero", 0, currency

    def extract(self) -> CheckoutResult:
        last_promotion_error: InvalidPromotionError | None = None
        for full_attempt in range(1, self.config.full_attempts + 1):
            checkout_session = None
            checkout_proxy, update_proxy = self._stage_proxies()
            try:
                self.log(f"Full checkout flow {full_attempt}/{self.config.full_attempts}.")
                self._preflight(checkout_proxy, update_proxy)
                checkout_session, checkout = self._create_checkout_with_retry(checkout_proxy)
                self.log(f"Checkout created: {checkout['cs_id'][:12]}...")
                update_payload = self._update_promotion(
                    checkout_session, checkout, checkout_proxy, update_proxy
                )
                verification, amount, amount_currency = self._verify_amount(
                    checkout_session, checkout, update_payload
                )
                return CheckoutResult(
                    ok=True,
                    long_url=checkout_short_url(
                        checkout["cs_id"],
                        checkout["billing_country"],
                        checkout.get("processor_entity", ""),
                    ),
                    cs_id=checkout["cs_id"],
                    processor_entity=processor_entity_for_country(
                        checkout["billing_country"], checkout.get("processor_entity", "")
                    ),
                    billing_country=self.config.billing_country,
                    currency=self.config.currency,
                    payment_locale=self.config.payment_locale,
                    amount_verification=verification,
                    amount_minor=amount,
                    amount_currency=amount_currency,
                )
            except InvalidPromotionError as exc:
                last_promotion_error = exc
                if full_attempt >= self.config.full_attempts:
                    raise
                self.log("Promotion was invalid; discarding this checkout and rebuilding the full flow.")
            finally:
                safe_close(checkout_session)
        raise last_promotion_error or ExtractorError("checkout flow did not return a result")


def read_credential_input(args: argparse.Namespace) -> str:
    if args.credential:
        return str(args.credential)
    if args.credential_file:
        if args.credential_file == "-":
            if sys.stdin.isatty():
                return getpass.getpass("Access token or session JSON: ")
            return sys.stdin.read()
        try:
            return Path(args.credential_file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise ExtractorError(f"cannot read credential file: {exc}") from exc
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return getpass.getpass("Access token or session JSON: ")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a PH/PHP ChatGPT checkout short link through US checkout and TR update proxies."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--credential-file", default="", help="token/session JSON file, or '-' for stdin")
    parser.add_argument(
        "--credential",
        default="",
        help="raw token/session JSON (less secure than --credential-file)",
    )
    parser.add_argument("--session-cookie", default="", help="optional ChatGPT payment cookie header")
    parser.add_argument(
        "--checkout-proxy",
        default=os.getenv("DIRECT_CARD_CHECKOUT_PROXY", ""),
        help="kookeey-format checkout proxy URL (env: DIRECT_CARD_CHECKOUT_PROXY)",
    )
    parser.add_argument(
        "--update-proxy",
        default=os.getenv("DIRECT_CARD_UPDATE_PROXY", ""),
        help="kookeey-format update proxy URL (env: DIRECT_CARD_UPDATE_PROXY)",
    )
    parser.add_argument("--billing-country", default="PH")
    parser.add_argument("--currency", default="PHP")
    parser.add_argument("--checkout-proxy-country", default="US")
    parser.add_argument("--update-proxy-country", default="TR")
    parser.add_argument("--payment-locale", default="en")
    parser.add_argument("--plan-name", default="chatgptplusplan")
    parser.add_argument("--promo-campaign-id", default="plus-1-month-free")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--checkout-attempts", type=int, default=10)
    parser.add_argument("--update-attempts", type=int, default=15)
    parser.add_argument("--full-attempts", type=int, default=3)
    parser.add_argument("--cf-same-identity-attempts", type=int, default=5)
    parser.add_argument("--cf-retry-delay", type=float, default=3.0)
    parser.add_argument("--skip-proxy-check", action="store_true")
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    return parser


def config_from_args(args: argparse.Namespace) -> ExtractorConfig:
    return ExtractorConfig(
        billing_country=str(args.billing_country).upper(),
        currency=str(args.currency).upper(),
        payment_locale=str(args.payment_locale),
        checkout_proxy_country=str(args.checkout_proxy_country).upper(),
        update_proxy_country=str(args.update_proxy_country).upper(),
        checkout_proxy=str(args.checkout_proxy),
        update_proxy=str(args.update_proxy),
        plan_name=str(args.plan_name),
        promo_campaign_id=str(args.promo_campaign_id),
        timeout=float(args.timeout),
        checkout_attempts=int(args.checkout_attempts),
        update_attempts=int(args.update_attempts),
        full_attempts=int(args.full_attempts),
        cf_same_identity_attempts=int(args.cf_same_identity_attempts),
        cf_retry_delay=max(0.0, float(args.cf_retry_delay)),
        verify_proxy_country=not bool(args.skip_proxy_check),
    )


def print_json(payload: dict[str, Any], pretty: bool) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        credentials = parse_credentials(read_credential_input(args), args.session_cookie)
        result = CheckoutExtractor(credentials, config_from_args(args)).extract()
        print_json(asdict(result), args.pretty)
        return 0
    except (ExtractorError, UpstreamError) as exc:
        print_json(
            {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
            args.pretty,
        )
        return 1
    except KeyboardInterrupt:
        print_json({"ok": False, "error_type": "Cancelled", "error": "cancelled"}, args.pretty)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
