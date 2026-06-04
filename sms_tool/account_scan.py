"""Batch OAuth account scan helpers.

The scan is intentionally probe-only for phone verification: it detects an
OAuth add-phone challenge but never sends an SMS or consumes a phone number.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .codex_export import _openai_refresh_token, _refresh_with_openai_oauth
from .codex_oauth import collect_codex_oauth_tokens
from .session_refresh import _load_seed_session
from .storage import upsert_account


def scan_accounts(emails, session_file="", workers=4, proxy=None, timeout=120):
    emails = _unique_emails(emails)
    workers = max(1, min(int(workers or 1), 8, len(emails) or 1))
    print(f"[*] One-click account scan: {len(emails)} account(s), workers={workers}")

    ordered = [None] * len(emails)
    if workers <= 1:
        for index, email in enumerate(emails):
            ordered[index] = _scan_one(index, len(emails), email, session_file=session_file if len(emails) == 1 else "", proxy=proxy, timeout=timeout)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(_scan_one, index, len(emails), email, session_file=session_file if len(emails) == 1 else "", proxy=proxy, timeout=timeout)
                for index, email in enumerate(emails)
            ]
            for future in as_completed(futures):
                result = future.result()
                ordered[int(result.get("index", 0))] = result

    results = [r for r in ordered if r is not None]
    ok_count = sum(1 for r in results if r.get("ok"))
    deactivated_count = sum(1 for r in results if r.get("scan_status") == "account_deactivated")
    phone_required_count = sum(1 for r in results if r.get("phone_verification_required"))
    secondary_phone_count = sum(1 for r in results if r.get("secondary_phone_verification_required"))
    failed_count = len(results) - ok_count - deactivated_count - phone_required_count
    summary = {
        "ok": failed_count == 0,
        "total": len(results),
        "alive": ok_count,
        "account_deactivated": deactivated_count,
        "phone_verification_required": phone_required_count,
        "secondary_phone_verification_required": secondary_phone_count,
        "failed": max(0, failed_count),
        "results": [_public_scan_result(r) for r in results],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def _scan_one(index, total, email, session_file="", proxy=None, timeout=120):
    print(f"\n[{index + 1}/{total}] Account scan: {email}")
    started = time.time()
    data, json_path = _load_seed_session(email=email, session_file=session_file)
    data.setdefault("email", email)
    auth_session = data.get("auth_session") if isinstance(data.get("auth_session"), dict) else {}
    refresh_token = _openai_refresh_token(data, auth_session)
    had_rt = bool(refresh_token)
    had_phone = _has_verified_phone(data)
    refresh_result = {"ok": False, "mode": "none", "error": "missing_refresh_token"}

    if refresh_token:
        refresh_result = _refresh_with_openai_oauth(data, refresh_token, proxy=proxy)
        if refresh_result.get("ok"):
            data.update(refresh_result.get("data") or {})
            data["refresh_token_status"] = "oauth_present"
            data["refresh_token_updated_at"] = int(time.time())
            print(f"[OK] {email} OAuth RT refresh ok")
        elif _looks_account_deactivated(refresh_result):
            result = _result(index, email, "account_deactivated", False, had_rt, had_phone, refresh_result=refresh_result, started=started)
            _persist_scan(data, json_path, result)
            print(f"[DEACTIVATED] {email}")
            return result
        else:
            print(f"[!] {email} RT refresh failed: {refresh_result.get('error', 'unknown')}")

    oauth_result = collect_codex_oauth_tokens(
        data=data,
        proxy=proxy,
        timeout=timeout,
        force_email_otp_login=True,
        phone_pool=None,
        phone_probe_only=True,
    )

    if oauth_result.get("ok"):
        tokens = oauth_result.get("tokens") if isinstance(oauth_result.get("tokens"), dict) else {}
        if tokens:
            data["access_token"] = str(tokens.get("access_token") or data.get("access_token") or "").strip()
            data["id_token"] = str(tokens.get("id_token") or data.get("id_token") or "").strip()
            data["oauth_refresh_token"] = str(tokens.get("refresh_token") or data.get("oauth_refresh_token") or "").strip()
            data["refresh_token_status"] = "oauth_present" if data.get("oauth_refresh_token") else str(data.get("refresh_token_status") or "no_rt")
            data["refresh_token_updated_at"] = int(time.time())
        result = _result(index, email, "alive", True, had_rt or bool(data.get("oauth_refresh_token")), had_phone, refresh_result=refresh_result, oauth_result=oauth_result, started=started)
        _persist_scan(data, json_path, result)
        print(f"[OK] {email} alive")
        return result

    if _looks_account_deactivated(oauth_result):
        result = _result(index, email, "account_deactivated", False, had_rt, had_phone, refresh_result=refresh_result, oauth_result=oauth_result, started=started)
        _persist_scan(data, json_path, result)
        print(f"[DEACTIVATED] {email}")
        return result

    phone_required = _looks_phone_required(oauth_result)
    if phone_required:
        status = "secondary_phone_verification_required" if had_rt else "phone_verification_required"
        result = _result(
            index,
            email,
            status,
            False,
            had_rt,
            had_phone,
            refresh_result=refresh_result,
            oauth_result=oauth_result,
            phone_verification_required=True,
            secondary_phone_verification_required=had_rt,
            started=started,
        )
        _persist_scan(data, json_path, result)
        label = "SECONDARY_PHONE" if had_rt else "PHONE_REQUIRED"
        print(f"[{label}] {email}")
        return result

    # If RT refresh succeeded but the deeper OAuth probe could not complete due
    # to mailbox/session limitations, keep the account as alive but record that
    # the add-phone check was inconclusive.
    if refresh_result.get("ok"):
        result = _result(index, email, "alive_probe_inconclusive", True, had_rt, had_phone, refresh_result=refresh_result, oauth_result=oauth_result, started=started)
        _persist_scan(data, json_path, result)
        print(f"[OK] {email} alive; OAuth probe inconclusive: {oauth_result.get('error', 'unknown')}")
        return result

    result = _result(index, email, "scan_failed", False, had_rt, had_phone, refresh_result=refresh_result, oauth_result=oauth_result, started=started)
    _persist_scan(data, json_path, result)
    print(f"[FAIL] {email}: {oauth_result.get('error', refresh_result.get('error', 'unknown'))}")
    return result


def _persist_scan(data, json_path, result):
    now = int(time.time())
    updated = dict(data or {})
    updated["email"] = result.get("email") or updated.get("email", "")
    updated["account_scan_status"] = result.get("scan_status", "")
    updated["account_scan_updated_at"] = now
    updated["account_scan"] = _public_scan_result(result)

    status = result.get("scan_status")
    if status == "account_deactivated":
        updated["success"] = False
        updated["status"] = "account_deactivated"
        updated["error"] = "account_deactivated"
    elif result.get("secondary_phone_verification_required"):
        updated["status"] = "at_invalid"
        updated["error"] = "secondary_phone_verification_required:add_phone_required"
    elif result.get("phone_verification_required"):
        # No-RT accounts are expected to hit add-phone during the scan.  This
        # means "not yet SMS/RT verified", not AT invalidation.  Keep the paid
        # account visible as alive/paid unless the scan detected deactivation.
        updated["success"] = True
        if str(updated.get("error") or "").strip().lower() in {
            "account_deactivated",
            "account_deatived",
            "add_phone_required",
            "secondary_phone_verification_required:add_phone_required",
        }:
            updated.pop("error", None)
        if str(updated.get("status") or "").strip().lower() in {
            "account_deactivated",
            "account_deatived",
            "at_invalid",
            "access_token_invalid",
            "token_invalidated",
        }:
            updated["status"] = "registered"
    elif result.get("ok"):
        updated["success"] = True
        if updated.get("error") in {"account_deactivated", "add_phone_required", "secondary_phone_verification_required:add_phone_required"}:
            updated.pop("error", None)
        if str(updated.get("status") or "").strip().lower() in {"account_deactivated", "at_invalid", "access_token_invalid", "token_invalidated"}:
            updated["status"] = "registered"

    if json_path:
        try:
            Path(json_path).write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"[!] Failed to update session JSON {json_path}: {exc}")
    upsert_account(updated, json_path=json_path)


def _result(index, email, status, ok, had_rt, had_phone, refresh_result=None, oauth_result=None, phone_verification_required=False, secondary_phone_verification_required=False, started=0):
    return {
        "index": index,
        "email": email,
        "ok": bool(ok),
        "scan_status": status,
        "has_rt": bool(had_rt),
        "had_verified_phone": bool(had_phone),
        "phone_verification_required": bool(phone_verification_required),
        "secondary_phone_verification_required": bool(secondary_phone_verification_required),
        "refresh": _public_oauth_result(refresh_result or {}),
        "oauth": _public_oauth_result(oauth_result or {}),
        "elapsed_seconds": round(time.time() - started, 2) if started else 0,
    }


def _public_scan_result(result):
    output = dict(result or {})
    output.pop("index", None)
    output["refresh"] = _public_oauth_result(output.get("refresh") or {})
    output["oauth"] = _public_oauth_result(output.get("oauth") or {})
    return output


def _public_oauth_result(result):
    if not isinstance(result, dict):
        return {}
    output = {key: value for key, value in result.items() if key != "tokens"}
    tokens = result.get("tokens") if isinstance(result.get("tokens"), dict) else {}
    if tokens:
        output["has_access_token"] = bool(tokens.get("access_token"))
        output["has_refresh_token"] = bool(tokens.get("refresh_token"))
    body = output.get("body")
    if isinstance(body, str) and len(body) > 300:
        output["body"] = body[:300]
    return output


def _has_verified_phone(data):
    phone = str((data or {}).get("phone") or (data or {}).get("phone_number") or "").strip()
    response = (data or {}).get("response") if isinstance((data or {}).get("response"), dict) else {}
    phone_verification = response.get("phone_verification") if isinstance(response.get("phone_verification"), dict) else {}
    return bool(phone) or bool(phone_verification.get("ok") and phone_verification.get("phone"))


def _looks_phone_required(result):
    if not isinstance(result, dict):
        return False
    if result.get("phone_verification_required"):
        return True
    phone_attempt = result.get("phone_attempt") if isinstance(result.get("phone_attempt"), dict) else {}
    text = " ".join(str(value or "") for value in (
        result.get("error"),
        result.get("last_url"),
        phone_attempt.get("error"),
        phone_attempt.get("message"),
    )).lower()
    return "add_phone_required" in text or "phone_verification" in text or "/add-phone" in text


def _looks_account_deactivated(result):
    if not isinstance(result, dict):
        return False
    text = json.dumps(_public_oauth_result(result), ensure_ascii=False).lower()
    return (
        "account_deactivated" in text
        or "account_deatived" in text
        or "deleted or deactivated" in text
        or "account has been deleted" in text
        or "account has been deactivated" in text
    )


def _unique_emails(emails):
    output = []
    seen = set()
    for email in emails or []:
        value = str(email or "").strip().lower()
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output
