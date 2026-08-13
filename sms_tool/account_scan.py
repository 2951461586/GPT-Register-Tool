"""Batch OAuth account scan helpers.

The scan is intentionally probe-only for phone verification: it detects an
OAuth add-phone challenge but never sends an SMS or consumes a phone number.
"""

import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .codex_export import _openai_refresh_token, _refresh_with_openai_oauth
from .codex_oauth import collect_codex_oauth_tokens
from .account_liveness import probe_account_liveness
from .account_recovery import (
    is_permanently_deactivated,
    refresh_local_quota_statuses,
    relogin_codex_account,
)
from .http_client import is_transient_transport_error
from .session_refresh import _load_seed_session
from .storage import upsert_account
from .workspace_scan import inspect_workspace, parse_workspace_fallback_ids
from .error_classification import classify_error

_GMAIL_SCAN_LOCKS = {}
_GMAIL_SCAN_LOCKS_GUARD = threading.Lock()


def scan_accounts(
    emails,
    session_file="",
    workers=4,
    proxy=None,
    timeout=120,
    workspace_check=False,
    switch_workspace_id="",
    fallback_workspace_ids=None,
    auto_switch_workspace=False,
    quota_relogin_on_401=False,
    relogin_mode="auto",
):
    emails = _unique_emails(emails)
    workers = max(1, min(int(workers or 1), 8, len(emails) or 1))
    print(f"[*] One-click account scan: {len(emails)} account(s), workers={workers}")

    ordered = [None] * len(emails)
    if workers <= 1:
        for index, email in enumerate(emails):
            ordered[index] = _scan_one_with_lane(
                index,
                len(emails),
                email,
                session_file=session_file if len(emails) == 1 else "",
                proxy=proxy,
                timeout=timeout,
                workspace_check=workspace_check,
                switch_workspace_id=switch_workspace_id,
                fallback_workspace_ids=fallback_workspace_ids,
                auto_switch_workspace=auto_switch_workspace,
                quota_relogin_on_401=quota_relogin_on_401,
                relogin_mode=relogin_mode,
            )
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(
                    _scan_one_with_lane,
                    index,
                    len(emails),
                    email,
                    session_file=session_file if len(emails) == 1 else "",
                    proxy=proxy,
                    timeout=timeout,
                    workspace_check=workspace_check,
                    switch_workspace_id=switch_workspace_id,
                    fallback_workspace_ids=fallback_workspace_ids,
                    auto_switch_workspace=auto_switch_workspace,
                    quota_relogin_on_401=quota_relogin_on_401,
                    relogin_mode=relogin_mode,
                )
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
    workspace_deactivated_count = sum(
        1 for r in results if ((r.get("workspace") or {}).get("status") == "workspace_deactivated")
    )
    workspace_switched_count = sum(
        1 for r in results if ((r.get("workspace") or {}).get("status") == "workspace_switched")
    )
    workspace_free_fallback_count = sum(
        1 for r in results if ((r.get("workspace") or {}).get("status") == "workspace_fallback_free")
    )
    workspace_inconclusive_count = sum(
        1 for r in results if ((r.get("workspace") or {}).get("status") == "workspace_check_inconclusive")
    )
    failed_count = len(results) - ok_count - deactivated_count - phone_required_count
    at_invalid_count = sum(1 for r in results if str(r.get("scan_status") or r.get("status") or "").lower() in {"at_invalid", "access_token_invalid", "token_invalidated"})
    summary = {
        "ok": failed_count == 0,
        "total": len(results),
        "alive": ok_count,
        "account_deactivated": deactivated_count,
        "at_invalid": at_invalid_count,
        "phone_verification_required": phone_required_count,
        "secondary_phone_verification_required": secondary_phone_count,
        "workspace_deactivated": workspace_deactivated_count,
        "workspace_switched": workspace_switched_count,
        "workspace_fallback_free": workspace_free_fallback_count,
        "workspace_inconclusive": workspace_inconclusive_count,
        "failed": max(0, failed_count),
        "results": [_public_scan_result(r) for r in results],
        "overview": [_scan_overview(r) for r in results],
    }
    quota_refresh = _refresh_quota_after_scan(
        results,
        workers=workers,
        timeout=timeout,
        proxy=proxy,
        relogin_on_401=quota_relogin_on_401,
        relogin_mode=relogin_mode,
    )
    if quota_refresh:
        summary["quota_refresh"] = quota_refresh
    _print_scan_overview(results)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def _scan_one(
    index,
    total,
    email,
    session_file="",
    proxy=None,
    timeout=120,
    workspace_check=False,
    switch_workspace_id="",
    fallback_workspace_ids=None,
    auto_switch_workspace=False,
    quota_relogin_on_401=False,
    relogin_mode="auto",
):
    print(f"\n[{index + 1}/{total}] Account scan: {email}")
    started = time.time()
    data, json_path = _load_seed_session(email=email, session_file=session_file)
    data.setdefault("email", email)

    if is_permanently_deactivated(data):
        result = _result(
            index,
            email,
            "account_deactivated",
            False,
            bool(_openai_refresh_token(data, data.get("auth_session") or {})),
            _has_verified_phone(data),
            started=started,
        )
        result["terminal"] = True
        result["skipped_relogin"] = True
        _persist_scan(data, json_path, result)
        print(f"[DEACTIVATED] {email}; terminal account skipped")
        return result

    token_probe = _probe_existing_access_token(data, proxy=proxy, timeout=timeout)
    auth_session = data.get("auth_session") if isinstance(data.get("auth_session"), dict) else {}
    refresh_token = _openai_refresh_token(data, auth_session)
    had_rt = bool(refresh_token)
    had_phone = _has_verified_phone(data)
    refresh_result = {"ok": False, "mode": "none", "error": "missing_refresh_token"}
    relogin_result = {}

    recovering_401 = quota_relogin_on_401 and _token_probe_is_invalid(token_probe)
    if refresh_token and not recovering_401:
        refresh_result = _refresh_with_openai_oauth(data, refresh_token, proxy=proxy)
        if refresh_result.get("ok"):
            data.update(refresh_result.get("data") or {})
            data["refresh_token_status"] = "oauth_present"
            data["refresh_token_updated_at"] = int(time.time())
            print(f"[OK] {email} OAuth RT refresh ok")
        elif _looks_account_deactivated(refresh_result):
            result = _result(
                index,
                email,
                "account_deactivated",
                False,
                had_rt,
                had_phone,
                refresh_result=refresh_result,
                token_probe=token_probe,
                started=started,
            )
            _persist_scan(data, json_path, result)
            print(f"[DEACTIVATED] {email}")
            return result
        else:
            print(f"[!] {email} RT refresh failed: {refresh_result.get('error', 'unknown')}")

    if recovering_401:
        relogin_result, data, json_path, token_probe = _attempt_scan_relogin(
            email=email,
            data=data,
            json_path=json_path,
            proxy=proxy,
            timeout=timeout,
            fallback_probe=token_probe,
            relogin_mode=relogin_mode,
        )
        if relogin_result.get("ok"):
            had_rt = had_rt or bool(str(data.get("oauth_refresh_token") or "").strip())
        else:
            print(f"[!] {email} relogin failed: {_oauth_error(relogin_result)}")

    workspace_result = _workspace_probe(
        data,
        proxy=proxy,
        timeout=timeout,
        enabled=workspace_check,
        switch_workspace_id=switch_workspace_id,
        fallback_workspace_ids=fallback_workspace_ids,
        auto_switch_workspace=auto_switch_workspace,
    )

    if relogin_result:
        oauth_result = relogin_result
    else:
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
            data["refresh_token_status"] = "oauth_present" if data.get("oauth_refresh_token") else str(
                data.get("refresh_token_status") or "no_rt"
            )
            data["refresh_token_updated_at"] = int(time.time())
        result = _result(
            index,
            email,
            "alive",
            True,
            had_rt or bool(data.get("oauth_refresh_token")),
            had_phone,
            refresh_result=refresh_result,
            oauth_result=oauth_result,
            relogin_result=relogin_result,
            token_probe=token_probe,
            started=started,
            workspace_result=workspace_result,
        )
        result["subscription_type"] = _subscription_type(data)
        result["at_status"] = _at_status_label(result, refresh_result, oauth_result)
        result["phone_verification_required_label"] = "是" if result.get("phone_verification_required") else "否"
        result["dropped"] = "否"
        _persist_scan(data, json_path, result)
        print(f"[OK] {email} alive")
        return result

    if _looks_account_deactivated(oauth_result):
        result = _result(
            index,
            email,
            "account_deactivated",
            False,
            had_rt,
            had_phone,
            refresh_result=refresh_result,
            oauth_result=oauth_result,
            relogin_result=relogin_result,
            token_probe=token_probe,
            started=started,
            workspace_result=workspace_result,
        )
        result["subscription_type"] = _subscription_type(data)
        result["at_status"] = _at_status_label(result, refresh_result, oauth_result)
        result["phone_verification_required_label"] = "是" if result.get("phone_verification_required") else "否"
        result["dropped"] = "是"
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
            relogin_result=relogin_result,
            token_probe=token_probe,
            phone_verification_required=True,
            secondary_phone_verification_required=had_rt,
            started=started,
            workspace_result=workspace_result,
        )
        result["subscription_type"] = _subscription_type(data)
        result["at_status"] = _at_status_label(result, refresh_result, oauth_result)
        result["phone_verification_required_label"] = "是"
        result["dropped"] = "否"
        _persist_scan(data, json_path, result)
        label = "SECONDARY_PHONE" if had_rt else "PHONE_REQUIRED"
        print(f"[{label}] {email}")
        return result

    # If the account still has a usable AT (either from RT refresh or from the
    # existing local AT probe), do not downgrade it to a failed scan merely
    # because the deeper auth/add-phone probe could not finish.
    if refresh_result.get("ok") or _token_probe_is_active(token_probe):
        result = _result(
            index,
            email,
            "alive_probe_inconclusive",
            True,
            had_rt,
            had_phone,
            refresh_result=refresh_result,
            oauth_result=oauth_result,
            relogin_result=relogin_result,
            token_probe=token_probe,
            started=started,
            workspace_result=workspace_result,
        )
        result["subscription_type"] = _subscription_type(data)
        result["at_status"] = _at_status_label(result, refresh_result, oauth_result)
        result["phone_verification_required_label"] = "是" if result.get("phone_verification_required") else "否"
        result["dropped"] = "否"
        _persist_scan(data, json_path, result)
        print(f"[OK] {email} alive; OAuth probe inconclusive: {_oauth_error(oauth_result)}")
        return result

    failure_class = classify_error(relogin_result or oauth_result or refresh_result)
    if failure_class == "network":
        failure_status = "network_failed"
    else:
        failure_status = "relogin_failed" if relogin_result and not relogin_result.get("ok") else "scan_failed"
    result = _result(
        index,
        email,
        failure_status,
        False,
        had_rt,
        had_phone,
        refresh_result=refresh_result,
        oauth_result=oauth_result,
        relogin_result=relogin_result,
        token_probe=token_probe,
        started=started,
        workspace_result=workspace_result,
    )
    result["subscription_type"] = _subscription_type(data)
    result["failure_class"] = failure_class
    result["at_status"] = _at_status_label(result, refresh_result, oauth_result)
    result["phone_verification_required_label"] = "是" if result.get("phone_verification_required") else "否"
    result["dropped"] = "否"
    _persist_scan(data, json_path, result)
    print(f"[FAIL] {email}: {_oauth_error(relogin_result or oauth_result or refresh_result)}")
    return result


