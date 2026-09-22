"""Read iCloud forwarding mailboxes exposed through per-account OTP URLs."""

from __future__ import annotations

import hashlib
import base64
import json
import re
import html
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, unquote_to_bytes, urlencode, urljoin, urlsplit, urlunsplit

from curl_cffi import requests as curl_requests

from ..mail_otp import _extract_otp_from_text, _message_received_ts
from ..mailbox_errors import MailboxEndpointUnavailableError
from ..mailbox_quarantine import (
    TRANSIENT_AUTH_INVALID_COOLDOWN_SECONDS,
    raise_if_mailbox_quarantined,
    record_mailbox_auth_invalid,
    record_mailbox_endpoint_unavailable,
)
from .mailbox_graph import MailboxAuthInvalidError


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

#: 「API 型」转发渠道返回的 JSON 里代表验证码的字段名。
#:
#: 同一份 ``icloud_url`` 池里混着两种渠道形态：``icloud-api.top`` 回 HTML 页面，
#: 而 ``ima3.52dfd.top`` 回 JSON。旧实现只认 HTML，于是后者被解析成「0 封邮件」，
#: 轮询跑满 300s 也读不到码（2026-09-16 batch 25116：18/18 ``matched=False``）。
#:
#: 键名沿用参考实现（abai ``core/api_mailbox.py:_extract_code``）的优先键集合。
#: ⚠️ ``code`` 在这个渠道的「无码」响应里取值 ``"no_code"`` —— 不是 6 位数字，
#: 所以 ``_six_digit_code`` 不会把它当成验证码（这是刻意的：宁可漏报不误报）。
_API_OTP_KEYS: tuple[str, ...] = (
    "verification_code", "verificationcode", "verify_code", "verifycode",
    "mail_code", "mailcode", "otp", "one_time_code", "onetimecode", "code",
)

#: 递归遍历 JSON 时跳过的键：它们的值经常是数字但不是验证码。
#: 除参考实现的集合外多加了 ``id`` —— 消息/请求 id 是 6 位数字的概率不低，
#: 且它**永远**不可能是验证码，所以这里刻意比参考实现更保守。
_API_IGNORED_KEYS: frozenset[str] = frozenset({
    "email", "mail", "url", "api_url", "password", "pass", "token",
    "status", "status_code", "timestamp", "created_at", "updated_at",
    "id",
})

#: 合成邮件的外观：下游 ``_normalize_otp_subject`` 会把带上下文的主题改写成
#: ``… login code``，从而命中注册泳道的 ``verification code|login code`` 关键词。
_API_OTP_SUBJECT = "Your temporary ChatGPT verification code"
_API_OTP_SENDER = "OpenAI <noreply@openai.com>"

# The OTP wait re-fetches the *same* forwarding URL every ``otp_poll_interval``
# seconds.  If that page -- or a CDN in front of it -- serves a cached body, every
# poll inside the window returns the same stale listing and a mail that lands
# mid-window stays invisible until the budget expires; the run then reports
# ``email_otp_poll_timeout`` while the code is already sitting in the inbox
# (2026-09-11 triage).  Asking for revalidation is the cheap, safe half of that
# fix: a cache-busting query parameter would defeat URL-keyed caching more
# thoroughly, but these are signed forwarding URLs and an extra parameter risks
# a 403, so it is deliberately NOT done here.
_NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def is_icloud_url_line(value: Any) -> bool:
    email, url = split_icloud_url_line(value)
    if not email or not url or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].lower()
    return domain in {"icloud.com", "me.com", "mac.com"} and _valid_mailbox_url(url)


