"""Shared browser-like headers for OpenAI auth protocol calls."""

from __future__ import annotations

import random
from urllib.parse import urlparse


DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36"
DEFAULT_SEC_CH_UA = '"Google Chrome";v="110", "Chromium";v="110", "Not_A Brand";v="24"'


def datadog_trace_headers() -> dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    trace_hex = format(int(trace_id), "016x")
    parent_hex = format(int(parent_id), "016x")
    return {
        "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def origin_from_referer(referer: str = "") -> str:
    try:
        parsed = urlparse(str(referer or ""))
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return ""


def _extra_header_value(extra: dict | None, name: str) -> str:
    if not isinstance(extra, dict):
        return ""
    target = name.lower()
    for key, value in extra.items():
        if str(key).lower() == target:
            return str(value or "").strip()
    return ""


def openai_auth_headers(
    did: str = "",
    *,
    referer: str = "",
    origin: str = "",
    accept: str = "application/json",
    sentinel: dict | None = None,
    sentinel_token: str = "",
    sentinel_so_token: str = "",
    extra: dict | None = None,
    include_trace: bool = True,
) -> dict[str, str]:
    referer = str(referer or "").strip() or _extra_header_value(extra, "referer")
    origin = str(origin or "").strip() or _extra_header_value(extra, "origin")
    headers = {
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": DEFAULT_USER_AGENT,
        "sec-ch-ua": DEFAULT_SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    if referer:
        headers["Referer"] = str(referer)
    resolved_origin = str(origin or "").strip() or origin_from_referer(referer)
    if resolved_origin:
        headers["Origin"] = resolved_origin
    did = str(did or "").strip()
    if did:
        headers["oai-device-id"] = did
    if include_trace:
        headers.update(datadog_trace_headers())
    if sentinel or sentinel_token or sentinel_so_token:
        try:
            from .codex_sentinel import attach_sentinel

            sentinel_data = dict(sentinel or {})
            if sentinel_token:
                sentinel_data["sentinel_token"] = sentinel_token
            if sentinel_so_token:
                sentinel_data["sentinel_so_token"] = sentinel_so_token
            attach_sentinel(headers, sentinel_data)
        except Exception:
            pass
    if extra:
        headers.update({str(k): str(v) for k, v in extra.items() if v is not None})
    return headers


def openai_auth_headers_lower(did: str = "", extra: dict | None = None, **kwargs) -> dict[str, str]:
    headers = openai_auth_headers(did, extra=extra, **kwargs)
    return {str(k).lower(): v for k, v in headers.items()}
