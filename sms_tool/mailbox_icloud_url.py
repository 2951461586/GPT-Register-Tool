"""Read iCloud forwarding mailboxes exposed through per-account OTP URLs."""

from __future__ import annotations

import hashlib
import base64
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, unquote_to_bytes, urlencode, urljoin, urlsplit, urlunsplit

from curl_cffi import requests as curl_requests

from .mail_otp import _extract_otp_from_text, _message_received_ts


PROVIDER = "icloud_url"
_VOID_HTML_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}
_OTP_CONTEXT_RE = re.compile(
    r"openai|chatgpt|login\s+code|verification\s+code|temporary\s+code|"
    r"临时.{0,12}代码|登录.{0,12}代码|验证码",
    re.IGNORECASE,
)


def is_icloud_url_line(value: Any) -> bool:
    email, url = split_icloud_url_line(value)
    if not email or not url or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].lower()
    return domain in {"icloud.com", "me.com", "mac.com"} and _valid_mailbox_url(url)


def split_icloud_url_line(value: Any) -> tuple[str, str]:
    text = str(value or "").strip().lstrip("\ufeff")
    for delimiter in ("----", "---"):
        if delimiter not in text:
            continue
        email, url = (part.strip() for part in text.split(delimiter, 1))
        if url.lower().startswith(("http://", "https://")):
            return email.lower(), url
    return "", ""


def fetch_icloud_url_messages(mailbox, limit: int = 25, proxy: str | None = None) -> list[dict[str, Any]]:
    page_url, email, text = _fetch_icloud_url_page(mailbox, limit=limit, proxy=proxy)
    api_paths = _yangyang_api_paths(text)
    if api_paths:
        messages = _fetch_yangyang_messages(
            page_url,
            api_paths,
            email=email,
            limit=limit,
            proxy=proxy,
        )
    else:
        messages = _parse_card_messages(text, email=email, limit=limit)
    return sorted(messages, key=_message_received_ts, reverse=True)


def snapshot_icloud_url_messages(mailbox, limit: int = 25, proxy: str | None = None) -> list[dict[str, Any]]:
    page_url, email, text = _fetch_icloud_url_page(mailbox, limit=limit, proxy=proxy)
    api_paths = _yangyang_api_paths(text)
    if not api_paths:
        messages = _parse_card_messages(text, email=email, limit=limit)
        return sorted(messages, key=_message_received_ts, reverse=True)

    items = _fetch_yangyang_items(page_url, api_paths, limit=limit, proxy=proxy)
    return [
        _message(
            email=email,
            message_id=str(item.get("id") or ""),
            subject=str(item.get("subject") or ""),
            sender=str(item.get("from_address") or item.get("fromAddress") or ""),
            received_at=str(item.get("received_at") or item.get("receivedAt") or ""),
            body="",
        )
        for item in items
        if item.get("id") not in (None, "")
    ]


def _fetch_icloud_url_page(mailbox, *, limit: int, proxy: str | None) -> tuple[str, str, str]:
    url = str(getattr(mailbox, "token", "") or "").strip()
    email = str(getattr(mailbox, "email", "") or "").strip().lower()
    if not email or not _valid_mailbox_url(url):
        raise RuntimeError("invalid iCloud OTP URL mailbox")

    page_url = _with_message_limit(url, limit)
    page = _request(page_url, proxy=proxy)
    if page.status_code < 200 or page.status_code >= 300:
        raise RuntimeError(f"iCloud OTP URL fetch failed: HTTP {page.status_code}")
    return page_url, email, str(page.text or "")


def _request(url: str, *, proxy: str | None = None):
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        return curl_requests.get(
            url,
            proxies=proxies,
            impersonate="chrome124",
            timeout=35,
        )
    except Exception as exc:
        # The URL contains mailbox credentials, so never include it in diagnostics.
        raise RuntimeError(f"iCloud OTP URL request failed: {type(exc).__name__}") from None


def _valid_mailbox_url(value: Any) -> bool:
    parsed = urlsplit(str(value or "").strip())
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)


def _with_message_limit(url: str, limit: int) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["n"] = str(max(1, min(int(limit or 25), 50)))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _yangyang_api_paths(text: str) -> tuple[str, str, str] | None:
    detail_base = re.search(r"var\s+detailBase\s*=\s*['\"]([^'\"]+)['\"]", text)
    detail_suffix = re.search(r"var\s+detailSuffix\s*=\s*['\"]([^'\"]+)['\"]", text)
    page_base = re.search(r"var\s+pageBase\s*=\s*['\"]([^'\"]+)['\"]", text)
    if not (detail_base and detail_suffix and page_base):
        return None
    return detail_base.group(1), detail_suffix.group(1), page_base.group(1)


def _fetch_yangyang_messages(
    page_url: str,
    paths: tuple[str, str, str],
    *,
    email: str,
    limit: int,
    proxy: str | None,
) -> list[dict[str, Any]]:
    detail_base, detail_suffix, _page_base = paths
    items = _fetch_yangyang_items(page_url, paths, limit=limit, proxy=proxy)
    messages: list[dict[str, Any]] = []
    for item in items:
        detail_url = urljoin(page_url, f"{detail_base}{item['id']}{detail_suffix}")
        detail_response = _request(detail_url, proxy=proxy)
        if detail_response.status_code < 200 or detail_response.status_code >= 300:
            continue
        try:
            detail = detail_response.json()
        except Exception:
            continue
        if not isinstance(detail, dict):
            continue
        subject = str(detail.get("subject") or item.get("subject") or "").strip()
        body = _decode_mail_body(detail.get("body"))
        messages.append(_message(
            email=email,
            message_id=str(item.get("id") or ""),
            subject=subject,
            sender=str(detail.get("fromAddress") or item.get("from_address") or ""),
            received_at=str(detail.get("receivedAt") or item.get("received_at") or ""),
            body=body,
        ))
    return messages


