from __future__ import annotations

try:  # pragma: no cover - direct script execution
    from ..payment_wire import _new_session
except ImportError:
    from payment_wire import _new_session  # type: ignore
try:  # pragma: no cover - direct script execution
    from ..pp_link_helpers import DEFAULT_TIMEOUT
except ImportError:
    from pp_link_helpers import DEFAULT_TIMEOUT  # type: ignore
from typing import Any
from collections.abc import Mapping
from urllib.parse import urljoin
from ._extract import (
    _upi_extract_qr_from_html,
    _upi_is_instructions_url,
)
from .env import _emit, _env_bool
from .dump import _upi_dump_http
from .session import _upi_apply_fingerprint


def _upi_hydrate_qr_data(
    qr_data: dict,
    proxy_url: str,
    fingerprint: Mapping[str, str] | None = None,
) -> dict:
    """如果 JSON 中没有 upi://，访问 hosted_instructions_url 从 HTML 中解析.

    旧实现的守卫是 ``if hosted_url and not result.get("upi_uri")`` —— 只要 JSON
    任何角落出现过 ``upi://`` 字符串（包括**描述文案**里的示意串）就不再 hydrate,
    导致真正的深链取不到。这里改为: 只要还没有以 ``upi://`` 开头的 ``upi_uri``
    **且** 有 ``hosted_instructions_url`` 就去抓页面；抓到后按「深链优先」合并。
    """
    result = dict(qr_data)
    hosted_url = result.get("hosted_instructions_url")
    if not hosted_url:
        return result
    if str(result.get("upi_uri") or "").startswith("upi://"):
        return result
    try:
        session = _new_session(proxy_url)
        _upi_apply_fingerprint(session, fingerprint or {})
        resp = session.get(
            hosted_url,
            timeout=DEFAULT_TIMEOUT,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://js.stripe.com/",
            },
        )
        if resp.status_code < 400:
            _upi_dump_http(resp, "hydrate_html", None, "GET", str(hosted_url), force=resp.status_code >= 400)
            extracted = _upi_extract_qr_from_html(resp.text)
            for k, v in extracted.items():
                if v and (k == "upi_uri" or not result.get(k)):
                    result[k] = v
        else:
            _upi_dump_http(resp, "hydrate_html", None, "GET", str(hosted_url), force=True)
    except Exception as exc:
        _emit("hydrate", f"hydrate failed (non-fatal): {type(exc).__name__}: {exc}")
    return result


def _upi_resolve_external_redirect(session: Any, start_url: str, max_hops: int = 5) -> str:
    """参考实现 ``resolve_external_redirect``: 跟随跳转直到 instructions 页。

    旧实现拿到的 ``redirect_url`` 直接返回, 不会再跟一跳 ⇒ 交付给用户的是
    ``hooks.stripe.com`` 之类的中间跳转, 而不是真能扫的指令页。
    """
    if not start_url or not _env_bool("UPI_FOLLOW_REDIRECT", True):
        return start_url
    current = start_url
    for _hop in range(1, max_hops + 1):
        if _upi_is_instructions_url(current):
            return current
        try:
            resp = session.get(current, timeout=DEFAULT_TIMEOUT, allow_redirects=False)
        except Exception as exc:
            _emit("redirect", f"follow redirect failed (non-fatal): {type(exc).__name__}: {exc}")
            return current
        location = str(resp.headers.get("location") or resp.headers.get("Location") or "")
        if not location:
            return current
        current = urljoin(current, location)
    return current
