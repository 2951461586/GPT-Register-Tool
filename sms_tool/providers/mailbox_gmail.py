"""Gmail IMAP/SMTP adapter used by mailbox polling and K12 invite handling."""

from __future__ import annotations

import base64
import html
import imaplib
import re
import socket
import smtplib
import ssl
from urllib.parse import unquote, urlparse
from email.message import EmailMessage
from email.utils import formataddr

from curl_cffi import requests as curl_requests

from .outlook_imap import discover_imap_folders, imap_message_to_graph_shape


GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}
DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 465
DEFAULT_IMAP_FOLDERS = ["INBOX", "[Gmail]/Spam", "Spam", "[Gmail]/All Mail"]
DEFAULT_OAUTH_SCOPE = "https://mail.google.com/"


def mailbox_domain(mailbox):
    email_value = str(getattr(mailbox, "email", "") or "").strip().lower()
    return email_value.rsplit("@", 1)[1] if "@" in email_value else ""


def is_gmail_mailbox(mailbox):
    domain = mailbox_domain(mailbox)
    provider = str(getattr(mailbox, "provider", "") or "").strip().lower()
    return provider == "gmail" or domain in GMAIL_DOMAINS


def mailbox_auth_mode(mailbox):
    explicit = str(getattr(mailbox, "auth_mode", "") or "").strip().lower()
    if explicit in {"app_password", "oauth_refresh"}:
        return explicit
    if getattr(mailbox, "refresh_token", "") and getattr(mailbox, "token", "") and getattr(mailbox, "client_secret", ""):
        return "oauth_refresh"
    if getattr(mailbox, "access_token", "") and not getattr(mailbox, "password", ""):
        return "oauth_refresh"
    if getattr(mailbox, "password", ""):
        return "app_password"
    if getattr(mailbox, "access_token", ""):
        return "oauth_refresh"
    return ""


def mailbox_has_credentials(mailbox, cfg=None):
    if not is_gmail_mailbox(mailbox):
        return False
    mode = mailbox_auth_mode(mailbox)
    if mode == "app_password":
        return bool(_normalize_app_password(getattr(mailbox, "password", "")))
    if mode == "oauth_refresh":
        client_id = str(getattr(mailbox, "token", "") or (cfg or {}).get("client_id") or "").strip()
        client_secret = str(getattr(mailbox, "client_secret", "") or (cfg or {}).get("client_secret") or "").strip()
        refresh_token = str(getattr(mailbox, "refresh_token", "") or "").strip()
        access_token = str(getattr(mailbox, "access_token", "") or "").strip()
        return bool(access_token or (client_id and client_secret and refresh_token))
    return False