def _persist_scan(data, json_path, result):
    now = int(time.time())
    updated = dict(data or {})
    updated["email"] = result.get("email") or updated.get("email", "")
    updated["account_scan_status"] = result.get("scan_status", "")
    updated["account_scan_updated_at"] = now
    updated["account_scan"] = _public_scan_result(result)
    updated["account_scan_overview"] = _scan_overview(result)
    workspace = result.get("workspace") if isinstance(result.get("workspace"), dict) else {}
    if workspace:
        is_free_workspace = str(workspace.get("account_type_after") or "").strip().lower() == "free"
        updated["workspace_scan"] = workspace
        updated["workspace_status"] = str(workspace.get("status") or "")
        updated["workspace_id"] = "" if is_free_workspace else str(workspace.get("actual_workspace_id") or "")
        updated["workspace_name"] = "" if is_free_workspace else str(workspace.get("workspace_name") or workspace.get("actual_workspace_name") or "")
        updated["workspace_switch_result"] = str(workspace.get("switch_status") or workspace.get("switch_error") or "")
        updated["workspace_updated_at"] = now
        if workspace.get("account_type_after"):
            updated["account_type"] = str(workspace.get("account_type_after") or "")

    status = result.get("scan_status")
    if status == "account_deactivated":
        updated["success"] = False
        updated["status"] = "account_deactivated"
        updated["error"] = "account_deactivated"
    elif result.get("secondary_phone_verification_required"):
        updated["status"] = "at_invalid"
        updated["error"] = "secondary_phone_verification_required:add_phone_required"
    elif result.get("phone_verification_required"):
        # No-RT accounts are expected to hit add-phone during the scan. This
        # means "not yet SMS/RT verified", not AT invalidation. Keep the paid
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
        if updated.get("error") in {
            "account_deactivated",
            "add_phone_required",
            "secondary_phone_verification_required:add_phone_required",
        }:
            updated.pop("error", None)
        if str(updated.get("status") or "").strip().lower() in {
            "account_deactivated",
            "at_invalid",
            "access_token_invalid",
            "token_invalidated",
        }:
            updated["status"] = "registered"
    elif status in {"relogin_failed", "scan_failed"} and _token_probe_is_invalid((result or {}).get("token_probe") or {}):
        updated["success"] = False
        updated["status"] = "at_invalid"
        updated["error"] = _oauth_error(
            (result or {}).get("relogin")
            or (result or {}).get("oauth")
            or (result or {}).get("refresh")
            or {}
        )

    if json_path:
        try:
            Path(json_path).write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"[!] Failed to update session JSON {json_path}: {exc}")
    upsert_account(updated, json_path=json_path)


