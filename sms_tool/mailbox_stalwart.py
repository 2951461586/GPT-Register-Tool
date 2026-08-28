"""Stalwart / generic JMAP mailbox OTP poller.

Stalwart is a real MTA that exposes JMAP (and IMAP) for reading mail. This
module lets the registration flow treat a self-hosted Stalwart domain mailbox as
a first-class OTP source: OpenAI delivers the verification code to the domain's
MX, and we read it back over JMAP with HTTP Basic auth.

Registration is automatic on import (no central if-elif needed): the poller is
registered under provider name ``stalwart`` via ``mailbox_strategies``.

Account discovery
-----------------
A Stalwart login is *not* limited to a single JMAP account. In the deployment
this was written against, the Cloudflare Email Routing worker imports mail into
a *different* internal account (JMAP accountId ``c``) than the one the login
resolves to as primary (``b``). Polling only ``primaryAccounts`` therefore
returned an empty inbox forever.

So we enumerate every account the credentials can reach via ``Principal/query``
and poll all of their inboxes. That keeps the poller correct even if the
delivery account changes, and requires no server-side change.
"""
from __future__ import annotations

import re
import time

import requests

from .mailbox_strategies import register_otp_poller

STALWART_DEFAULT_BASE = "https://stalwart.liziai.cloud"
_JMAP_CAPS = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"]
_JMAP_PRINCIPAL_CAPS = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:principals"]
_OTP_RE = re.compile(r"(?:\b|^)([0-9]{5,8})(?:\b|$)")

# A mail must look like an OpenAI verification mail to be trusted immediately.
# Far from the deadline we insist on these markers; only when we are about to
# give up do we fall back to a looser scan.
_OAI_MARKERS = ("openai", "chatgpt", "verification code", "verify your", "验证码")


def _stalwart_base(mailbox) -> str:
    host = str(getattr(mailbox, "host", "") or "").strip()
    if host:
        return host if host.startswith("http") else f"https://{host}"
    return STALWART_DEFAULT_BASE


def _jmap_session(base: str, email: str, password: str):
    s = requests.Session()
    s.auth = (email, password)
    s.headers.update({"Accept": "application/json"})
    r = s.get(f"{base}/.well-known/jmap", timeout=25)
    r.raise_for_status()
    return s, r.json()


def _jmap_call(sess_http, api_url: str, using: list[str], method: str,
               args: dict, cid: str = "c0"):
    """POST a single JMAP method call; return (name, args) or (None, None)."""
    body = {"using": using, "methodCalls": [[method, args, cid]]}
    r = sess_http.post(api_url, json=body, timeout=30)
    r.raise_for_status()
    responses = r.json().get("methodResponses") or []
    if not responses:
        return None, None
    name, margs, _c = responses[0]
    return name, margs


def _inbox_id(sess_http, api_url: str, account_id: str):
    name, args = _jmap_call(sess_http, api_url, _JMAP_CAPS, "Mailbox/query",
                            {"accountId": account_id, "filter": {"role": "inbox"}}, "m1")
    if name == "Mailbox/query" and args and args.get("ids"):
        return args["ids"][0]
    # fallback: first mailbox
    name, args = _jmap_call(sess_http, api_url, _JMAP_CAPS, "Mailbox/query",
                            {"accountId": account_id}, "m2")
    if name == "Mailbox/query" and args and args.get("ids"):
        return args["ids"][0]
    return None


def _discover_account_ids(sess_http, api_url: str, primary_id: str):
    """Return every JMAP accountId the credentials can reach.

    ``primary_id`` is always first so the common case stays cheap; the rest come
    from ``Principal/query``. Discovery is best-effort — on any failure we just
    fall back to ``[primary_id]``.
    """
    ids: list[str] = []
    if primary_id:
        ids.append(primary_id)
    try:
        name, args = _jmap_call(sess_http, api_url, _JMAP_PRINCIPAL_CAPS,
                                "Principal/query",
                                {"accountId": primary_id, "limit": 100}, "p1")
        if name == "Principal/query" and args and not args.get("type"):
            for pid in args.get("ids", []) or []:
                if pid not in ids:
                    ids.append(pid)
    except Exception as e:
        print(f"[stalwart otp] principal discovery unavailable ({e}); "
              f"using primary account only")
    return ids