def split_icloud_url_line(value: Any) -> tuple[str, str]:
    """Return ``(email, url)`` from a pool line, dropping every field after the URL.

    ``split(delimiter, 1)`` used to keep everything after the first delimiter, so a
    four-part supplier line (``email----url----account----2fa``) yielded a ``url`` with
    the trailing fields glued on.  ``_valid_mailbox_url`` only checks scheme + hostname,
    so that tail passed validation and travelled into the request URL through
    ``MailboxAccount.token``:

    * on a **path**-style channel (``icloud-api.top``) the server tolerates it;
    * on a **query**-style channel (``api798.com``) it lands inside ``auth_code`` and the
      server answers ``HTTP 403 错误：授权码无效``.

    That asymmetry is why the 2026-08-10 probe read as "api798 0/33, channel dead" when
    the channel was healthy and the *import format* was broken -- 33 usable mailboxes were
    deleted on that verdict.  Only the first two fields are ever meaningful, so drop the
    rest here, at the parse boundary, rather than trying to repair it downstream.
    """
    text = str(value or "").strip().lstrip("\ufeff")
    for delimiter in ("----", "---"):
        if delimiter not in text:
            continue
        parts = [part.strip() for part in text.split(delimiter)]
        if len(parts) < 2:
            continue
        email, url = parts[0], parts[1]
        if url.lower().startswith(("http://", "https://")):
            return email.lower(), url
    return "", ""


def fetch_icloud_url_messages(mailbox, limit: int = 25, proxy: str | None = None) -> list[dict[str, Any]]:
    page_url, email, text = _fetch_icloud_url_page(mailbox, limit=limit, proxy=proxy)
    # 「API 型」渠道回 JSON 而不是 HTML，必须在这里分流：下面两条 HTML 路径
    # 都会把它解析成「0 封邮件」，与「邮箱里确实没邮件」完全无法区分。
    api_messages = _api_payload_messages(text, email=email)
    if api_messages is not None:
        return api_messages
    # 「最新邮件」型渠道（api798.com）把正文藏在 JS 字符串里，卡片分支必然读成 0 封。
    latest_messages = _latest_mail_js_message(text, email=email)
    if latest_messages is not None:
        return latest_messages
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
    api_messages = _api_payload_messages(text, email=email)
    if api_messages is not None:
        return api_messages
    # 快照走的是另一个入口，必须同样分流，否则基线会把「有邮件」记成「0 封」。
    latest_messages = _latest_mail_js_message(text, email=email)
    if latest_messages is not None:
        return latest_messages
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
    raise_if_mailbox_quarantined(mailbox)
    url = str(getattr(mailbox, "token", "") or "").strip()
    email = str(getattr(mailbox, "email", "") or "").strip().lower()
    if not email or not _valid_mailbox_url(url):
        raise RuntimeError("invalid iCloud OTP URL mailbox")

    page_url = _with_message_limit(url, limit)
    page = _request(page_url, proxy=proxy)
    if page.status_code in {404, 410}:
        record_mailbox_endpoint_unavailable(mailbox)
        raise MailboxEndpointUnavailableError(page.status_code)
    if page.status_code == 401:
        # A 401 here is usually a rotated/expired forwarding token, not a dead
        # credential.  Quarantine it only briefly: a permanent entry froze OTP
        # recovery for the whole pool (see mailbox_quarantine.py).
        record_mailbox_auth_invalid(
            mailbox,
            reason=f"icloud_http_{page.status_code}",
            cooldown_seconds=TRANSIENT_AUTH_INVALID_COOLDOWN_SECONDS,
        )
        raise MailboxAuthInvalidError(detail=f"iCloud inbox HTTP {page.status_code}")
    if page.status_code < 200 or page.status_code >= 300:
        raise RuntimeError(f"iCloud OTP URL fetch failed: HTTP {page.status_code}")
    text = str(page.text or "")
    # The forwarding page can be HTTP 200 while the upstream iCloud account
    # has lost its app-specific password.  Treat that state as terminal;
    # otherwise the registration loop polls the same dead page until the full
    # OTP timeout and only prints repetitive dots.
    lowered = text.lower()
    if (
        "账号登录失败" in text
        or "账户登录失败" in text
        or "应用专用密码" in text
        or "app-specific password" in lowered
        or "account login failed" in lowered
        or "password is invalid" in lowered
        or "password has expired" in lowered
    ):
        record_mailbox_auth_invalid(
            mailbox,
            reason="icloud_app_specific_password_invalid_or_expired",
        )
        raise MailboxAuthInvalidError(
            getattr(mailbox, "email", ""),
            "iCloud app-specific password is invalid or expired",
        )
    return page_url, email, text


