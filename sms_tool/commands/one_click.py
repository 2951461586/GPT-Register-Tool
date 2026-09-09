"""CLI boundary for one-click account commands (--one-click-sms, --one-click-scan).

``codex_oauth``/``account_scan``/``phone_reuse`` own protocol behavior; this
module only resolves targets from argparse values and orchestrates workers.
``OneClickCommandContext`` keeps the legacy CLI's replaceable hooks explicit.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .helpers import public_oauth_result, read_email_file, unique_emails
from ..sanitizer import account_reference

logger = logging.getLogger(__name__)


def _emit_one_click_event(
    *,
    run_id: str,
    batch_id: str,
    stage: str,
    status: str = "running",
    email: str = "",
    detail: str = "",
    total: int = 0,
    account_terminal: bool = False,
    batch_terminal: bool = False,
    failure_class: str = "",
) -> None:
    """Publish desktop-only one-click progress without changing CLI output."""
    try:
        from ..desktop_ipc import emit_event

        payload = {
            "domain": "one_click_sms",
            "operation": "one_click_sms",
            "run_id": run_id,
            "batch_id": batch_id,
            "account_ref": account_reference(email),
            "stage": stage,
            "status": status,
            "detail": detail,
            "total": total,
            "account_terminal": account_terminal,
            "batch_terminal": batch_terminal,
        }
        if failure_class:
            payload["failure_class"] = str(failure_class)[:80]
        emit_event(payload)
    except (OSError, RuntimeError, TypeError, ValueError):
        # Progress is observational and must never change the OAuth outcome.
        return


@dataclass(frozen=True)
class OneClickCommandContext:
    """Legacy CLI hooks required by one-click command orchestration."""

    load_mailbox_pool: Callable[[Any], list[Any]]
    max_reuse: Callable[[Any], int]
    mailbox_snapshot: Callable[[Any], dict[str, Any]]
    persist_failure: Callable[..., None]
    upsert_account: Callable[..., Any]


def one_click_sms(args: Any, ctx: OneClickCommandContext) -> None:
    """Refresh selected account(s) through Codex OAuth and phone SMS, then store RT."""
    from ..codex_oauth import refresh_codex_oauth_session
    from ..phone_reuse import create_phone_pool, print_phone_pool_status
    from ..session_refresh import _load_seed_session

    emails = read_email_file(args.email_file)
    if args.email:
        emails = [(args.email or "").strip()]
    if not emails and args.session_file:
        seed, _ = _load_seed_session(session_file=args.session_file)
        if seed.get("email"):
            emails = [str(seed.get("email") or "").strip()]
    emails = unique_emails(emails)
    if not emails:
        print("[Error] --email, --email-file, or --session-file is required with --one-click-sms")
        raise SystemExit(2)

    explicit_mailboxes = {}
    if getattr(args, "chatai_mailbox_file", None) or getattr(args, "mailbox_file", None):
        explicit_mailboxes = {
            str(getattr(mailbox, "email", "") or "").strip().lower(): mailbox
            for mailbox in ctx.load_mailbox_pool(args)
            if str(getattr(mailbox, "email", "") or "").strip()
        }

    one_click_max_reuse = ctx.max_reuse(args)
    phone_pool = create_phone_pool(
        max_reuse_count=one_click_max_reuse,
        send_cooldown_seconds=args.phone_send_cooldown,
        source_override=args.phone_source,
    )
    if not phone_pool.phones:
        print("[Error] --one-click-sms requires a phone pool. Configure phone_reuse.smsbower.api_key/SMSBOWER_API_KEY or phone_reuse.phone_pool.")
        raise SystemExit(2)
    phone_pool.reset_exhausted_smsbower_slots()
    print_phone_pool_status(phone_pool)
    if phone_pool.total_capacity <= 0:
        print("[Error] --one-click-sms requires at least one available phone slot; current phone pool is exhausted.")
        raise SystemExit(2)

    workers = max(1, min(int(args.workers or 1), 4, len(emails)))
    batch_id = uuid.uuid4().hex
    run_id = batch_id
    _emit_one_click_event(
        run_id=run_id,
        batch_id=batch_id,
        stage="batch_started",
        total=len(emails),
        detail="一键接码开始",
    )
    print(f"[*] One-click SMS RT refresh: {len(emails)} account(s), workers={workers}")
    logger.info("one-click SMS RT refresh started: accounts=%d workers=%d", len(emails), workers)

    def _run_one(index, email):
        print(f"\n[{index + 1}/{len(emails)}] One-click SMS: {email}")
        account_run_id = f"{batch_id}:{account_reference(email)}"
        _emit_one_click_event(
            run_id=account_run_id,
            batch_id=batch_id,
            stage="session_load",
            email=email,
            detail="读取账号会话",
        )
        data, json_path = _load_seed_session(
            email=email,
            session_file=args.session_file if len(emails) == 1 else "",
        )
        data.setdefault("email", email)
        mailbox = explicit_mailboxes.get(email.strip().lower())
        if mailbox is not None:
            data["mailbox"] = ctx.mailbox_snapshot(mailbox)
        _emit_one_click_event(
            run_id=account_run_id,
            batch_id=batch_id,
            stage="oauth_refresh",
            email=email,
            detail="执行 OAuth 与邮箱验证码流程",
        )
        result = refresh_codex_oauth_session(
            data,
            json_path=json_path,
            proxy=args.proxy,
            timeout=args.refresh_timeout,
            force_email_otp_login=True,
            phone_pool=phone_pool,
        )
        if result.get("ok"):
            phone = str(result.get("phone") or "").strip()
            phone_suffix = f" phone={phone}" if phone else ""
            print(f"[OK] {email} RT stored: {result.get('refresh_token_status', '')}{phone_suffix}")
            logger.info("one-click SMS %s: RT stored (%s)", email, result.get("refresh_token_status", ""))
            _emit_one_click_event(
                run_id=account_run_id,
                batch_id=batch_id,
                stage="completed",
                status="success",
                email=email,
                detail="RT 已保存",
                account_terminal=True,
            )
        else:
            print(f"[FAIL] {email}: {result.get('error', 'unknown')}")
            logger.warning("one-click SMS %s failed: %s", email, result.get("error", "unknown"))
            ctx.persist_failure(data, json_path, email, result)
            _emit_one_click_event(
                run_id=account_run_id,
                batch_id=batch_id,
                stage="failed",
                status="failed",
                email=email,
                detail=str(result.get("error") or "一键接码失败")[:160],
                account_terminal=True,
                failure_class=str(result.get("failure_class") or "unknown"),
            )
        result.setdefault("email", email)
        return index, result

    ordered = [None] * len(emails)
    if workers <= 1:
        for index, email in enumerate(emails):
            i, result = _run_one(index, email)
            ordered[i] = result
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_run_one, i, email) for i, email in enumerate(emails)]
            for future in as_completed(futures):
                i, result = future.result()
                ordered[i] = result

    results = [result for result in ordered if result is not None]
    ok_count = sum(1 for result in results if result.get("ok"))
    logger.info("one-click SMS RT refresh finished: ok=%d/%d", ok_count, len(emails))
    summary = {
        "ok": ok_count == len(emails),
        "total": len(emails),
        "success": ok_count,
        "failed": len(emails) - ok_count,
        "results": results,
    }
    _emit_one_click_event(
        run_id=run_id,
        batch_id=batch_id,
        stage="batch_completed",
        status="completed" if ok_count == len(emails) else "failed",
        detail=f"完成 {ok_count}/{len(emails)}",
        total=len(emails),
        batch_terminal=True,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if ok_count != len(emails):
        raise SystemExit(3)


def one_click_scan(args: Any) -> None:
    """Batch OAuth probe accounts without sending SMS."""
    from ..accounts.account_scan import scan_accounts
    from ..session_refresh import _load_seed_session
    from ..storage import list_paypal_accounts

    emails = read_email_file(args.email_file)
    if args.email:
        emails = [(args.email or "").strip()]
    if not emails and args.session_file:
        seed, _ = _load_seed_session(session_file=args.session_file)
        if seed.get("email"):
            emails = [str(seed.get("email") or "").strip()]
    if not emails:
        emails = [str(row.get("email") or "").strip() for row in list_paypal_accounts()]
    emails = unique_emails(emails)
    if not emails:
        print("[Error] no account email was found for --one-click-scan")
        raise SystemExit(2)

    summary = scan_accounts(
        emails,
        session_file=args.session_file if len(emails) == 1 else "",
        workers=args.workers,
        proxy=args.proxy,
        timeout=args.refresh_timeout,
        workspace_check=False,
        switch_workspace_id="",
        fallback_workspace_ids=[],
        auto_switch_workspace=False,
        quota_relogin_on_401=bool(args.quota_auto_relogin),
        relogin_mode=args.scan_relogin_mode,
        deep_probe=bool(getattr(args, "scan_deep_probe", False)),
    )
    if summary.get("failed", 0):
        raise SystemExit(3)


def persist_one_click_sms_failure(data, json_path, email, result, ctx: OneClickCommandContext) -> None:
    now = int(time.time())
    refreshed = dict(data or {})
    refreshed["email"] = email
    refreshed["success"] = bool(refreshed.get("access_token"))
    refreshed["error"] = str(result.get("error") or "one_click_sms_failed")
    refreshed["refresh_token_status"] = str(refreshed.get("refresh_token_status") or "no_rt")
    refreshed["refresh_token_updated_at"] = now
    response = refreshed.get("response") if isinstance(refreshed.get("response"), dict) else {}
    response["codex_oauth"] = public_oauth_result(result)
    refreshed["response"] = response
    phone_attempt = result.get("phone_attempt") if isinstance(result.get("phone_attempt"), dict) else {}
    if phone_attempt:
        refreshed["phone"] = phone_attempt.get("phone", refreshed.get("phone", ""))
        response["phone_verification"] = phone_attempt
    if json_path:
        try:
            Path(json_path).write_text(json.dumps(refreshed, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"[!] Failed to update session JSON {json_path}: {exc}")
    ctx.upsert_account(refreshed, json_path=json_path)
