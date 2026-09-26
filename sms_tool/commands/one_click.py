"""CLI boundary for one-click account commands (--one-click-sms, --one-click-scan).

``codex_oauth``/``account_scan``/``phone_reuse`` own protocol behavior; this
module only resolves targets from argparse values and orchestrates workers.
``OneClickCommandContext`` keeps the legacy CLI's replaceable hooks explicit.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any

from .helpers import read_email_file, unique_emails
from ..sanitizer import account_reference, sanitize_text

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
    persist_failure: Callable[..., dict[str, Any]]


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
        print("[Error] --one-click-sms requires a phone pool. Configure the selected provider's key, e.g. phone_reuse.smsbower.api_key / SMSBOWER_API_KEY.")
        raise SystemExit(2)
    phone_pool.reset_exhausted_slots()
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
        stage = "session_load"
        try:
            _emit_one_click_event(
                run_id=account_run_id,
                batch_id=batch_id,
                stage=stage,
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
            stage = "oauth_refresh"
            _emit_one_click_event(
                run_id=account_run_id,
                batch_id=batch_id,
                stage=stage,
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
            if not isinstance(result, dict):
                raise TypeError("unexpected OAuth result")
        except Exception:
            # The remote outcome can be ambiguous after a worker exception.
            # Never expose the exception text or automatically retry this account.
            logger.warning("one-click SMS worker failed: account=%s stage=%s", account_reference(email), stage)
            result = {
                "email": email, "ok": False, "batch_status": "unknown",
                "error_code": "one_click_worker_exception", "error_stage": stage,
            }
            _emit_one_click_event(
                run_id=account_run_id,
                batch_id=batch_id,
                stage="unknown",
                status="unknown",
                email=email,
                detail="接码结果未知，已停止派发新账号",
                account_terminal=True,
                failure_class="internal",
            )
            return index, result, True

        persisted = bool(
            result.get("persistence", {}).get("persisted", result.get("persisted", True))
            if isinstance(result.get("persistence"), dict)
            else result.get("persisted", True)
        )
        fatal = False
        if result.get("ok"):
            result["batch_status"] = "success"
            if persisted:
                output_line = f"[OK] {email} RT stored: {result.get('refresh_token_status', '')}"
                logger.info("one-click SMS %s: RT stored (%s)", account_reference(email), result.get("refresh_token_status", ""))
            else:
                output_line = f"[FAIL] {email}: OAuth completed, local save failed"
                logger.warning("one-click SMS local save failed: account=%s", account_reference(email))
            print(output_line)
            _emit_one_click_event(
                run_id=account_run_id,
                batch_id=batch_id,
                stage="completed" if persisted else "failed",
                status="success" if persisted else "failed",
                email=email,
                detail="RT 已保存" if persisted else "OAuth 已完成，但本地保存失败",
                account_terminal=True,
                failure_class="" if persisted else "persistence",
            )
        else:
            result["batch_status"] = "failed"
            error = sanitize_text(result.get("error") or "unknown")[:160]
            result["error"] = error
            print(f"[FAIL] {email}: {error}")
            logger.warning("one-click SMS %s failed: %s", account_reference(email), error)
            try:
                result["persistence"] = ctx.persist_failure(data, json_path, email, result)
                persisted = bool(result["persistence"].get("persisted"))
            except Exception:
                fatal = True
                persisted = False
                result["persistence"] = {"persisted": False, "error_code": "failure_persistence_exception"}
                logger.warning("one-click SMS failure save failed: account=%s", account_reference(email))
            _emit_one_click_event(
                run_id=account_run_id,
                batch_id=batch_id,
                stage="failed",
                status="failed",
                email=email,
                detail=error,
                account_terminal=True,
                failure_class="persistence" if fatal else str(result.get("failure_class") or "unknown"),
            )
        result.setdefault("email", email)
        result["persisted"] = persisted
        result.pop("tokens", None)
        return index, result, fatal

    ordered = [None] * len(emails)
    stopped = False
    if workers <= 1:
        for index, email in enumerate(emails):
            i, result, stopped = _run_one(index, email)
            ordered[i] = result
            if stopped:
                break
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            next_index = 0
            pending = {}
            while next_index < min(workers, len(emails)):
                pending[executor.submit(_run_one, next_index, emails[next_index])] = next_index
                next_index += 1
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    index = pending.pop(future)
                    try:
                        i, result, fatal = future.result()
                    except Exception:
                        # Defensive fallback for failures outside the worker's
                        # own exception guard; do not lose its account slot.
                        i, result, fatal = index, {
                            "email": emails[index], "ok": False, "batch_status": "unknown",
                            "error_code": "one_click_worker_exception",
                        }, True
                        _emit_one_click_event(
                            run_id=f"{batch_id}:{account_reference(emails[index])}",
                            batch_id=batch_id, stage="unknown", status="unknown",
                            email=emails[index], detail="接码结果未知，已停止派发新账号",
                            account_terminal=True, failure_class="internal",
                        )
                    ordered[i] = result
                    stopped = stopped or fatal
                if not stopped:
                    while next_index < len(emails) and len(pending) < workers:
                        pending[executor.submit(_run_one, next_index, emails[next_index])] = next_index
                        next_index += 1

    for index, result in enumerate(ordered):
        if result is not None:
            continue
        email = emails[index]
        ordered[index] = {"email": email, "ok": False, "batch_status": "skipped", "error_code": "batch_stopped"}
        _emit_one_click_event(
            run_id=f"{batch_id}:{account_reference(email)}",
            batch_id=batch_id, stage="skipped", status="skipped",
            email=email, detail="前序异常，未执行",
            account_terminal=True,
        )

    results = ordered
    ok_count = sum(1 for result in results if result.get("ok"))
    failed_count = sum(1 for result in results if result.get("batch_status") == "failed")
    unknown_count = sum(1 for result in results if result.get("batch_status") == "unknown")
    skipped_count = sum(1 for result in results if result.get("batch_status") == "skipped")
    persist_failed = sum(
        1 for result in results
        if result.get("batch_status") not in {"unknown", "skipped"} and result.get("persisted") is False
    )
    batch_ok = ok_count == len(emails) and not persist_failed
    logger.info("one-click SMS RT refresh finished: ok=%d/%d", ok_count, len(emails))
    summary = {
        "ok": batch_ok,
        "total": len(emails),
        "attempted": len(emails) - skipped_count,
        "success": ok_count,
        "failed": failed_count,
        "unknown": unknown_count,
        "skipped": skipped_count,
        "persist_failed": persist_failed,
        "results": results,
    }
    _emit_one_click_event(
        run_id=run_id,
        batch_id=batch_id,
        stage="batch_completed",
        status="completed" if batch_ok else "failed",
        detail=f"完成 {ok_count}/{len(emails)}，保存失败 {persist_failed}，未知 {unknown_count}，跳过 {skipped_count}",
        total=len(emails),
        batch_terminal=True,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not batch_ok:
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
    if summary.get("failed", 0) or summary.get("persist_failed", 0):
        raise SystemExit(3)