def _result(
    index,
    email,
    status,
    ok,
    had_rt,
    had_phone,
    refresh_result=None,
    oauth_result=None,
    relogin_result=None,
    token_probe=None,
    phone_verification_required=False,
    secondary_phone_verification_required=False,
    started=0,
    workspace_result=None,
):
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
        **({"relogin": _public_oauth_result(relogin_result or {})} if relogin_result else {}),
        **({"token_probe": _public_probe_result(token_probe or {})} if token_probe else {}),
        "workspace": workspace_result or {},
        "elapsed_seconds": round(time.time() - started, 2) if started else 0,
    }


def _scan_overview(result):
    dropped_value = str((result or {}).get("dropped") or "").strip().lower()
    dropped = (
        str((result or {}).get("scan_status") or "").strip() == "account_deactivated"
        or dropped_value in {"是", "yes", "true", "1"}
    )
    return {
        "email": str((result or {}).get("email") or "").strip(),
        "at_status": _at_status_label(result or {}, (result or {}).get("refresh") or {}, (result or {}).get("oauth") or {}),
        "phone_verification_required": "是" if bool((result or {}).get("phone_verification_required")) else "否",
        "subscription_type": _subscription_type(result or {}),
        "dropped": "是" if dropped else "否",
    }


def _print_scan_overview(results):
    if not results:
        return
    print("[*] 扫号概览:")
    for index, result in enumerate(results, 1):
        overview = _scan_overview(result)
        print(
            f"{index}. {overview['email']} | "
            f"AT: {overview['at_status']} | "
            f"需要手机号验证: {overview['phone_verification_required']} | "
            f"订阅类型: {overview['subscription_type']} | "
            f"已掉号: {overview['dropped']}"
        )