def _request(url: str, *, proxy: str | None = None):
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        return curl_requests.get(
            url,
            headers=dict(_NO_CACHE_HEADERS),
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


#: ``api798.com`` 的 ``/latest`` 页面形态 —— 与 ``icloud-api.top`` **完全不同**。
#:
#: 它的邮件正文**不在 DOM 里**：正文被塞进一段 JS 字符串（``var htmlContent = "…"``）
#: 再写进一个没有 ``src`` 的 ``<iframe>``。页面级可见文本因此**只有主题、没有验证码**，
#: 而 ``_parse_card_messages`` 期待的是 ``<div class="card">`` 布局 ⇒ 一封都读不出。
#: 「0 封」与「邮箱里确实没邮件」在返回值上完全一样，所以只会表现为轮询跑满超时。
#:
#: 2026-09-16 实测（批次抽中 ``jags-burly4k+oai02@icloud.com``，provider ``api798.com``）：
#: OpenAI 于 23:48:03 发码，页面显示**同一秒**的接收时间与越南语主题，
#: 验证码 ``494652`` 就嵌在那段 JS 字符串里 —— 而 ``fetch_icloud_url_messages``
#: 返回 **0 封**，轮询 303s 后 ``email_otp_poll_timeout``。
#: 同批 17 个 ``icloud-api.top`` 邮箱不受影响（它们的正文在 DOM 卡片里）。
_LATEST_MAIL_MARKERS = ("最新邮件信息", "接收时间：", "邮件主题：")
_JS_HTML_CONTENT_RE = re.compile(r'var\s+htmlContent\s*=\s*"((?:[^"\\]|\\.)*)"', re.S)
_LATEST_RECEIVED_RE = re.compile(
    r'class="label">\s*接收时间：\s*</div>\s*<div[^>]*>\s*(.*?)\s*</div>', re.S
)
_LATEST_SUBJECT_RE = re.compile(
    r'class="label">\s*邮件主题：\s*</div>\s*<div[^>]*>\s*(.*?)\s*</div>', re.S
)
_CN_TIME_RE = re.compile(
    r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2})\s*:\s*(\d{2})\s*:\s*(\d{2})"
)


def _label_value(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1) if match else ""


def _decode_js_string(raw: str) -> str:
    """把 JS 字符串字面量的**内容**解回文本（``\\"``、``\\r\\n``、``\\uXXXX``）。

    解出来的就是一封完整邮件（HTML），所以直接交给下游的验证码提取，
    而不是再走一遍卡片解析。
    """
    try:
        return json.loads(f'"{raw}"')
    except ValueError:
        return raw.replace("\\r\\n", "\n").replace('\\"', '"').replace("\\/", "/")


def _latest_mail_received_at(value: str) -> str:
    """``2026年09月16日 23:48:03 (北京时间)`` → ISO8601（``+08:00``）。

    页面已标注北京时间，所以显式带上 ``+08:00``，让下游 ``issued_after_unix``
    的比较拿到真实时刻，而不是落到「解析不出 ⇒ 不参与比较」那条宽容分支上。
    """
    match = _CN_TIME_RE.search(value or "")
    if not match:
        return ""
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    try:
        return datetime(
            year, month, day, hour, minute, second, tzinfo=timezone(timedelta(hours=8))
        ).isoformat()
    except ValueError:
        return ""


def _latest_mail_js_message(text: str, *, email: str) -> list[dict[str, Any]] | None:
    """解析 ``api798.com`` ``/latest`` 的「最新邮件」页；``None`` = 不是这种版式。

    返回列表即代表**确认是这种版式**（页面出现本身就说明有邮件），所以恒返回一条。
    无邮件时服务端回的是「未找到匹配的邮件」页面（不含本函数的标记）⇒ 返回 ``None``，
    由下面的卡片分支处理 —— 这样「我们读不出」与「确实没邮件」仍然分得开。
    """
    if not all(marker in text for marker in _LATEST_MAIL_MARKERS):
        return None
    received_at = _latest_mail_received_at(_label_value(_LATEST_RECEIVED_RE, text))
    subject = html.unescape(_clean_text(_label_value(_LATEST_SUBJECT_RE, text)))
    body_match = _JS_HTML_CONTENT_RE.search(text)
    body = _decode_js_string(body_match.group(1)) if body_match else ""
    return [
        _message(
            email=email,
            message_id=hashlib.sha256(
                f"api798-latest:{subject}\n{received_at}".encode("utf-8")
            ).hexdigest()[:24],
            subject=subject,
            sender="",
            received_at=received_at,
            body=body,
        )
    ]