def _polling_targets(sess_http, api_url: str, account_ids: list[str]):
    """Map each reachable accountId to its inbox id; drop unreachable ones."""
    targets: list[tuple[str, str]] = []
    for aid in account_ids:
        try:
            inbox = _inbox_id(sess_http, api_url, aid)
        except Exception as e:
            print(f"[stalwart otp] account {aid} unreachable: {e}")
            continue
        if inbox:
            targets.append((aid, inbox))
    return targets


def _body_text(em: dict, values: dict, key: str) -> str:
    """Join JMAP EmailBodyPart references into plain text.

    ``textBody``/``htmlBody`` are lists of EmailBodyPart objects, not strings;
    the actual content lives in ``bodyValues[partId].value``.
    """
    parts = em.get(key) or []
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            pid = part.get("partId")
            # Some servers inline the value on the part itself.
            inline = part.get("value") or part.get("text")
            if inline:
                chunks.append(str(inline))
                continue
        else:
            pid = part
        if pid is None:
            continue
        entry = values.get(pid) or {}
        if entry.get("value"):
            chunks.append(str(entry["value"]))
    return "\n".join(chunks)


def _candidate_code(blob: str, excluded: set[str] | None = None) -> str | None:
    """Extract an OTP from mail text, reusing the shared scoring extractor."""
    code = ""
    try:
        from .mail_otp import _extract_otp_from_text
        code = _extract_otp_from_text(blob) or ""
    except Exception:
        code = ""
    if not code:
        m = _OTP_RE.search(blob or "")
        code = m.group(1) if m else ""
    if not code:
        return None
    if excluded and code in excluded:
        return None
    return code


def _keyword_hit(blob_lc: str, keyword: str) -> bool:
    """``subject_keyword`` arrives pipe-separated, e.g. "verification code|login code".

    Mirrors sms_tool.mail_otp: split on "|" and accept if any part matches.
    """
    parts = [p.strip().lower() for p in str(keyword or "").split("|") if p.strip()]
    if not parts:
        return True
    return any(p in blob_lc for p in parts)


def _looks_like_otp_mail(blob_lc: str, keyword: str) -> bool:
    """High-confidence match: caller keyword AND an OpenAI marker."""
    if not _keyword_hit(blob_lc, keyword):
        return False
    return any(marker in blob_lc for marker in _OAI_MARKERS)


def _has_openai_marker(blob_lc: str) -> bool:
    return any(marker in blob_lc for marker in _OAI_MARKERS)


def _stalwart_matcher(mailbox, config=None) -> bool:
    return str(getattr(mailbox, "provider", "") or "").strip().lower() == "stalwart"


