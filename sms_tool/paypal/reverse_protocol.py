"""Pure HTML/form/token/CSRF/cookie protocol helpers for PayPal reverse auth.

Extracted from :mod:`sms_tool.paypal.reverse` so stateless HTML parsing,
CAPTCHA/block pattern matching, and result dataclasses live apart from the
stateful HTTP / browser-fallback client.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ..kernel.native_loader import load_native_module

_NATIVE_PAYPAL_REVERSE_HTML = load_native_module(
    "paypal-reverse-html-rs",
    "paypal_reverse_html_rs",
    "RUST_ANYTHING_UNIT_PAYPAL_REVERSE_HTML",
)
_NATIVE_EXTRACT_HIDDEN_FIELDS = getattr(_NATIVE_PAYPAL_REVERSE_HTML, "extract_hidden_fields", None)
_NATIVE_EXTRACT_INPUT_FIELDS = getattr(_NATIVE_PAYPAL_REVERSE_HTML, "extract_input_fields", None)
_NATIVE_EXTRACT_SELECT_FIELDS = getattr(_NATIVE_PAYPAL_REVERSE_HTML, "extract_select_fields", None)
_NATIVE_FIND_SIGNUP_FORM = getattr(_NATIVE_PAYPAL_REVERSE_HTML, "find_signup_form", None)
_NATIVE_FIND_SUBMIT_FORM = getattr(_NATIVE_PAYPAL_REVERSE_HTML, "find_submit_form", None)
_NATIVE_EXTRACT_CHALLENGE_FORM = getattr(_NATIVE_PAYPAL_REVERSE_HTML, "extract_challenge_form", None)


@dataclass
class ReversePayResult:
    ok: bool
    email: str = ""
    error: str = ""
    failed_step: str = ""
    access_token: str = ""
    oauth_refresh_token: str = ""
    refresh_token_status: str = ""
    paypal_status: str = ""
    redirect_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"ok": self.ok, "email": self.email}
        if self.ok:
            d.update(
                {
                    "access_token": self.access_token,
                    "oauth_refresh_token": self.oauth_refresh_token,
                    "refresh_token_status": self.refresh_token_status,
                    "paypal_status": self.paypal_status,
                    "redirect_url": self.redirect_url,
                }
            )
        else:
            d.update({"error": self.error, "failed_step": self.failed_step})
        return d


class _NeedBrowserFallback(Exception):
    """Raised when the reverse-engineered flow cannot continue."""

    def __init__(self, step: str, detail: str):
        self.step = step
        self.detail = detail
        super().__init__(f"[{step}] {detail}")


# Patterns indicating JS-only rendering or CAPTCHA
_CAPTCHA_PATTERNS = [
    re.compile(r"data-app=[\"']?authchallenge_response", re.I),
    re.compile(r"id=[\"']?captcha-standalone", re.I),
    re.compile(r"data-enable-ads-captcha=[\"']?true", re.I),
    re.compile(r"adsddcaptcha", re.I),
    re.compile(r"ngrlCaptcha", re.I),
    re.compile(r"g-recaptcha", re.I),
    re.compile(r"recaptcha", re.I),
    re.compile(r"are you a human", re.I),
    re.compile(r"verify you are human", re.I),
]

_BLOCK_PATTERNS = [
    re.compile(r"unusual activity", re.I),
    re.compile(r"temporarily locked", re.I),
    re.compile(r"try again later", re.I),
    re.compile(r"access denied", re.I),
    re.compile(r"unable to process", re.I),
]


def build_reverse_session_headers(chrome_version: str, chrome_full_version: str, user_agent: str) -> dict[str, str]:
    """Return initial Chrome-like navigation headers for PayPal reverse sessions."""
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
        "Priority": "u=0, i",
        "sec-ch-ua": f'"Google Chrome";v="{chrome_version}", "Chromium";v="{chrome_version}", "Not(A:Brand";v="24"',
        "sec-ch-ua-full-version-list": f'"Google Chrome";v="{chrome_full_version}", "Chromium";v="{chrome_full_version}", "Not(A:Brand";v="24.0.0.0"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }


def extract_token(data: object, *keys: str) -> str:
    """Extract the first non-empty string token from a JSON dict or nested session dict."""
    if not isinstance(data, dict):
        return ""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    session = data.get("session")
    if isinstance(session, dict):
        for key in keys:
            value = session.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def get_attr(tag: str, attr: str) -> str:
    """Get an attribute value from an HTML tag."""
    m = re.search(rf'{attr}=["\']([^"\']*)["\']', tag, re.I)
    return m.group(1) if m else ""


def extract_form_action(form_html: str, current_url: str) -> str:
    """Extract the action URL from a form tag, resolved against ``current_url``."""
    m = re.search(r'<form[^>]*action=["\']([^"\']*)["\']', form_html, re.I)
    if m:
        action = m.group(1)
        if action.startswith("/"):
            parsed = urlparse(current_url)
            return f"{parsed.scheme}://{parsed.netloc}{action}"
        if action.startswith("http"):
            return action
    return current_url


def extract_hidden_fields(form_html: str) -> dict[str, str]:
    """Extract hidden input fields from a form."""
    if _NATIVE_EXTRACT_HIDDEN_FIELDS is not None and isinstance(form_html, str):
        return _NATIVE_EXTRACT_HIDDEN_FIELDS(form_html)
    fields: dict[str, str] = {}
    pattern = re.compile(r'<input[^>]*type=["\']hidden["\'][^>]*>', re.IGNORECASE)
    for match in pattern.finditer(form_html):
        tag = match.group(0)
        name = get_attr(tag, "name")
        value = get_attr(tag, "value")
        if name:
            fields[name] = value
    return fields


def extract_input_fields(form_html: str) -> dict[str, str]:
    """Extract all non-button input fields from a form."""
    if _NATIVE_EXTRACT_INPUT_FIELDS is not None and isinstance(form_html, str):
        return _NATIVE_EXTRACT_INPUT_FIELDS(form_html)
    fields: dict[str, str] = {}
    pattern = re.compile(r"<input[^>]*>", re.IGNORECASE)
    for match in pattern.finditer(form_html):
        tag = match.group(0)
        name = get_attr(tag, "name")
        value = get_attr(tag, "value")
        input_type = get_attr(tag, "type").lower()
        if name and input_type not in ("submit", "button", "image"):
            fields[name] = value
    return fields


def extract_select_fields(form_html: str) -> dict[str, str]:
    """Extract select field names with their selected/default option values."""
    if _NATIVE_EXTRACT_SELECT_FIELDS is not None and isinstance(form_html, str):
        return _NATIVE_EXTRACT_SELECT_FIELDS(form_html)
    fields: dict[str, str] = {}
    pattern = re.compile(r"<select[^>]*>.*?</select>", re.IGNORECASE | re.DOTALL)
    for match in pattern.finditer(form_html):
        tag = match.group(0)
        name = get_attr(tag, "name")
        if not name:
            continue
        selected = re.search(r'<option[^>]*selected[^>]*value=["\']([^"\']*)["\']', tag, re.I)
        if selected:
            fields[name] = selected.group(1)
            continue
        first = re.search(r'<option[^>]*value=["\']([^"\']*)["\']', tag, re.I)
        fields[name] = first.group(1) if first else ""
    return fields


def find_form_by_fields(html: str, current_url: str, field_names: list[str]) -> tuple[str, dict[str, str]] | None:
    """Find a form containing any of the specified field names."""
    form_pattern = re.compile(r"<form[^>]*>(.*?)</form>", re.DOTALL | re.IGNORECASE)
    for match in form_pattern.finditer(html):
        form_html = match.group(0)
        form_lower = form_html.lower()
        for name in field_names:
            if name.lower() in form_lower:
                action = extract_form_action(form_html, current_url)
                fields = extract_hidden_fields(form_html)
                fields.update(extract_input_fields(form_html))
                fields.update(extract_select_fields(form_html))
                return action, fields
    return None


def find_signup_form(html: str, current_url: str) -> tuple[str, dict[str, str]] | None:
    """Find the main signup/login form on the page."""
    if _NATIVE_FIND_SIGNUP_FORM is not None and isinstance(html, str) and isinstance(current_url, str):
        return _NATIVE_FIND_SIGNUP_FORM(html, current_url)
    return find_form_by_fields(
        html,
        current_url,
        ["email", "login_email", "signup_email", "login_emailcopy"],
    )


def find_submit_form(html: str, current_url: str) -> tuple[str, dict[str, str]] | None:
    """Find a form with a submit button containing agree/pay text."""
    if _NATIVE_FIND_SUBMIT_FORM is not None and isinstance(html, str) and isinstance(current_url, str):
        return _NATIVE_FIND_SUBMIT_FORM(html, current_url)
    form_pattern = re.compile(r"<form[^>]*>(.*?)</form>", re.DOTALL | re.IGNORECASE)
    for match in form_pattern.finditer(html):
        form_html = match.group(0)
        if re.search(r"(?:agree|pay\s*now|continue|submit|confirm|authorize)", form_html, re.I):
            action = extract_form_action(form_html, current_url)
            fields = extract_hidden_fields(form_html)
            return action, fields
    return None


def is_js_only_page(html: str) -> bool:
    """Check if the page is JS-only (no parseable forms)."""
    if len(html) < 500:
        return True
    if re.search(r'<div id="(app|root|__next)"[^>]*>\s*</div>', html):
        return True
    return False


def extract_challenge_form(html: str, current_url: str) -> tuple[str, dict[str, str]] | None:
    """Extract PayPal ADS CAPTCHA challenge action URL and hidden inputs."""
    if _NATIVE_EXTRACT_CHALLENGE_FORM is not None and isinstance(html, str) and isinstance(current_url, str):
        return _NATIVE_EXTRACT_CHALLENGE_FORM(html, current_url)
    action_match = re.search(
        r'<form[^>]*name=["\']?challenge["\']?[^>]*action=["\']([^"\']+)["\']',
        html,
        re.I,
    )
    if not action_match:
        action_match = re.search(r'action=["\']([^"\']*(?:challenge|auth)[^"\']*)["\']', html, re.I)
    if not action_match:
        return None
    action = action_match.group(1)
    if action.startswith("/"):
        parsed = urlparse(current_url)
        action = f"{parsed.scheme}://{parsed.netloc}{action}"
    form_match = re.search(r'<form[^>]*name=["\']?challenge["\']?[^>]*>(.*?)</form>', html, re.DOTALL | re.I)
    fields: dict[str, str] = {}
    if form_match:
        fields = extract_hidden_fields(form_match.group(1))
    return action, fields


def cookie_dict(response: Any) -> dict[str, str]:
    """Return {name: value} from response cookies, compatible with requests and curl_cffi."""
    jar = response.cookies
    if hasattr(jar, "get_dict"):
        return jar.get_dict()
    return dict(jar)


def extract_csrf_token(response: Any) -> str:
    """Extract CSRF token from HTML meta/input tags or Set-Cookie headers."""
    html = response.text if hasattr(response, "text") else ""
    m = re.search(r'<meta[^>]*name=["\']csrf[_-]?token["\'][^>]*content=["\']([^"\']*)["\']', html, re.I)
    if m:
        return m.group(1)
    m = re.search(r'<input[^>]*name=["\']_token["\'][^>]*value=["\']([^"\']*)["\']', html, re.I)
    if m:
        return m.group(1)
    for name, value in cookie_dict(response).items():
        if "csrf" in name.lower() or "xsrftoken" in name.lower():
            return value
    return ""