def _api_payload_messages(text: str, *, email: str) -> list[dict[str, Any]] | None:
    """解析「API 型」转发渠道回的 JSON；``None`` = 这不是 JSON 响应。

    返回列表表示这确实是 JSON：**空列表即「此刻没有验证码」**，调用方继续轮询。
    这两种「空」必须分开 —— 合成不出码却回非空，会把「渠道没投递」误报成
    「我们读到了邮件但没码」。
    """
    payload = _load_json_payload(text)
    if payload is None:
        return None
    code = _api_payload_otp(payload)
    if not code:
        return []
    return [_message(
        email=email,
        message_id=hashlib.sha256(f"icloud-api-otp:{code}".encode("utf-8")).hexdigest()[:24],
        subject=_API_OTP_SUBJECT,
        sender=_API_OTP_SENDER,
        received_at=_now_iso(),
        body=f"{_API_OTP_SUBJECT}. Enter this code to continue: {code}",
    )]


def _load_json_payload(text: str) -> Any:
    """只有整体是 JSON 对象/数组才认；HTML 以 ``<`` 开头，天然进不来。"""
    stripped = str(text or "").strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except Exception:
        return None


def _api_payload_otp(payload: Any) -> str:
    """取 6 位验证码：先按字段名找，再回落到「非忽略键」文本的噪声过滤提取。

    兜底**不能**直接喂 ``json.dumps(payload)``：``{"code":"ok","id":"123456"}``
    会被文本提取器读成验证码 —— 忽略键在字段名那一层跳过了，在整段文本里却
    重新露出来。所以先按同一份忽略键集合把 payload 压成文本再提取。
    """
    preferred = _walk_api_code(payload)
    if preferred:
        return preferred
    return _extract_otp_from_text(_api_payload_text(payload))


def _api_payload_text(value: Any) -> str:
    """把 payload 压成「非忽略键」的文本，供噪声过滤提取器兜底。

    忽略键的过滤只有 **dict 分支**这一个 owner：能走到字符串分支，就说明它的
    父键已经过了那道过滤，再判一次是死代码。
    """
    chunks: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key or "").strip().lower().replace("-", "_") in _API_IGNORED_KEYS:
                continue
            chunks.append(_api_payload_text(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            chunks.append(_api_payload_text(child))
    elif isinstance(value, str):
        chunks.append(value)
    return "\n".join(chunk for chunk in chunks if chunk)


def _walk_api_code(value: Any) -> str:
    """递归找 6 位验证码；忽略键的过滤同样只有 dict 分支一个 owner。"""
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key or "").strip().lower().replace("-", "_") in _API_OTP_KEYS:
                code = _six_digit_code(child)
                if code:
                    return code
        for key, child in value.items():
            if str(key or "").strip().lower().replace("-", "_") in _API_IGNORED_KEYS:
                continue
            code = _walk_api_code(child)
            if code:
                return code
        return ""
    if isinstance(value, (list, tuple)):
        for child in value:
            code = _walk_api_code(child)
            if code:
                return code
        return ""
    if isinstance(value, str):
        return _six_digit_code(value)
    return ""


def _six_digit_code(value: Any) -> str:
    """整串就是 6 位数字才算 —— ``"no_code"`` 这种状态码必须落空。"""
    match = re.fullmatch(r"\d{6}", str(value if value is not None else "").strip())
    return match.group(0) if match else ""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


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
    if listing.status_code in {404, 410}:
        raise MailboxEndpointUnavailableError(listing.status_code)
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
