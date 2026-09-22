"""Batch TOTP (2FA) enrollment for EXISTING saved accounts.

Why this exists
---------------
2FA enrollment in this project only ever ran inside the registration pipeline
(``registration_finalize.enroll_totp`` -> ``accounts/account_2fa.setup_totp_2fa``).
Accounts registered with ``--no-2fa`` (the WPF default since 2026-09-12) never
got a chance, and there was no way to backfill them.  This is that backfill.

Reused machinery (no new protocol logic)
----------------------------------------
* ``accounts.account_2fa``       -- ``_mfa_info`` / ``_totp_already_enabled`` /
                                    ``_enroll_totp`` / ``_activate_totp`` /
                                    ``_needs_reauth``
* ``auth_flow._login_existing_account_with_email_otp``
                                 -- the proven passwordless re-login lane
                                    (it polls the mailbox OTP itself)
* ``account_creation._fetch_auth_session`` / ``_auth_session_access_token``
* ``proxy_routing.operation_proxy_candidates``
                                 -- same egress decision point as every other
                                    post-registration operation

Why the enrollment is re-implemented instead of calling ``setup_totp_2fa``
--------------------------------------------------------------------------
``setup_totp_2fa`` activates the enrollment and only *then* returns the secret,
and it discards the secret entirely when activation raises.  Two consequences:

1. A crash between "activation succeeded server-side" and "secret persisted"
   leaves the account requiring a TOTP code nobody holds -> locked out.
2. An activation failure loses a secret that may still be the account's real
   enrolled factor.

So this runner journals the secret to an append-only, fsync'd file the moment
``_enroll_totp`` returns -- i.e. **before** ``_activate_totp`` is attempted --
and keeps it on activation failure.  That journal is the rollback path.

Usage
-----
    python scripts/batch_enable_2fa.py --dry-run
    python scripts/batch_enable_2fa.py --limit 5 --workers 3
    python scripts/batch_enable_2fa.py --workers 6
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "runtime" / "analysis"
DB = ROOT / "runtime" / "accounts.sqlite3"

PROMO_FREE = "Free·无优惠"

#: Substring that identifies a dead proxy pool.  ``curl: (7) CONNECT tunnel
#: failed, response 407`` is the gateway saying *the credentials are rejected*,
#: which is an account-level condition -- see the circuit breaker in ``main``.
POOL_DEAD_MARKER = "response 407"
POOL_DEAD_STREAK_LIMIT = 12

#: Markers for the *other* pool failure mode, measured 2026-09-21 04:42-04:51:
#: the gateway accepts our auth and then cannot establish the upstream tunnel,
#: answering ``502 Bad Gateway`` on CONNECT.  Unlike 407 this is transient --
#: ten minutes later the same sids were 9/10 healthy again with ``loc=IN`` --
#: but during the outage the success rate collapsed from ~100% to 27%, so it
#: still spends accounts.  The correct response is to pause, not to abort.
#: ``curl: (7)`` covers the tunnel failure itself; ``response 502`` covers the
#: gateway's own answer, which is what the error string actually carries.
TRANSIENT_DEAD_MARKERS = ("response 502", "curl: (7)")
TRANSIENT_STREAK_LIMIT = 10
TRANSIENT_COOLDOWN_SECONDS = 120
#: Give up if the outage survives this many cooldowns -- at that point it is no
#: longer transient and the operator has to decide, exactly like a 407.
TRANSIENT_COOLDOWN_MAX = 3

#: State string returned by ``run_one`` when OpenAI reports the account already
#: has 2FA enabled.  Defined above ``TERMINAL_ERROR_MARKERS`` because it is a
#: member of that tuple.
ALREADY_ENROLLED_MARKER = "already_enrolled"

#: Failure classes that reproduce *identically* on every retry: a dead mailbox
#: endpoint, a dead app-specific password, an account OpenAI has deleted.  They
#: are not transient, so re-attempting them costs a full login round-trip and
#: buys nothing.
#:
#: This matters more than it looks, because of the sort order in
#: ``load_targets``: "cheap-first" puts accounts that carry an access token at
#: the FRONT, and the dead-mailbox accounts all carry one.  So without this
#: filter every pass opens by grinding through the same graveyard.  Measured
#: 2026-09-21 run 3: 13 of the first 21 attempts were terminal (8 x HTTP 404,
#: 5 x mailbox_auth_invalid), and 29 accounts in total carry a terminal marker.
#:
#: Deliberately a *filter*, not a deletion -- the rows stay in the DB and the
#: export still lists them (with an empty 2FA field).  Use ``--retry-terminal``
#: to attempt them anyway, e.g. after replenishing the mailbox source.
TERMINAL_ERROR_MARKERS = (
    "mailbox_auth_invalid",
    "mailbox_endpoint_unavailable",
    "has been deleted or deactivated",
    # The account has a password step but we hold no password for it, so the
    # email lane cannot produce a session and there is nothing else to try.
    # Verified 2026-09-21 on the two accounts that hit this: both had an empty
    # ``password`` in the DB column *and* in raw_json.
    "existing_login_password_required",
    # The account is passwordless in a way that lands the email code on the
    # signup profile step and yields no session -- auth_flow deliberately stops
    # rather than spending the code to learn the same thing.
    "existing_login_no_password_step",
    # 2FA is already on for this account but we never captured the secret
    # (the crash-safe journal has no record of it either -- cross-checked
    # 2026-09-21: 0 journal-only secrets).  Unrecoverable from our side.
    "existing_login_totp_secret_missing",
    #: ``already_enrolled`` is reported with ``ok=True`` and **no** secret --
    #: OpenAI already has 2FA on the account and never hands the TOTP secret
    #: back, so it is unrecoverable too.  🔴 It has to be listed here *and*
    #: persisted by the consumer loop, because ``ok=True`` makes both persist
    #: branches skip it: without this the account is re-attempted (a full
    #: login round) on every single pass and the export cannot tell it apart
    #: from a transient blip.  Measured 2026-09-21: two accounts reported
    #: ``already_enrolled`` on 10+ passes in one day, and the export filed
    #: them under "retryable" because the error column held only the last
    #: transport hiccup.
    ALREADY_ENROLLED_MARKER,
)

#: Deliberately NOT in the list above: ``missing_mailbox``.  It returns before
#: any network call (batch_enable_2fa.py:361), so it costs ~nothing per pass,
#: and replenishing the mailbox pool can make those accounts resolvable again.

#: The session lane's own 429 circuit breaker.  Once it opens, *every*
#: subsequent account fails instantly with this marker and is never actually
#: attempted -- so continuing just burns the list.  Measured 2026-09-21 on the
#: local US exit: 8 of 28 accounts in one pass were collateral of this, not
#: real failures.
#:
#: Unlike the proxy breakers this is not a streak condition: one occurrence
#: means the whole session lane is shut.  The circuit reports its own
#: ``retry_after`` (observed ``retry_after=300s``), so honour that rather than
#: guessing a cooldown.
SESSION_CIRCUIT_MARKER = "session_circuit_open"
SESSION_CIRCUIT_DEFAULT_WAIT = 300
SESSION_CIRCUIT_MAX_WAITS = 6

# ---------------------------------------------------------------- journal ---

_JOURNAL_LOCK = threading.Lock()


class Journal:
    """Append-only, fsync'd record of every TOTP secret we have ever seen.

    Written before activation.  If the process dies mid-run, this file is the
    only place the secret exists -- treat it as the recovery source of truth.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        if not path.exists():
            path.write_text(
                "# email\tstate\ttotp_secret\tunix_ts\tiso_ts\n",
                encoding="utf-8",
            )

    def append(self, email: str, state: str, secret: str) -> None:
        now = int(time.time())
        line = (
            f"{email}\t{state}\t{secret}\t{now}\t"
            f"{datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        with _JOURNAL_LOCK:
            with open(self.path, "a", encoding="utf-8", newline="") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())