def _public_scan_result(result):
    output = dict(result or {})
    output.pop("index", None)
    output["refresh"] = _public_oauth_result(output.get("refresh") or {})
    output["oauth"] = _public_oauth_result(output.get("oauth") or {})
    if "relogin" in output:
        output["relogin"] = _public_oauth_result(output.get("relogin") or {})
    if "token_probe" in output:
        output["token_probe"] = _public_probe_result(output.get("token_probe") or {})
    return output


def _subscription_type(data):
    candidates = [
        (data or {}).get("subscription_type"),
        _nested_value(data, "planType"),
        _nested_value(data, "plan_type"),
        _nested_value(data, "account", "planType"),
        _nested_value(data, "account", "plan_type"),
        _nested_value(data, "auth_session", "account", "planType"),
        _nested_value(data, "auth_session", "account", "plan_type"),
        _jwt_plan_type(str((data or {}).get("access_token") or "")),
    ]
    for value in candidates:
        normalized = _normalize_subscription_type(value)
        if normalized:
            return normalized
    return "free"


def _normalize_subscription_type(value):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "team" in text or "business" in text:
        return "team"
    if "plus" in text:
        return "plus"
    if "free" in text:
        return "free"
    return ""


def _nested_value(data, *keys):
    node = data
    for key in keys:
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
    return node