def refresh_gmail_access_token(mailbox, cfg, proxy=None, scope_override=None):
    cfg = cfg if isinstance(cfg, dict) else {}
    client_id = str(getattr(mailbox, "token", "") or cfg.get("client_id") or "").strip()
    client_secret = str(getattr(mailbox, "client_secret", "") or cfg.get("client_secret") or "").strip()
    refresh_token = str(getattr(mailbox, "refresh_token", "") or "").strip()
    token_url = str(cfg.get("token_url") or "https://oauth2.googleapis.com/token").strip()
    if not client_id or not client_secret or not refresh_token:
        raise RuntimeError("gmail oauth_refresh requires client_id, client_secret, and refresh_token")
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }
    if scope_override:
        data["scope"] = scope_override
    proxies = {"http": proxy, "https": proxy} if proxy else None
    response = curl_requests.post(token_url, data=data, proxies=proxies, impersonate="chrome124", timeout=30)
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text[:500]}
    if response.status_code != 200:
        raise RuntimeError(f"gmail token refresh failed: {body}")
    access_token = str(body.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("gmail token refresh returned empty access token")
    mailbox.access_token = access_token
    if body.get("refresh_token"):
        mailbox.refresh_token = str(body["refresh_token"]).strip()
    return access_token


def fetch_gmail_imap_messages(
    mailbox,
    token_fetcher=None,
    folders=None,
    limit=25,
    host=DEFAULT_IMAP_HOST,
    port=DEFAULT_IMAP_PORT,
    proxy=None,
    timeout=30,
):
    mode = mailbox_auth_mode(mailbox)
    folders = list(folders or DEFAULT_IMAP_FOLDERS)
    limit = max(1, min(int(limit or 25), 50))
    mail = _imap_ssl_client(host, int(port or DEFAULT_IMAP_PORT), proxy=proxy, timeout=timeout)
    messages = []
    seen = set()
    try:
        if mode == "oauth_refresh":
            if token_fetcher is None:
                raise RuntimeError("gmail oauth_refresh requires token_fetcher")
            _gmail_imap_auth_xoauth2(mail, mailbox, token_fetcher)
        elif mode == "app_password":
            password = _normalize_app_password(getattr(mailbox, "password", ""))
            if not password:
                raise RuntimeError("gmail app password is required")
            mail.login(mailbox.email, password)
        else:
            raise RuntimeError("unsupported gmail auth mode")
        for folder in discover_imap_folders(mail, folders):
            try:
                typ, _ = mail.select(f'"{folder}"', readonly=True)
                if typ != "OK":
                    typ, _ = mail.select(folder, readonly=True)
                if typ != "OK":
                    continue
                typ, nums = mail.search(None, "ALL")
                if typ != "OK" or not nums or not nums[0]:
                    continue
                selected = nums[0].split()[-limit:]
                for num in reversed(selected):
                    typ, data = mail.fetch(num, "(RFC822)")
                    if typ != "OK" or not data:
                        continue
                    for item in data:
                        if isinstance(item, tuple) and item[1]:
                            shaped = imap_message_to_graph_shape(folder, num, item[1])
                            key = str(shaped.get("message_id") or shaped.get("id") or "")
                            if key and key in seen:
                                break
                            if key:
                                seen.add(key)
                            messages.append(shaped)
                            break
                    if len(messages) >= limit:
                        return messages
            except Exception as exc:
                print(f"[gmail imap folder {folder} error: {exc}]")
                continue
    finally:
        try:
            mail.logout()
        except Exception:
            pass
    if messages:
        print(f"[gmail imap] fetched {len(messages)} message(s)")
    return messages


def send_gmail_message(
    mailbox,
    to_addresses,
    subject,
    text_body="",
    html_body="",
    cfg=None,
    proxy=None,
    timeout=30,
):
    del proxy  # SMTP proxying is not supported in the stdlib transport.
    cfg = cfg if isinstance(cfg, dict) else {}
    auth_mode = mailbox_auth_mode(mailbox)
    host = str(cfg.get("smtp_host") or DEFAULT_SMTP_HOST).strip() or DEFAULT_SMTP_HOST
    port = int(cfg.get("smtp_port") or DEFAULT_SMTP_PORT)
    use_ssl = _as_bool(cfg.get("smtp_use_ssl"), True)
    sender_name = str(getattr(mailbox, "sender_name", "") or cfg.get("sender_name") or "").strip()
    recipients = _normalize_recipients(to_addresses)
    if not recipients:
        raise RuntimeError("gmail send requires at least one recipient")
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, mailbox.email)) if sender_name else mailbox.email
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = str(subject or "").strip() or "Gmail test"
    normalized_text = str(text_body or "").strip()
    normalized_html = str(html_body or "").strip()
    if normalized_html:
        msg.set_content(normalized_text or _html_to_text(normalized_html))
        msg.add_alternative(normalized_html, subtype="html")
    else:
        msg.set_content(normalized_text or "(empty)")

    if use_ssl:
        server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)
    try:
        if not use_ssl:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        if auth_mode == "oauth_refresh":
            access_token = str(getattr(mailbox, "access_token", "") or "").strip()
            if not access_token:
                access_token = refresh_gmail_access_token(mailbox, cfg, scope_override=DEFAULT_OAUTH_SCOPE)
            auth_string = _xoauth2_string(mailbox.email, access_token)
            code, resp = server.docmd("AUTH", "XOAUTH2 " + base64.b64encode(auth_string).decode("ascii"))
            if int(code or 0) != 235:
                raise RuntimeError(f"gmail smtp oauth auth failed: {code} {resp!r}")
        elif auth_mode == "app_password":
            password = _normalize_app_password(getattr(mailbox, "password", ""))
            if not password:
                raise RuntimeError("gmail app password is required")
            server.login(mailbox.email, password)
        else:
            raise RuntimeError("unsupported gmail auth mode")
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass
    return {
        "ok": True,
        "provider": "gmail",
        "from": mailbox.email,
        "to": recipients,
        "subject": msg["Subject"],
        "auth_mode": auth_mode,
        "smtp_host": host,
        "smtp_port": port,
    }