def poll_stalwart_otp(
    mailbox,
    subject_keyword: str = "",
    timeout: int = 300,
    issued_after_unix: int = 0,
    proxy=None,
    excluded_otps: set[str] | None = None,
    runtime_config=None,
    registry=None,
) -> str | None:
    """Poll Stalwart JMAP inboxes for an OpenAI verification code.

    Reads mail directly over HTTPS (no proxy needed — the Stalwart host is
    publicly reachable; only outbound port 25 SMTP is blocked on some hosts).

    Every account the credentials can reach is polled, because the delivery
    account may differ from the login's primary account.
    """
    base = _stalwart_base(mailbox)
    email = str(getattr(mailbox, "email", "") or "").strip()
    password = str(getattr(mailbox, "password", "") or "").strip()
    if not email or not password:
        print("[stalwart otp] missing email/password")
        return None

    try:
        sess_http, sess = _jmap_session(base, email, password)
    except Exception as e:
        print(f"[stalwart otp] JMAP session failed: {e}")
        return None

    api_url = sess.get("apiUrl") or f"{base}/jmap/"
    primary_id = sess.get("primaryAccounts", {}).get("urn:ietf:params:jmap:mail")
    if not primary_id:
        print("[stalwart otp] no mail account in JMAP session")
        return None

    account_ids = _discover_account_ids(sess_http, api_url, primary_id)
    targets = _polling_targets(sess_http, api_url, account_ids)
    if not targets:
        print("[stalwart otp] no reachable inbox")
        return None

    keyword = (subject_keyword or "").lower().strip()
    deadline = time.time() + max(timeout, 20)
    loose_after = time.time() + max(timeout, 20) * 0.75
    interval = 5.0
    excluded = set(excluded_otps or set())
    loose_hit: str | None = None      # no OpenAI marker at all — last resort
    marker_hit: str | None = None     # OpenAI marker but caller keyword missed
    print(f"[stalwart otp] polling accounts={[t[0] for t in targets]} base={base} "
          f"email={email} timeout={timeout}s keyword={keyword!r}")

    while time.time() < deadline:
        allow_loose = time.time() >= loose_after
        try:
            for account_id, inbox in targets:
                body = {"using": _JMAP_CAPS,
                        "methodCalls": [
                            ["Email/query", {"accountId": account_id,
                                             "inMailbox": inbox,
                                             "limit": 10,
                                             "sort": [{"property": "receivedAt",
                                                       "isAscending": False}]}, "q"],
                            ["Email/get", {"accountId": account_id,
                                           "#ids": {"resultOf": "q",
                                                    "name": "Email/query",
                                                    "path": "/ids"},
                                           "properties": ["subject", "from", "to",
                                                          "receivedAt", "messageId",
                                                          "textBody", "htmlBody",
                                                          "bodyValues"],
                                           "fetchAllBodyValues": True}, "g"]]}
                r = sess_http.post(api_url, json=body, timeout=30)
                r.raise_for_status()
                for name, args, _cid in r.json().get("methodResponses", []):
                    if name != "Email/get":
                        continue
                    for em in args.get("list", []):
                        recv = str(em.get("receivedAt", ""))
                        recv_ts = _parse_iso(recv)
                        if issued_after_unix and recv_ts and recv_ts < issued_after_unix - 30:
                            continue
                        subj = str(em.get("subject", "") or "")
                        values = em.get("bodyValues") or {}
                        text = _body_text(em, values, "textBody")
                        if not text.strip():
                            html = _body_text(em, values, "htmlBody")
                            text = re.sub(r"<[^>]+>", " ", html)
                        blob = f"{subj}\n{text}"
                        blob_lc = blob.lower()
                        if _looks_like_otp_mail(blob_lc, keyword):
                            code = _candidate_code(blob, excluded)
                            if code:
                                print(f"[stalwart otp] got code={code} "
                                      f"account={account_id} subject={subj!r} recv={recv}")
                                return code
                        elif _has_openai_marker(blob_lc):
                            # Clearly an OpenAI mail, but the caller's
                            # subject_keyword did not match — keep it as a
                            # fallback instead of discarding it.
                            if marker_hit is None:
                                marker_hit = _candidate_code(blob, excluded)
                                print(f"[stalwart otp] keyword miss, holding code="
                                      f"{marker_hit} subject={subj!r}")
                        elif allow_loose and loose_hit is None:
                            loose_hit = _candidate_code(blob, excluded)
        except Exception as e:
            print(f"[stalwart otp] poll error: {e}")

        if marker_hit and allow_loose:
            print(f"[stalwart otp] using keyword-miss code={marker_hit}")
            return marker_hit
        time.sleep(interval)

    if marker_hit:
        print(f"[stalwart otp] using keyword-miss code={marker_hit} (timeout fallback)")
        return marker_hit
    if loose_hit:
        print(f"[stalwart otp] falling back to unverified code={loose_hit} "
              f"(no OpenAI-marked mail matched)")
        return loose_hit
    print("[stalwart otp] poll timeout, no code")
    return None


def _parse_iso(s: str) -> int:
    if not s:
        return 0
    try:
        # JMAP receivedAt is RFC3339 UTC, e.g. 2026-08-28T12:00:00Z
        s2 = s.replace("Z", "+00:00")
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


# Self-register the Stalwart OTP poller under provider name "stalwart".
register_otp_poller("stalwart", _stalwart_matcher, poll_stalwart_otp)