def _jwt_plan_type(token):
    try:
        parts = token.split(".")
        if len(parts) < 2 or not parts[1]:
            return ""
        payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        body = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return ""
    auth = body.get("https://api.openai.com/auth") if isinstance(body, dict) else {}
    if isinstance(auth, dict):
        return auth.get("chatgpt_plan_type") or auth.get("plan_type") or ""
    return ""


def _at_status_label(result, refresh_result=None, oauth_result=None):
    refresh_ok = bool((refresh_result or {}).get("ok"))
    oauth_ok = bool((oauth_result or {}).get("ok"))
    token_probe = (result or {}).get("token_probe") if isinstance((result or {}).get("token_probe"), dict) else {}
    scan_status = str((result or {}).get("scan_status") or "").strip()
    if scan_status == "account_deactivated":
        return "AT失效"
    if oauth_ok and not refresh_ok:
        return "AT失效已刷新"
    if refresh_ok or bool((result or {}).get("ok")) or _token_probe_is_active(token_probe):
        return "AT有效"
    return "AT失效"


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


def _public_probe_result(result):
    if not isinstance(result, dict):
        return {}
    output = dict(result)
    for key in ("body", "raw", "error"):
        value = output.get(key)
        if isinstance(value, str) and len(value) > 300:
            output[key] = value[:300]
    return output


def _scan_one_with_lane(*args, **kwargs):
    email = args[2] if len(args) >= 3 else kwargs.get("email", "")
    lane = _gmail_scan_lane_key(email)
    if not lane:
        return _scan_one(*args, **kwargs)
    with _GMAIL_SCAN_LOCKS_GUARD:
        lock = _GMAIL_SCAN_LOCKS.setdefault(lane, threading.Lock())
    with lock:
        return _scan_one(*args, **kwargs)


def _gmail_scan_lane_key(email):
    value = str(email or "").strip().lower()
    if "@" not in value:
        return ""
    local, domain = value.rsplit("@", 1)
    if domain not in {"gmail.com", "googlemail.com"}:
        return ""
    return value if local else ""


def _workspace_probe(data, proxy=None, timeout=120, enabled=True, switch_workspace_id="", fallback_workspace_ids=None, auto_switch_workspace=False):
    fallback_ids = fallback_workspace_ids
    if isinstance(fallback_ids, str):
        fallback_ids = parse_workspace_fallback_ids(fallback_ids)
    try:
        result = inspect_workspace(
            data,
            proxy=proxy,
            timeout=min(max(int(timeout or 30), 10), 120),
            enabled=enabled,
            target_workspace_id=switch_workspace_id,
            fallback_workspace_ids=fallback_ids or [],
            auto_switch=auto_switch_workspace,
        )
        status = str(result.get("status") or "")
        if status and status != "workspace_check_disabled":
            print(
                f"[*] Workspace scan: {status}"
                f" current={str(result.get('actual_workspace_id') or result.get('token_workspace_id') or '')[:8]}"
                f" target={str(result.get('target_workspace_id') or '')[:8]}"
            )
        return result
    except Exception as exc:
        error = str(exc)[:300]
        if is_transient_transport_error(exc):
            return {"ok": False, "status": "workspace_check_inconclusive", "error": error, "inconclusive": True}
        return {"ok": False, "status": "workspace_check_failed", "error": error}