def _gmail_imap_auth_xoauth2(mail, mailbox, token_fetcher):
    access_token = str(getattr(mailbox, "access_token", "") or "").strip()
    for attempt in range(2):
        if not access_token:
            access_token = token_fetcher(DEFAULT_OAUTH_SCOPE)
        auth_string = _xoauth2_string(mailbox.email, access_token)
        try:
            typ, _ = mail.authenticate("XOAUTH2", lambda _: auth_string)
            if typ == "OK":
                mailbox.access_token = access_token
                return
        except imaplib.IMAP4.error:
            pass
        access_token = token_fetcher(DEFAULT_OAUTH_SCOPE)
    raise RuntimeError("gmail imap XOAUTH2 failed")


def _xoauth2_string(email_address, access_token):
    return f"user={email_address}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")


def _imap_ssl_client(host, port=DEFAULT_IMAP_PORT, proxy=None, timeout=30):
    proxy = str(proxy or "").strip()
    if not proxy:
        return imaplib.IMAP4_SSL(host, int(port or DEFAULT_IMAP_PORT), timeout=timeout)
    return _ProxiedIMAP4_SSL(host, int(port or DEFAULT_IMAP_PORT), proxy=proxy, timeout=timeout)


class _ProxiedIMAP4_SSL(imaplib.IMAP4_SSL):
    def __init__(self, host="", port=DEFAULT_IMAP_PORT, *, proxy, ssl_context=None, timeout=30):
        self._target_host = host
        self._target_port = int(port or DEFAULT_IMAP_PORT)
        self._proxy = proxy
        super().__init__(
            host=host,
            port=self._target_port,
            ssl_context=ssl_context or ssl.create_default_context(),
            timeout=timeout,
        )

    def _create_socket(self, timeout):
        raw_sock = _create_proxied_socket(
            self._target_host,
            self._target_port,
            self._proxy,
            timeout=timeout,
        )
        return self.ssl_context.wrap_socket(raw_sock, server_hostname=self._target_host)


def _create_proxied_socket(host, port, proxy, timeout=30):
    parsed = _parse_proxy_url(proxy)
    scheme = parsed["scheme"]
    if scheme in {"socks5", "socks5h"}:
        return _create_socks5_socket(host, port, parsed, timeout=timeout, remote_dns=(scheme == "socks5h"))
    if scheme in {"http", "https"}:
        # For HTTPS proxy URLs we still use HTTP CONNECT to the proxy endpoint.  If a
        # deployment ever needs TLS to the proxy itself, add it explicitly instead
        # of silently changing the tunnel semantics here.
        return _create_http_connect_socket(host, port, parsed, timeout=timeout)
    raise RuntimeError(f"unsupported gmail imap proxy scheme: {scheme}")


def _parse_proxy_url(proxy):
    value = str(proxy or "").strip()
    if not value:
        raise RuntimeError("empty gmail imap proxy")
    if "://" not in value:
        value = "http://" + value
    parsed = urlparse(value)
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname
    if not host:
        raise RuntimeError(f"invalid gmail imap proxy: {proxy!r}")
    if parsed.port:
        port = parsed.port
    elif scheme in {"socks5", "socks5h"}:
        port = 1080
    elif scheme in {"http", "https"}:
        port = 8080
    else:
        port = 0
    return {
        "scheme": scheme,
        "host": host,
        "port": int(port),
        "username": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
    }


def _create_tcp_socket(host, port, timeout=30):
    return socket.create_connection((host, int(port)), timeout=timeout)


