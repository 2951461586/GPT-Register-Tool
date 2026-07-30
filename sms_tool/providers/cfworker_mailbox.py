import json
import re
import time
import uuid
import urllib.error
import urllib.request
from email import policy
from email.header import decode_header, make_header
from email.parser import Parser
from html import unescape
from html.parser import HTMLParser
from urllib.parse import quote

from curl_cffi import requests as curl_requests


EMAIL_RE = re.compile(r"(?i)[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}")
OTP_RE = re.compile(r"(^|[^0-9])([0-9]{6})([^0-9]|$)")


class CFWorkerMailboxClient:
    def __init__(self, base_url, admin_token="", cf_api_token="", timeout=30, proxy=None):
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.admin_token = str(admin_token or "").strip()
        self.cf_api_token = str(cf_api_token or "").strip()
        self.timeout = timeout
        self.proxy = str(proxy or "").strip()
        if not self.base_url:
            raise RuntimeError("cfworker_url is required")

    def create_mailboxes(self, count=1, domain="liziai.cloud"):
        count = max(1, min(int(count or 1), 200))
        domain = str(domain or "liziai.cloud").strip().lstrip("@").lower()
        payload = {"domain": domain, "count": count, "quantity": count}
        candidates = [
            ("POST", "/api/mailboxes", payload),
            ("POST", "/api/emails", payload),
            ("POST", "/api/email/create", payload),
            ("POST", "/api/create", payload),
            ("POST", "/api/admin/mailboxes", payload),
            ("GET", f"/api/mailboxes?domain={quote(domain)}&count={count}", None),
            ("GET", f"/api/emails?domain={quote(domain)}&count={count}", None),
        ]
        for method, path, body in candidates:
            result = self._request(method, path, json_body=body, allow_404=True)
            if not result.get("ok"):
                continue
            emails = _extract_emails(result.get("data"), domain=domain)
            if emails:
                return emails[:count]
        return [f"oai-{uuid.uuid4().hex[:16]}@{domain}" for _ in range(count)]

    def fetch_messages(self, email, limit=25):
        email = str(email or "").strip().lower()
        if not email:
            return []
        encoded = quote(email, safe="")
        limit = max(1, min(int(limit or 25), 100))
        saw_empty_endpoint = False
        last_error = ""
        candidates = [
            f"/api/messages?email={encoded}&limit={limit}",
            f"/api/messages?address={encoded}&limit={limit}",
            f"/api/messages?to_address={encoded}&limit={limit}",
            f"/api/emails/{encoded}/messages?limit={limit}",
            f"/api/mailboxes/{encoded}/messages?limit={limit}",
            f"/api/mailbox/{encoded}?limit={limit}",
            f"/api/inbox/{encoded}?limit={limit}",
            f"/api/messages/{encoded}?limit={limit}",
            f"/messages/{encoded}?limit={limit}",
            f"/inbox/{encoded}?limit={limit}",
            f"/emails/{encoded}",
            f"/api/emails/{encoded}",
            f"/emails/{encoded}?limit={limit}",
            f"/api/emails/{encoded}?limit={limit}",
        ]
        for path in candidates:
            result = self._request("GET", path, allow_404=True)
            if not result.get("ok"):
                last_error = result.get("error", "")
                continue
            messages = _extract_messages(result.get("data"))
            if messages:
                filtered = _messages_for_mailbox(messages, email, allow_missing_recipient=False)
                if filtered:
                    return [_normalize_message(msg, email=email) for msg in filtered[:limit]]
            if _looks_empty_message_list(result.get("data")):
                saw_empty_endpoint = True
                continue
            single = _extract_single_message(result.get("data"))
            if single:
                filtered = _messages_for_mailbox([single], email, allow_missing_recipient=False)
                if filtered:
                    return [_normalize_message(filtered[0], email=email)]
        try:
            admin_messages = self._fetch_admin_messages(email, limit)
        except Exception as exc:
            admin_messages = None
            last_error = str(exc)
        if admin_messages:
            return [_normalize_message(msg, email=email) for msg in admin_messages[:limit]]
        if admin_messages == []:
            saw_empty_endpoint = True
        if saw_empty_endpoint:
            return []
        raise RuntimeError(last_error or "cfworker messages endpoint not found")

    def _fetch_admin_messages(self, email, limit):
        if not self.admin_token:
            return None
        scan_limit = max(100, min(500, limit * 4))
        all_result = self._request("GET", f"/admin/all?limit={scan_limit}", allow_404=True)
        all_endpoint_available = bool(all_result.get("ok"))
        if all_result.get("ok"):
            messages = _messages_for_mailbox(_extract_messages(all_result.get("data")), email, allow_missing_recipient=False)
            if messages:
                return [self._fetch_admin_message_detail(msg, email) for msg in messages[:limit]]
        if not all_result.get("ok") and all_result.get("error") != "not_found":
            raise RuntimeError(all_result.get("error") or "cfworker admin all failed")

        domain = email.rsplit("@", 1)[1] if "@" in email else ""
        domain_query = f"&domain={quote(domain, safe='')}" if domain else ""
        collected = []
        page = 1
        max_pages = 10
        total_pages = 1
        address_query = f"&address={quote(email, safe='')}&to_address={quote(email, safe='')}&email={quote(email, safe='')}"
        while page <= min(max_pages, total_pages):
            result = self._request("GET", f"/admin/emails?page={page}{domain_query}{address_query}", allow_404=True)
            if not result.get("ok"):
                if result.get("error") == "not_found":
                    return [] if all_endpoint_available else None
                raise RuntimeError(result.get("error") or "cfworker admin emails failed")
            data = result.get("data")
            messages = _extract_messages(data)
            collected.extend(_messages_for_mailbox(messages, email, allow_missing_recipient=False))
            page_data = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else {}
            try:
                page_size = max(1, int(page_data.get("pageSize") or len(messages) or 20))
                total = max(0, int(page_data.get("total") or len(messages) or 0))
                total_pages = max(1, (total + page_size - 1) // page_size)
            except Exception:
                total_pages = page
            if len(collected) >= limit or not messages:
                break
            page += 1
        return [self._fetch_admin_message_detail(msg, email) for msg in collected[:limit]]

    def _fetch_admin_message_detail(self, message, email):
        if not isinstance(message, dict):
            return message
        message_id = str(_first(message, "id", "message_id") or "").strip()
        if not message_id:
            return message
        recipients = _message_recipients(message)
        detail_email = recipients[0] if recipients else str(email or "").strip().lower()
        path = f"/admin/msg?id={quote(message_id, safe='')}&email={quote(detail_email, safe='')}"
        result = self._request("GET", path, allow_404=True)
        if not result.get("ok"):
            return message
        detail = _extract_single_message(result.get("data"))
        if not detail:
            return message
        return {**message, **detail}

    def _request(self, method, path, json_body=None, allow_404=False):
        url = self.base_url + path
        headers = self._headers()
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        try:
            if method == "POST":
                response = curl_requests.post(
                    url,
                    headers={**headers, "Content-Type": "application/json"},
                    data=json.dumps(json_body or {}),
                    proxies=proxies,
                    timeout=self.timeout,
                    impersonate="chrome124",
                )
            else:
                response = curl_requests.get(url, headers=headers, proxies=proxies, timeout=self.timeout, impersonate="chrome124")
            if allow_404 and response.status_code == 404:
                return {"ok": False, "error": "not_found"}
            try:
                data = response.json()
            except Exception:
                data = response.text
            if response.status_code < 200 or response.status_code >= 300:
                return {"ok": False, "status_code": response.status_code, "error": _safe_error(data)}
            return {"ok": True, "status_code": response.status_code, "data": data}
        except Exception as exc:
            fallback = self._request_urllib(method, url, headers, json_body=json_body, allow_404=allow_404)
            if fallback.get("ok") or fallback.get("error") == "not_found":
                return fallback
            return {"ok": False, "error": f"{exc}; urllib fallback: {fallback.get('error', '')}"}

    def _request_urllib(self, method, url, headers, json_body=None, allow_404=False):
        data = None
        request_headers = dict(headers)
        if method == "POST":
            request_headers["Content-Type"] = "application/json"
            data = json.dumps(json_body or {}).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                try:
                    data = json.loads(raw)
                except Exception:
                    data = raw
                return {"ok": True, "status_code": response.status, "data": data}
        except urllib.error.HTTPError as exc:
            if allow_404 and exc.code == 404:
                return {"ok": False, "error": "not_found"}
            raw = exc.read().decode("utf-8", "replace")[:500]
            try:
                data = json.loads(raw)
            except Exception:
                data = raw
            return {"ok": False, "status_code": exc.code, "error": _safe_error(data)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _headers(self):
        headers = {"Accept": "application/json"}
        if self.admin_token:
            headers["Authorization"] = "Bearer " + self.admin_token
            headers["X-Admin-Token"] = self.admin_token
        if self.cf_api_token:
            headers["X-CF-API-Token"] = self.cf_api_token
        return headers


def _extract_emails(data, domain=""):
    found = []

    def visit(value):
        if isinstance(value, dict):
            for key in ("email", "address", "mailbox", "account"):
                raw = str(value.get(key) or "").strip().lower()
                if EMAIL_RE.fullmatch(raw):
                    found.append(raw)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            found.extend(match.lower() for match in EMAIL_RE.findall(value))

    visit(data)
    unique = []
    seen = set()
    for email in found:
        if domain and not email.endswith("@" + domain):
            continue
        if email not in seen:
            seen.add(email)
            unique.append(email)
    return unique


def _extract_messages(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("messages", "mails", "emails", "items", "data", "value", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = _extract_messages(value)
                if nested:
                    return nested
    return []


def _extract_single_message(data):
    if isinstance(data, dict):
        for key in ("message", "email", "mail", "item", "data", "result"):
            value = data.get(key)
            if isinstance(value, dict) and any(k in value for k in ("subject", "bodyPreview", "body", "from", "receivedDateTime", "created_at", "message_id", "to_address")):
                return value
        if any(k in data for k in ("subject", "bodyPreview", "body", "from", "receivedDateTime", "created_at")):
            return data
    return None


def _looks_empty_message_list(data):
    if isinstance(data, list):
        return len(data) == 0
    if isinstance(data, dict):
        for key in ("messages", "mails", "emails", "items", "data", "value", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return len(value) == 0
            if isinstance(value, dict) and _looks_empty_message_list(value):
                return True
    return False


def _normalize_message(msg, email=""):
    if not isinstance(msg, dict):
        msg = {"body": str(msg or "")}
    subject = _decode_header_value(_first(msg, "subject", "title"))
    body_text = _message_body_text(msg)
    body = msg.get("body")
    if isinstance(body, dict):
        body_text = _display_text(body.get("content") or body.get("text") or body_text)
    from_value = _sender(msg)
    received = _format_received_time(_first(msg, "receivedDateTime", "received_at", "created_at", "date", "timestamp"))
    recipients = _message_recipients(msg)
    return {
        "id": str(_first(msg, "id", "message_id") or ""),
        "receivedDateTime": received,
        "from": {"emailAddress": {"address": from_value}},
        "subject": subject,
        "bodyPreview": body_text[:500],
        "body": {"content": body_text},
        "toRecipients": [{"emailAddress": {"address": address}} for address in recipients],
    }


def _message_body_text(msg):
    raw_body = str(_first(msg, "body", "raw_text", "text", "content", "html", "raw_html") or "")
    decoded_body = _decode_rfc822_body(raw_body)
    if decoded_body:
        return decoded_body
    displayed_body = _display_text(raw_body)
    extracted = str(_first(msg, "extracted_json", "results") or "")
    displayed_codes = {match.group(2) for match in OTP_RE.finditer(displayed_body)}
    extracted_codes = {match.group(2) for match in OTP_RE.finditer(extracted)}
    if displayed_codes and extracted_codes and displayed_codes.intersection(extracted_codes):
        return extracted
    if displayed_codes:
        return displayed_body
    if extracted_codes:
        return extracted
    return _display_text(_first(
        msg,
        "bodyPreview",
        "preview",
        "text",
        "content",
        "body",
        "raw_text",
        "html",
        "raw_html",
        "extracted_json",
        "results",
    ) or "")


def _decode_rfc822_body(raw):
    value = str(raw or "")
    if not value or not re.search(r"(?mi)^(from|subject|content-type|mime-version|received|dkim-signature|arc-|return-path|sender|to|date):", value):
        return ""
    try:
        message = Parser(policy=policy.default).parsestr(value)
    except Exception:
        return ""
    plain_parts = []
    html_parts = []
    for part in message.walk() if message.is_multipart() else (message,):
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True)
            charset = part.get_content_charset() or "utf-8"
            content = payload.decode(charset, "replace") if isinstance(payload, bytes) else str(payload or "")
        if content:
            if part.get_content_type() == "text/plain":
                plain_parts.append(_display_text(content))
            else:
                html_parts.append(_display_text(content))
    return "\n".join(part for part in (plain_parts or html_parts) if part)


def _decode_header_value(value):
    text = str(value or "")
    if not text:
        return ""
    try:
        return str(make_header(decode_header(text)))
    except Exception:
        return text


def _display_text(value):
    text = str(value or "")
    if not text:
        return ""
    if re.search(r"(?is)<(?:html|body|div|table|p|span|strong|title|br)\b", text):
        parser = _ReadableHtmlParser()
        try:
            parser.feed(text)
            parser.close()
            text = parser.text()
        except Exception:
            text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    text = _strip_leading_css(text)
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_leading_css(value):
    text = str(value or "")
    position = 0
    while position < len(text):
        start = position
        while start < len(text) and text[start].isspace():
            start += 1
        opening = text.find("{", start)
        if opening < 0 or opening - start > 500:
            break
        selector = text[start:opening].strip()
        simple_selector = re.fullmatch(r"(?:[#.]?[a-z][\w-]*)(?:\s*,\s*[#.]?[a-z][\w-]*)*", selector, re.I)
        if not (simple_selector or re.match(r"(?i)^@media\b", selector)):
            break
        depth = 0
        closing = -1
        for index in range(opening, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    closing = index
                    break
        if closing < 0:
            break
        position = closing + 1
    return text[position:] if position else text


class _ReadableHtmlParser(HTMLParser):
    BLOCK_TAGS = {"br", "p", "div", "tr", "li", "table", "section", "article", "h1", "h2", "h3", "h4"}
    SKIP_TAGS = {"style", "script", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)

    def text(self):
        return "".join(self.parts)


def _contains_otp(text):
    return bool(OTP_RE.search(str(text or "")))


def _first(mapping, *keys):
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return ""


def _sender(msg):
    value = msg.get("from") or msg.get("sender") or msg.get("from_email") or msg.get("from_address") or ""
    if isinstance(value, dict):
        if "emailAddress" in value and isinstance(value["emailAddress"], dict):
            return str(value["emailAddress"].get("address") or "")
        return str(value.get("address") or value.get("email") or value.get("name") or "")
    return str(value or "")


def _message_matches_email(msg, email):
    target = str(email or "").strip().lower()
    return bool(target and target in _message_recipients(msg))


def _messages_for_mailbox(messages, email, allow_missing_recipient=False):
    target = str(email or "").strip().lower()
    output = []
    for msg in messages or []:
        recipients = _message_recipients(msg)
        if target in recipients or (allow_missing_recipient and not recipients):
            output.append(msg)
    return output


def _message_recipients(msg):
    if not isinstance(msg, dict):
        return []
    values = []
    for key in ("to_address", "recipient", "mailbox", "email", "address", "to"):
        if key in msg:
            values.extend(_emails_from_value(msg.get(key)))
    for key in ("toRecipients", "recipients"):
        for item in msg.get(key) or []:
            values.extend(_emails_from_value(item))
    unique = []
    seen = set()
    for value in values:
        email = value.strip().lower()
        if email and email not in seen:
            seen.add(email)
            unique.append(email)
    return unique


def _emails_from_value(value):
    if isinstance(value, dict):
        found = []
        if "emailAddress" in value:
            found.extend(_emails_from_value(value.get("emailAddress")))
        for key in ("address", "email", "mail", "value"):
            found.extend(_emails_from_value(value.get(key)))
        return found
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(_emails_from_value(item))
        return found
    return [match.lower() for match in EMAIL_RE.findall(str(value or ""))]


def _format_received_time(value):
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        try:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))
        except Exception:
            return str(value)
    text = str(value or "")
    if text.isdigit():
        return _format_received_time(float(text))
    return text


def _safe_error(data):
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return text[:500]