# ------------------------------------------------------------------ store ---


def persist_twofa(email: str, secret: str, error: str = "") -> bool:
    """Surgically write the 2FA columns, mirroring ``mark_promotion_status``.

    Deliberately not ``upsert_account``: that recomputes status/success/plan
    from the whole model and would rewrite unrelated columns.
    """
    from sms_tool.store import _connect, _find_existing_account_email, _update_session_json

    now = int(time.time())
    conn = _connect()
    json_path = ""
    data: dict = {}
    try:
        lookup = _find_existing_account_email(conn, email) or email
        row = conn.execute(
            "SELECT raw_json, json_path FROM accounts WHERE email=?", (lookup,)
        ).fetchone()
        if row is None:
            return False
        try:
            data = json.loads(row["raw_json"] or "{}")
        except Exception:
            data = {}
        json_path = str(row["json_path"] or "").strip()
        if secret:
            data["totp_secret"] = secret
            data["twofa_enrolled_at"] = now
            data["twofa_enroll_error"] = ""
        elif error:
            data["twofa_enroll_error"] = error[:500]
        raw_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        # 🔴 ``twofa_enroll_error`` must be **cleared** when a secret lands.
        # The old shape (``CASE WHEN ? <> '' THEN ? ELSE twofa_enroll_error``
        # keyed on ``error``) left the previous failure text in the column while
        # ``raw_json`` got ``""`` -- so a successfully enrolled account still
        # read as failed, and any later "which accounts lack 2FA" query that
        # consults this column would mis-count it.  Measured 2026-09-21: two
        # freshly enrolled accounts kept a 409 ``invalid_state`` string from an
        # earlier attempt.
        conn.execute(
            """
            UPDATE accounts
            SET totp_secret = CASE WHEN ? <> '' THEN ? ELSE totp_secret END,
                twofa_enrolled_at = CASE WHEN ? <> '' THEN ? ELSE twofa_enrolled_at END,
                twofa_enroll_error = CASE WHEN ? <> '' THEN ''
                                          WHEN ? <> '' THEN ?
                                          ELSE twofa_enroll_error END,
                updated_at = ?,
                raw_json = ?
            WHERE email = ?
            """,
            (
                secret, secret,
                secret, now,
                secret, error, error[:500],
                now, raw_json, lookup,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    if json_path:
        try:
            _update_session_json(json_path, data)
        except Exception:
            pass
    return True


# --------------------------------------------------------------- targets ---


def load_targets(scope: str, *, retry_terminal: bool = False) -> list[dict]:
    """Return the account rows to process, cheapest-first ordering.

    ``retry_terminal`` re-includes accounts whose last failure was a
    permanent one (dead mailbox / deleted account) -- see
    ``TERMINAL_ERROR_MARKERS``.  Off by default so a pass does not spend its
    first slots on a graveyard that will answer exactly the same way.
    """
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows: list[dict] = []
    for r in conn.execute(
        "SELECT email, access_token, device_id, mailbox_token, mailbox_provider,"
        " status, raw_json, json_path, totp_secret, twofa_enroll_error"
        " FROM accounts WHERE email LIKE '%@icloud.com'"
    ):
        try:
            data = json.loads(r["raw_json"] or "{}")
        except Exception:
            data = {}
        label = str(data.get("promotion_status") or "")
        # Read the column as well as the JSON.  ``upsert_account`` rebuilds
        # raw_json from a whitelist that does not contain ``totp_secret``, so
        # the column is the durable copy; JSON-only would re-attempt accounts
        # that are already done.  (Today both agree -- 925 both / 759 neither
        # / 0 column-only -- but that is a coincidence of history, not a
        # guarantee.)
        has_2fa = bool(
            str(r["totp_secret"] or "").strip()
            or str(data.get("totp_secret") or "").strip()
        )
        if scope == "no-trial" and label != PROMO_FREE:
            continue
        if scope in {"no-trial", "all-icloud"} and has_2fa:
            continue
        if not retry_terminal:
            last_error = str(r["twofa_enroll_error"] or "")
            if any(m in last_error for m in TERMINAL_ERROR_MARKERS):
                continue
        account = dict(data)
        account.update({
            "email": r["email"],
            "access_token": r["access_token"] or data.get("access_token") or "",
            "device_id": r["device_id"] or data.get("device_id") or "",
            "mailbox_token": r["mailbox_token"] or data.get("mailbox_token") or "",
            "mailbox_provider": r["mailbox_provider"] or data.get("mailbox_provider") or "",
            "json_path": r["json_path"] or data.get("json_path") or "",
        })
        account["_promo_label"] = label
        rows.append(account)
    conn.close()
    # Cheap-first: a live access token can skip the whole email-OTP lane.
    rows.sort(key=lambda a: (not str(a.get("access_token") or "").strip(), a["email"]))
    return rows


# ----------------------------------------------------------------- lanes ---


def _prime(session, chat_base: str, base_headers: dict) -> str:
    """GET / then /api/auth/csrf -- the same warm-up the recovery lane uses."""
    from sms_tool.auth_flow import _json_or_raw
    from sms_tool.auth_headers import auth_impersonate
    from sms_tool.http_client import request_with_retry

    request_with_retry(
        session, "get", f"{chat_base}/",
        label="2FA batch prime",
        headers={**base_headers, "Accept": "text/html,application/xhtml+xml"},
        impersonate=auth_impersonate(),
    )
    response = request_with_retry(
        session, "get", f"{chat_base}/api/auth/csrf",
        label="2FA batch csrf",
        headers={**base_headers, "Accept": "application/json", "Referer": f"{chat_base}/"},
        impersonate=auth_impersonate(),
    )
    return str(_json_or_raw(response).get("csrfToken") or "").strip()


def enroll_with_token(session, access_token: str, did: str, base_headers: dict,
                      journal: Journal, email: str) -> dict:
    """Enroll using an access token we already hold. No email OTP is spent.

    The secret is journaled between enroll and activate -- see module docstring.
    """
    from sms_tool.accounts.account_2fa import (
        _activate_totp, _enroll_totp, _mfa_info, _totp_already_enabled,
    )

    info = _mfa_info(session, access_token, did, base_headers)
    if _totp_already_enabled(info):
        return {"ok": True, "state": ALREADY_ENROLLED_MARKER}

    secret, session_id = _enroll_totp(session, access_token, did, base_headers)
    if not secret:
        return {"ok": False, "state": "enroll_no_secret", "error": "enroll_missing_secret"}
    journal.append(email, "enrolled_pending_activate", secret)

    try:
        _activate_totp(session, access_token, did, secret, session_id, base_headers)
    except Exception as exc:
        journal.append(email, "activate_failed_secret_kept", secret)
        return {
            "ok": False,
            "state": "activate_failed",
            "totp_secret": secret,
            "error": f"activate_failed: {exc}"[:500],
        }

    verified = _mfa_info(session, access_token, did, base_headers)
    journal.append(email, "activated", secret)
    return {
        "ok": True,
        "state": "enrolled",
        "totp_secret": secret,
        "mfa_enabled": bool(verified and verified.get("mfa_enabled")),
    }


def run_one(
    account: dict, journal: Journal, proxy_limit: int, explicit_proxy: str = "",
) -> dict:
    """Try, in order: held access token -> email-OTP re-login, across proxies.

    ``explicit_proxy`` pins the egress for this run only.  It is passed through
    as ``operation_proxy_candidates(explicit=...)``, which places it *first* in
    the candidate list, so with ``--proxy-limit 1`` it is the only egress used.
    Deliberately a CLI override rather than a ``proxy.json`` edit: the pool in
    that file is shared with the registration lane, and rewriting it to point
    at a local exit would silently re-route registrations too.
    """
    from curl_cffi import requests as curl_requests

    from sms_tool.accounts.account_2fa import _needs_reauth
    from sms_tool.accounts.account_creation import (
        _auth_session_access_token, _fetch_auth_session,
    )
    from sms_tool.accounts.account_recovery import _stored_registration_password
    from sms_tool.auth_flow import _login_existing_account_with_email_otp
    from sms_tool.auth_headers import (
        auth_impersonate, openai_auth_headers, select_auth_fingerprint,
    )
    from sms_tool.codex_oauth import _mailbox_from_data
    from sms_tool.config import CFG
    from sms_tool.proxy_routing import operation_proxy_candidates
    from sms_tool.session_refresh import _auth_session_email
    from sms_tool.sentinel_tokens import _set_oai_did_cookie

    email = str(account.get("email") or "").strip().lower()
    device_id = str(account.get("device_id") or "").strip() or str(uuid.uuid4())
    held_token = str(account.get("access_token") or "").strip()

    mailbox = _mailbox_from_data(account)
    if mailbox is None:
        return {"email": email, "ok": False, "state": "no_mailbox",
                "error": "missing_mailbox"}

    chat_cfg = CFG.get("chatgpt") if isinstance(CFG.get("chatgpt"), dict) else {}
    auth_base = str(chat_cfg.get("auth_base_url") or "https://auth.openai.com").rstrip("/")
    chat_base = str(chat_cfg.get("chat_base_url") or "https://chatgpt.com").rstrip("/")

    try:
        candidates = [c.proxy for c in operation_proxy_candidates(
            account, operation="liveness", config=CFG,
            explicit=explicit_proxy or None,
        )]
    except Exception:
        candidates = []
    if not candidates:
        candidates = [None]
    candidates = candidates[: max(1, proxy_limit)]

    last: dict = {"email": email, "ok": False, "state": "no_attempt",
                  "error": "no_proxy_attempted"}

    for proxy in candidates:
        try:
            select_auth_fingerprint(rotate=True)
            session = curl_requests.Session()
            if proxy:
                session.proxies = {"http": proxy, "https": proxy}
            _set_oai_did_cookie(session, device_id)
            base_headers = openai_auth_headers(
                device_id, accept="application/json", include_trace=True,
            )
            csrf_token = _prime(session, chat_base, base_headers)
            if not csrf_token:
                last = {"email": email, "ok": False, "state": "prime_failed",
                        "error": "missing_csrf_token"}
                continue

            # --- lane A: the token we already hold (no email cost) ---
            if held_token:
                try:
                    result = enroll_with_token(
                        session, held_token, device_id, base_headers, journal, email,
                    )
                    if result.get("ok"):
                        result["email"] = email
                        result["lane"] = "held_token"
                        return result
                    if not _needs_reauth(RuntimeError(str(result.get("error") or ""))):
                        last = {**result, "email": email, "lane": "held_token"}
                        # A non-reauth failure (e.g. 500) is worth one re-login try.
                        if result.get("state") != "activate_failed":
                            continue
                    else:
                        last = {**result, "email": email, "lane": "held_token"}
                except Exception as exc:
                    if not _needs_reauth(exc):
                        last = {"email": email, "ok": False, "state": "held_token_failed",
                                "error": str(exc)[:500]}
                        continue
                    last = {"email": email, "ok": False, "state": "needs_reauth",
                            "error": str(exc)[:300]}

            # --- lane B: full email-OTP re-login on the same session ---
            def reauth_login_fn() -> str:
                login = _login_existing_account_with_email_otp(
                    session=session,
                    username=email,
                    mailbox=mailbox,
                    did=device_id,
                    session_logging_id=str(uuid.uuid4()).replace("-", ""),
                    auth_base=auth_base,
                    chat_base=chat_base,
                    base_headers=base_headers,
                    csrf_token=csrf_token,
                    proxy=proxy,
                    totp_secret="",
                    password=_stored_registration_password(email),
                    allow_passwordless=True,
                )
                if not login.get("ok"):
                    raise RuntimeError(str(login.get("error") or "existing_login_failed"))
                auth_result = _fetch_auth_session(session, chat_base, base_headers)
                body = auth_result.get("body") if isinstance(auth_result.get("body"), dict) else {}
                fresh = str(_auth_session_access_token(body) or "").strip()
                if not fresh:
                    raise RuntimeError("auth_session_missing_access_token")
                authed = _auth_session_email(body)
                if authed and authed != email:
                    raise RuntimeError("auth_session_email_mismatch")
                return fresh

            fresh_token = reauth_login_fn()
            result = enroll_with_token(
                session, fresh_token, device_id, base_headers, journal, email,
            )
            result["email"] = email
            result["lane"] = "email_otp_relogin"
            if result.get("ok"):
                return result
            last = result
        except Exception as exc:
            last = {"email": email, "ok": False, "state": "exception",
                    "error": f"{type(exc).__name__}: {exc}"[:500]}
            continue

    return last


def _parse_retry_after(text: str) -> int:
    """Pull ``retry_after=<n>s`` out of a session-circuit error string.

    The circuit states its own wait (observed ``retry_after=300s``); returning
    0 means "not stated", so the caller falls back to the default rather than
    sleeping zero.
    """
    match = re.search(r"retry_after=(\d+)", text or "")
    if not match:
        return 0
    try:
        return max(1, int(match.group(1)))
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------ main ---


def _pool_rejects_credentials(explicit_proxy: str = "", timeout: float = 12.0) -> bool:
    """One cheap request to decide whether the pool still rejects us.

    Called after a transient cooldown, before resuming.  ``407`` is an
    *account-level* condition -- every sid shares the credential -- so a single
    sid answers for the whole pool.

    Measured 2026-09-21 run 3: a 10-streak of ``502`` tripped the transient
    breaker at 05:12, the cooldown ran, and the pool then came back as a hard
    ``407``.  Because the 407s were interleaved with other failures, the
    12-consecutive threshold took until 05:18 to fire -- roughly 3 minutes and
    81 accounts spent rediscovering something one request would have shown.

    ``explicit_proxy`` must be threaded through from ``--explicit-proxy``:
    probing the *configured* pool while the run is actually pinned to a local
    exit would report the (unrelated, dead) rola credentials as a 407 and abort
    a perfectly healthy run.

    Returns True only on a *definitive* rejection.  Anything else (success,
    timeout, 502) returns False, so a blip never triggers an abort.
    """
    try:
        from curl_cffi import requests as curl_requests

        from sms_tool.config import CFG
        from sms_tool.proxy_routing import operation_proxy_candidates

        candidates = [
            c.proxy
            for c in operation_proxy_candidates(
                None, operation="liveness", config=CFG,
                explicit=explicit_proxy or None,
            )
        ]
    except Exception:
        return False
    proxy = next((c for c in candidates if c), "")
    if not proxy:
        return False
    try:
        curl_requests.get(
            "https://www.cloudflare.com/cdn-cgi/trace",
            proxies={"http": proxy, "https": proxy},
            impersonate="chrome",
            timeout=timeout,
        )
        return False
    except Exception as exc:
        return POOL_DEAD_MARKER in str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch TOTP enrollment for saved accounts")
    parser.add_argument("--scope", default="no-trial",
                        choices=["no-trial", "all-icloud"])
    parser.add_argument("--limit", type=int, default=0, help="0 = no limit")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--proxy-limit", type=int, default=3,
                        help="max egress candidates tried per account")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--retry-terminal", action="store_true",
                        help="also attempt accounts whose last failure was "
                             "permanent (dead mailbox / deleted account); by "
                             "default they are skipped")
    parser.add_argument("--explicit-proxy", default="",
                        help="pin this run's egress to one proxy URL, ahead of "
                             "the configured pool (e.g. http://127.0.0.1:7897). "
                             "Pair with --proxy-limit 1 to use it exclusively. "
                             "Does NOT modify proxy.json.")
    parser.add_argument("--emails-file", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    journal = Journal(out_dir / f"2fa_journal_{stamp}.tsv")
    report_path = out_dir / f"2fa_enroll_report_{stamp}.jsonl"

    targets = load_targets(args.scope, retry_terminal=args.retry_terminal)
    if args.emails_file:
        wanted = {
            line.strip().lower()
            for line in Path(args.emails_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        targets = [a for a in targets if a["email"] in wanted]
    if args.limit:
        targets = targets[: args.limit]

    print(f"目标账号: {len(targets)}")
    print(f"journal : {journal.path}")
    print(f"report  : {report_path}")
    if args.dry_run:
        for a in targets[:20]:
            print(f"  {a['email']}  promo={a['_promo_label']}  "
                  f"at={'yes' if a.get('access_token') else 'NO'}  "
                  f"mailbox={'yes' if a.get('mailbox_token') else 'NO'}")
        print(f"  ... ({len(targets)} total)")
        return 0

    results: list[dict] = []
    done = 0
    started = time.time()
    lock = threading.Lock()
    # 🔴 Pool-death circuit breaker.
    #
    # Measured 2026-09-21: the rola registration pool answered
    # ``407 Proxy Authentication Required`` on every sid at once (an
    # account-level rejection, not a per-session one).  Every subsequent
    # account then failed **instantly**, so the run raced through the rest of
    # the list writing 380 meaningless failures -- each one burning a row that a
    # later pass has to re-attempt, and hammering a gateway that had already
    # said no.  A dead pool is not a per-account condition; it must stop the
    # batch.  Consecutive (not cumulative) so ordinary one-off transport noise
    # never trips it.
    dead_streak = 0
    transient_streak = 0
    cooldowns = 0
    session_waits = 0
    session_circuit_text = ""
    aborted = False
    executor = ThreadPoolExecutor(max_workers=max(1, args.workers))
    #: In-flight window.  Deliberately NOT "submit every target up front":
    #: with the whole list queued, a pause in the consumer loop does not
    #: actually stop new accounts from being spent -- the workers just pull the
    #: next one off the queue.  Bounding the window is what makes the transient
    #: cooldown below a real pause.
    window = max(1, args.workers) * 2
    target_iter = iter(targets)
    pending: dict = {}

    def _fill_window() -> None:
        while len(pending) < window:
            try:
                account = next(target_iter)
            except StopIteration:
                return
            pending[executor.submit(
                run_one, account, journal, args.proxy_limit, args.explicit_proxy,
            )] = account

    with open(report_path, "a", encoding="utf-8") as report:
        try:
            _fill_window()
            while pending:
                done_set, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                for future in done_set:
                    account = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {"email": account["email"], "ok": False,
                                  "state": "worker_crash",
                                  "error": traceback.format_exc()[-500:]}
                    result["ts"] = int(time.time())

                    secret = str(result.get("totp_secret") or "")
                    if result.get("ok") and secret:
                        try:
                            persist_twofa(account["email"], secret)
                            result["persisted"] = True
                        except Exception as exc:
                            result["persisted"] = False
                            result["persist_error"] = str(exc)[:300]
                    elif not result.get("ok") and result.get("error"):
                        try:
                            persist_twofa(account["email"], "", str(result["error"]))
                        except Exception:
                            pass
                    elif result.get("state") == ALREADY_ENROLLED_MARKER:
                        # ``ok=True`` with no secret -- both branches above are
                        # skipped, so nothing would be written and the account
                        # would be re-attempted forever.  Record the terminal
                        # marker so ``load_targets`` skips it and the export
                        # files it under "terminal" rather than "retryable".
                        try:
                            persist_twofa(account["email"], "", ALREADY_ENROLLED_MARKER)
                        except Exception:
                            pass

                    with lock:
                        results.append(result)
                        done += 1
                        report.write(json.dumps(result, ensure_ascii=False) + "\n")
                        report.flush()
                        if done % 10 == 0 or done == len(targets):
                            ok = sum(1 for r in results if r.get("ok"))
                            rate = done / max(1e-6, time.time() - started)
                            eta = (len(targets) - done) / max(1e-6, rate)
                            print(
                                f"[{done}/{len(targets)}] ok={ok} "
                                f"({rate:.2f}/s, ETA {eta/60:.1f} min)",
                                flush=True,
                            )

                    error_text = str(result.get("error") or "")
                    if POOL_DEAD_MARKER in error_text:
                        dead_streak += 1
                    else:
                        dead_streak = 0

                    if any(m in error_text for m in TRANSIENT_DEAD_MARKERS):
                        transient_streak += 1
                    else:
                        transient_streak = 0

                    # Remember the most recent session-circuit text so the
                    # post-batch pause can read its retry_after.
                    if SESSION_CIRCUIT_MARKER in error_text:
                        session_circuit_text = error_text

                    if dead_streak >= POOL_DEAD_STREAK_LIMIT:
                        aborted = True
                        print(
                            f"\n🔴 ABORT: {dead_streak} consecutive "
                            f"{POOL_DEAD_MARKER!r} failures -- the proxy pool is rejecting "
                            f"every session (account-level block, not per-account). "
                            f"Stopping after {done}/{len(targets)} so the remaining "
                            f"accounts are not spent against a dead gateway.",
                            flush=True,
                        )
                        break

                if aborted:
                    break

                # Transient transport outage (502 on CONNECT): the gateway is
                # up and authenticating us, the upstream tunnel just is not
                # establishing.  Measured 2026-09-21 04:42-04:51 -- ten minutes
                # later the same sids were 9/10 healthy again.  So pause here
                # instead of aborting: with the windowed submission above, this
                # genuinely stops new accounts from being spent.
                if transient_streak >= TRANSIENT_STREAK_LIMIT:
                    if cooldowns >= TRANSIENT_COOLDOWN_MAX:
                        aborted = True
                        print(
                            f"\n🔴 ABORT: {transient_streak} consecutive transport "
                            f"failures survived {cooldowns} x "
                            f"{TRANSIENT_COOLDOWN_SECONDS}s cooldowns -- this is no "
                            f"longer transient. Stopping after {done}/{len(targets)}.",
                            flush=True,
                        )
                        break
                    cooldowns += 1
                    print(
                        f"\n⏸ transport outage: {transient_streak} consecutive "
                        f"{TRANSIENT_DEAD_MARKERS} failures. Cooling down "
                        f"{TRANSIENT_COOLDOWN_SECONDS}s "
                        f"(cooldown {cooldowns}/{TRANSIENT_COOLDOWN_MAX}) at "
                        f"{done}/{len(targets)}.",
                        flush=True,
                    )
                    time.sleep(TRANSIENT_COOLDOWN_SECONDS)
                    transient_streak = 0
                    # Do not resume blind: an outage that started as 502 has
                    # been observed to come back as a hard 407.  One request
                    # now is far cheaper than spending accounts to find out.
                    if _pool_rejects_credentials(args.explicit_proxy):
                        aborted = True
                        print(
                            f"\n🔴 ABORT: post-cooldown probe got "
                            f"{POOL_DEAD_MARKER!r} -- the transient outage has "
                            f"turned into an account-level rejection. Stopping "
                            f"after {done}/{len(targets)}.",
                            flush=True,
                        )
                        break
                    print("   post-cooldown probe: pool answers again, resuming.",
                          flush=True)

                # Session-lane 429 circuit.  Not a streak condition: one hit
                # means the whole lane is shut, and every further account fails
                # instantly without being attempted.  Honour the circuit's own
                # retry_after instead of spending the list on instant failures.
                if SESSION_CIRCUIT_MARKER in session_circuit_text:
                    if session_waits >= SESSION_CIRCUIT_MAX_WAITS:
                        aborted = True
                        print(
                            f"\n🔴 ABORT: the session lane stayed circuit-open "
                            f"through {session_waits} x "
                            f"{SESSION_CIRCUIT_DEFAULT_WAIT}s waits. Stopping "
                            f"after {done}/{len(targets)}.",
                            flush=True,
                        )
                        break
                    pause_for = (
                        _parse_retry_after(session_circuit_text)
                        or SESSION_CIRCUIT_DEFAULT_WAIT
                    )
                    session_waits += 1
                    print(
                        f"\n⏸ session lane circuit open (429): sleeping "
                        f"{pause_for}s "
                        f"(wait {session_waits}/{SESSION_CIRCUIT_MAX_WAITS}) at "
                        f"{done}/{len(targets)}. Accounts seen during this "
                        f"window were NOT attempted.",
                        flush=True,
                    )
                    time.sleep(pause_for)
                    session_circuit_text = ""

                _fill_window()
        finally:
            # cancel_futures drops everything not yet started; the handful
            # already in flight are allowed to finish so their secrets still
            # reach the journal.
            executor.shutdown(wait=not aborted, cancel_futures=aborted)

    ok = sum(1 for r in results if r.get("ok"))
    states: dict[str, int] = {}
    for r in results:
        states[str(r.get("state"))] = states.get(str(r.get("state")), 0) + 1
    summary = {
        "total": len(results),
        "ok": ok,
        "failed": len(results) - ok,
        "aborted_pool_dead": aborted,
        "elapsed_seconds": round(time.time() - started, 1),
        "states": states,
        "journal": str(journal.path),
        "report": str(report_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    (out_dir / f"2fa_enroll_summary_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