def _create_socks5_socket(host, port, proxy, timeout=30, remote_dns=True):
    sock = _create_tcp_socket(proxy["host"], proxy["port"], timeout=timeout)
    try:
        username = proxy.get("username") or ""
        password = proxy.get("password") or ""
        methods = [0x00]
        if username or password:
            methods.append(0x02)
        sock.sendall(bytes([0x05, len(methods), *methods]))
        ver_method = _recv_exact(sock, 2)
        if ver_method[0] != 0x05:
            raise RuntimeError("socks5 proxy returned invalid version")
        method = ver_method[1]
        if method == 0xFF:
            raise RuntimeError("socks5 proxy has no acceptable auth method")
        if method == 0x02:
            user_b = username.encode("utf-8")
            pass_b = password.encode("utf-8")
            if len(user_b) > 255 or len(pass_b) > 255:
                raise RuntimeError("socks5 proxy credentials are too long")
            sock.sendall(bytes([0x01, len(user_b)]) + user_b + bytes([len(pass_b)]) + pass_b)
            auth_reply = _recv_exact(sock, 2)
            if auth_reply[1] != 0x00:
                raise RuntimeError("socks5 proxy username/password auth failed")
        elif method != 0x00:
            raise RuntimeError(f"unsupported socks5 proxy auth method: {method}")

        sock.sendall(_socks5_connect_request(host, port, remote_dns=remote_dns))
        reply = _recv_exact(sock, 4)
        if reply[0] != 0x05:
            raise RuntimeError("socks5 proxy connect returned invalid version")
        if reply[1] != 0x00:
            raise RuntimeError(f"socks5 proxy connect failed: {_SOCKS5_REPLY_CODES.get(reply[1], reply[1])}")
        atyp = reply[3]
        if atyp == 0x01:
            _recv_exact(sock, 4)
        elif atyp == 0x03:
            size = _recv_exact(sock, 1)[0]
            _recv_exact(sock, size)
        elif atyp == 0x04:
            _recv_exact(sock, 16)
        else:
            raise RuntimeError(f"socks5 proxy returned invalid address type: {atyp}")
        _recv_exact(sock, 2)
        return sock
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        raise


def _socks5_connect_request(host, port, remote_dns=True):
    port_b = int(port).to_bytes(2, "big")
    if remote_dns:
        host_b = str(host).encode("idna")
        if len(host_b) > 255:
            raise RuntimeError("socks5 target host is too long")
        return b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b + port_b
    info = socket.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
    if not info:
        raise RuntimeError(f"socks5 local dns failed for {host}")
    family, _, _, _, sockaddr = info[0]
    addr = sockaddr[0]
    if family == socket.AF_INET:
        return b"\x05\x01\x00\x01" + socket.inet_pton(socket.AF_INET, addr) + port_b
    if family == socket.AF_INET6:
        return b"\x05\x01\x00\x04" + socket.inet_pton(socket.AF_INET6, addr) + port_b
    raise RuntimeError(f"socks5 unsupported local dns family: {family}")


def _create_http_connect_socket(host, port, proxy, timeout=30):
    sock = _create_tcp_socket(proxy["host"], proxy["port"], timeout=timeout)
    try:
        authority = f"{host}:{int(port)}"
        headers = [
            f"CONNECT {authority} HTTP/1.1",
            f"Host: {authority}",
            "Proxy-Connection: Keep-Alive",
        ]
        username = proxy.get("username") or ""
        password = proxy.get("password") or ""
        if username or password:
            token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            headers.append(f"Proxy-Authorization: Basic {token}")
        sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
        response = _recv_until(sock, b"\r\n\r\n", limit=65536)
        status_line = response.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
        parts = status_line.split()
        status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        if status < 200 or status >= 300:
            raise RuntimeError(f"http proxy CONNECT failed: {status_line}")
        return sock
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        raise


def _recv_exact(sock, n):
    chunks = []
    remaining = int(n)
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise RuntimeError("proxy connection closed unexpectedly")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_until(sock, marker, limit=65536):
    data = bytearray()
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("proxy connection closed unexpectedly")
        data.extend(chunk)
        if len(data) > limit:
            raise RuntimeError("proxy response is too large")
    return bytes(data)


_SOCKS5_REPLY_CODES = {
    0x01: "general failure",
    0x02: "connection not allowed",
    0x03: "network unreachable",
    0x04: "host unreachable",
    0x05: "connection refused",
    0x06: "ttl expired",
    0x07: "command not supported",
    0x08: "address type not supported",
}


def _normalize_app_password(secret):
    value = str(secret or "").strip()
    compact = "".join(value.split())
    if len(compact) == 16 and " " in value:
        return compact
    return value


def _normalize_recipients(value):
    if isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = re.split(r"[\r\n,;]+", str(value or ""))
    recipients = []
    seen = set()
    for item in raw_items:
        email_value = str(item or "").strip()
        if not email_value:
            continue
        lower = email_value.lower()
        if lower in seen:
            continue
        seen.add(lower)
        recipients.append(email_value)
    return recipients


def _html_to_text(value):
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", str(value or ""))
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", html.unescape(text))
    return text.strip()


def _as_bool(value, default):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
