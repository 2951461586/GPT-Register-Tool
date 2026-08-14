"""PayPal redirect parsing and transport helpers used by payment adapters."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from typing import Any, Optional

try:
    from curl_cffi.requests import Session as _CffiSession
    _HAS_CFFI = True
except ImportError:
    _CffiSession = None
    _HAS_CFFI = False

import requests

from .paypal_fingerprints import PAYPAL_USER_AGENT as USER_AGENT

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

PP_ORIGIN = "https://www.paypal.com"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")

# ── Regex patterns ─────────────────────────────────────────────────────────────

_BA_RE = re.compile(r"BA-[A-Za-z0-9_.-]+")
_EC_RE = re.compile(r"(EC-[A-Z0-9]{17,})")


# ── Config helper ──────────────────────────────────────────────────────────────

def _load_config() -> dict[str, Any]:
    try:
        with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ── HTTP Session ───────────────────────────────────────────────────────────────

def _paypal_redirect_impersonate() -> str:
    try:
        cfg = _load_config()
        value = str((cfg.get("paypal") or {}).get("redirect_impersonate") or "").strip()
        return value or "chrome136"
    except Exception:
        return "chrome136"


def _make_session(proxy: Optional[str] = None) -> Any:
    if _HAS_CFFI:
        s = _CffiSession(impersonate=_paypal_redirect_impersonate())
        s.trust_env = False
        if proxy:
            p = proxy
            if p.startswith("socks5://"):
                p = "socks5h://" + p[len("socks5://"):]
            s.proxies = {"http": p, "https": p}
        else:
            s.proxies = {"http": "", "https": ""}
        return s
    s = requests.Session()
    s.trust_env = False
    s.headers["User-Agent"] = USER_AGENT
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def _session_cookies(s: Any) -> dict[str, str]:
    try:
        return dict(s.cookies.get_dict())
    except Exception:
        try:
            return {c.name: c.value for c in s.cookies}
        except Exception:
            return {}


# ── BA / EC Token extraction ───────────────────────────────────────────────────

def extract_ba_token(paypal_redirect_url: str) -> Optional[str]:
    """从 Stripe 返回的 PayPal redirect URL 中提取 BA token。"""
    m = _BA_RE.search(paypal_redirect_url or "")
    return m.group(0) if m else None


def extract_ec_token(text: str) -> Optional[str]:
    m = _EC_RE.search(text or "")
    return m.group(1) if m else None


def _mask_ba_token(value: str) -> str:
    def _mask(match: re.Match[str]) -> str:
        return "[REDACTED]"

    return _BA_RE.sub(_mask, str(value or ""))


def _extract_paypal_approve_url(text: str) -> str:
    body = (
        str(text or "")
        .replace("\\u0026", "&")
        .replace("\\/", "/")
        .replace("&amp;", "&")
    )
    match = re.search(r"https?://(?:www\.)?paypal\.com/agreements/approve\?[^\s<>\"']+", body)
    if match:
        return match.group(0)
    match = re.search(r"ba_token=(BA-[A-Za-z0-9_.-]+)", body)
    if match:
        return f"{PP_ORIGIN}/agreements/approve?ba_token={urllib.parse.quote(match.group(1), safe='')}"
    return ""


# ── Stripe redirect follower ───────────────────────────────────────────────────

def _follow_stripe_redirect(
    stripe_url: str,
    proxy: Optional[str] = None,
    timeout: int = 15,
    log=None,
) -> str:
    """Resolve Stripe pm-redirects to PayPal /agreements/approve when possible."""
    s = _make_session(proxy)
    current = (stripe_url or "").strip()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Referer": "https://checkout.stripe.com/",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }

    def _log(message: str):
        if log:
            log(message)
        else:
            logger.info(message)

    for step in range(1, 9):
        if extract_ba_token(current):
            _log(f"redirect step={step}: BA token already present in {_mask_ba_token(current[:140])}")
            return current
        if not current:
            break
        try:
            r = s.get(current, headers=headers, timeout=timeout, allow_redirects=False)
        except Exception as exc:
            _log(f"redirect step={step}: request failed {type(exc).__name__}: {exc}")
            break

        status = getattr(r, "status_code", "?")
        loc = (r.headers or {}).get("location") or (r.headers or {}).get("Location") or ""
        if loc:
            next_url = urllib.parse.urljoin(current, loc.replace("\\u0026", "&").replace("&amp;", "&"))
            _log(f"redirect step={step}: status={status} location={_mask_ba_token(next_url[:160])}")
            current = next_url
            continue

        try:
            body = (getattr(r, "text", "") or "")[:12000]
        except Exception:
            body = ""
        approve_url = _extract_paypal_approve_url(body)
        if approve_url:
            _log(f"redirect step={step}: status={status} body={_mask_ba_token(approve_url[:160])}")
            return approve_url

        final_url = str(getattr(r, "url", "") or current)
        _log(f"redirect step={step}: status={status} no Location/BA final={_mask_ba_token(final_url[:160])}")
        return final_url
    return current