def _fetch_yangyang_items(
    page_url: str,
    paths: tuple[str, str, str],
    *,
    limit: int,
    proxy: str | None,
) -> list[dict[str, Any]]:
    _detail_base, _detail_suffix, page_base = paths
    listing = _request(urljoin(page_url, page_base), proxy=proxy)
    if listing.status_code < 200 or listing.status_code >= 300:
        raise RuntimeError(f"iCloud OTP URL list failed: HTTP {listing.status_code}")
    try:
        payload = listing.json()
    except Exception:
        raise RuntimeError("iCloud OTP URL list returned invalid JSON") from None
    items = payload.get("items") if isinstance(payload, dict) else []
    items = sorted(
        (item for item in list(items or []) if isinstance(item, dict)),
        key=lambda item: _message_received_ts({
            "receivedDateTime": _iso_datetime(item.get("received_at") or item.get("receivedAt")),
        }),
        reverse=True,
    )
    return [item for item in items[: max(1, min(int(limit or 25), 50))] if item.get("id") not in (None, "")]


class _CardMessageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.card_depth: int | None = None
        self.card_tag = ""
        self.field_depth: int | None = None
        self.field_tag = ""
        self.field = ""
        self.ignored_tags: list[str] = []
        self.current: dict[str, list[str]] | None = None
        self.cards: list[dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        # Some providers inject a raw ``<sender@example.com>`` address into the
        # sender field. HTMLParser treats it as an opening tag, which otherwise
        # corrupts our depth tracking and leaves the whole card unclosed.
        if self.current is not None and self.field and "@" in tag:
            self.current[self.field].append(f"<{tag}>")
            return
        attributes = dict(attrs or [])
        classes = set(str(attributes.get("class") or "").split())
        if tag in {"script", "style"}:
            self.ignored_tags.append(tag)
        is_card = (tag == "div" and "card" in classes) or (tag == "article" and "mail-card" in classes)
        if is_card and self.current is None:
            self.card_depth = self.depth
            self.card_tag = tag
            self.current = {"sender": [], "subject": [], "received_at": [], "body": []}
        elif self.current is not None and not self.field:
            field = next((name for css, name in (
                ("fr", "sender"),
                ("su", "subject"),
                ("dt", "received_at"),
                ("bd", "body"),
                ("meta", "sender"),
                ("subject", "subject"),
                ("date", "received_at"),
                ("body", "body"),
            ) if css in classes), "")
            if field:
                self.field = field
                self.field_depth = self.depth
                self.field_tag = tag
        if tag not in _VOID_HTML_TAGS:
            self.depth += 1

    def handle_endtag(self, tag):
        if self.current is not None and self.field and "@" in tag:
            return
        if tag in {"script", "style"} and self.ignored_tags:
            for index in range(len(self.ignored_tags) - 1, -1, -1):
                if self.ignored_tags[index] == tag:
                    del self.ignored_tags[index]
                    break
        if tag not in _VOID_HTML_TAGS:
            self.depth = max(0, self.depth - 1)
        if self.field and self.field_tag == tag and self.field_depth == self.depth:
            self.field = ""
            self.field_depth = None
            self.field_tag = ""
        if self.current is not None and self.card_tag == tag and self.card_depth == self.depth:
            self.cards.append({key: _clean_text(" ".join(value)) for key, value in self.current.items()})
            self.current = None
            self.card_depth = None
            self.card_tag = ""

    def handle_data(self, data):
        if self.current is None or not self.field or self.ignored_tags:
            return
        if str(data or "").strip():
            self.current[self.field].append(str(data))


def _parse_card_messages(text: str, *, email: str, limit: int) -> list[dict[str, Any]]:
    parser = _CardMessageParser()
    parser.feed(text)
    messages = []
    cards = sorted(
        parser.cards,
        key=lambda card: _message_received_ts({"receivedDateTime": _iso_datetime(card.get("received_at"))}),
        reverse=True,
    )
    for card in cards[: max(1, min(int(limit or 25), 50))]:
        body = card.get("body", "")
        subject = card.get("subject", "")
        digest = hashlib.sha256(
            f"{subject}\n{card.get('received_at', '')}\n{body}".encode("utf-8", errors="ignore")
        ).hexdigest()[:24]
        messages.append(_message(
            email=email,
            message_id=digest,
            subject=subject,
            sender=card.get("sender", ""),
            received_at=card.get("received_at", ""),
            body=body,
        ))
    return messages


def _message(*, email: str, message_id: str, subject: str, sender: str, received_at: str, body: str) -> dict[str, Any]:
    normalized_subject = _normalize_otp_subject(subject, body)
    return {
        "id": message_id,
        "subject": normalized_subject,
        "from": sender,
        "receivedDateTime": _iso_datetime(received_at),
        "bodyPreview": _clean_text(body)[:1000],
        "body": {"content": body},
        "toRecipients": [{"emailAddress": {"address": email}}],
    }


def _normalize_otp_subject(subject: str, body: str) -> str:
    combined = f"{subject}\n{body}"
    if _extract_otp_from_text(combined) and _OTP_CONTEXT_RE.search(combined):
        return f"{subject} login code".strip()
    return subject


def _decode_mail_body(value: Any) -> str:
    text = str(value or "")
    if not text.lower().startswith("data:") or "," not in text:
        return text
    metadata, payload = text.split(",", 1)
    try:
        raw = base64.b64decode(payload, validate=False) if ";base64" in metadata.lower() else unquote_to_bytes(payload)
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _iso_datetime(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = parsedate_to_datetime(text)
    except Exception:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return ""
    return parsed.isoformat()


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