def _refresh_quota_after_scan(results, workers=4, timeout=120, proxy=None, relogin_on_401=False, relogin_mode="auto"):
    emails = [
        str((result or {}).get("email") or "").strip()
        for result in results or []
        if str((result or {}).get("email") or "").strip()
    ]
    if not emails:
        return {}
    try:
        quota = refresh_local_quota_statuses(
            emails=emails,
            workers=max(1, min(int(workers or 1), 8)),
            proxy=proxy,
            timeout=min(max(int(timeout or 30), 5), 60),
            relogin_on_401=bool(relogin_on_401),
            relogin_timeout=max(int(timeout or 120), 60),
            relogin_mode=relogin_mode,
        )
        if quota.get("total", 0):
            print(f"[*] Local quota refreshed: {quota.get('success', 0)}/{quota.get('total', 0)}")
        elif quota.get("error"):
            print(f"[*] Local quota refresh skipped: {quota.get('error')}")
        return quota
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


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
    text = " ".join(
        str(value or "")
        for value in (
            result.get("error"),
            result.get("last_url"),
            phone_attempt.get("error"),
            phone_attempt.get("message"),
        )
    ).lower()
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


def _probe_existing_access_token(data, proxy=None, timeout=120):
    if not str((data or {}).get("access_token") or "").strip():
        return {}
    try:
        return probe_account_liveness(
            data,
            proxy=proxy,
            timeout=min(max(int(timeout or 30), 5), 45),
        )
    except Exception as exc:
        return {
            "ok": False,
            "mode": "local",
            "status": "unknown",
            "quota_status": "探测失败",
            "error": str(exc)[:300],
        }


def _token_probe_is_active(result):
    return str((result or {}).get("status") or "").strip().lower() == "active"


def _token_probe_is_invalid(result):
    return str((result or {}).get("status") or "").strip().lower() == "token_invalid"


def _attempt_scan_relogin(email, data, json_path="", proxy=None, timeout=120, fallback_probe=None, relogin_mode="auto"):
    account = dict(data or {})
    account["email"] = email
    if json_path:
        account["json_path"] = json_path
    relogin_mode = _normalize_relogin_mode(relogin_mode)
    print(f"[*] {email} AT invalid; trying the configured AT recovery chain...")
    relogin = relogin_codex_account(
        account,
        proxy=proxy,
        timeout=max(int(timeout or 120), 60),
        mode=relogin_mode,
    )
    if relogin.get("ok"):
        refreshed, refreshed_json_path = _load_seed_session(email=email, session_file=json_path or "")
        refreshed = dict(refreshed or {})
        refreshed.setdefault("email", email)
        if str(refreshed.get("access_token") or "").strip():
            refreshed_probe = dict(relogin.get("probe") or {})
            return relogin, refreshed, (refreshed_json_path or json_path), refreshed_probe
        relogin = dict(relogin)
        relogin["ok"] = False
        relogin["error"] = "relogin_persist_missing_access_token"
    return relogin, data, json_path, (fallback_probe or {})


def _normalize_relogin_mode(value):
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"web", "web_session", "session", "chatgpt_session"}:
        return "web_session"
    if text in {"codex", "codex_oauth", "oauth", "pkce"}:
        return "codex_oauth"
    return "auto"


def _oauth_error(result):
    if not isinstance(result, dict):
        return str(result or "unknown")
    phone_attempt = result.get("phone_attempt") if isinstance(result.get("phone_attempt"), dict) else {}
    for value in (
        result.get("error"),
        phone_attempt.get("error"),
        phone_attempt.get("message"),
        result.get("message"),
        result.get("last_url"),
    ):
        text = str(value or "").strip()
        if text:
            return text[:300]
    body = result.get("body")
    if isinstance(body, dict):
        return json.dumps(body, ensure_ascii=False)[:300]
    if isinstance(body, str) and body.strip():
        return body.strip()[:300]
    return "unknown"


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
