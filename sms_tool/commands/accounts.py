"""CLI boundary for account management commands (links, export, import, quota).

Storage/import/export domain modules own behavior; this module only translates
``argparse`` values into those domain calls.  ``AccountCommandContext`` keeps
the legacy CLI's replaceable hooks explicit so tests can keep patching
``sms_tool.cli`` symbols.
"""

from __future__ import annotations

import json
import logging
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .helpers import read_email_file, unique_emails

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountCommandContext:
    """Legacy CLI hooks required by account command orchestration."""

    list_paypal_accounts: Callable[..., list[dict[str, Any]]]
    get_paypal_url: Callable[[str], str]


def import_sessions_kwargs(args: Any, *, include_workers: bool = True) -> dict[str, Any]:
    """Shared ``import_account_session(s)`` keyword arguments built from args."""
    kwargs: dict[str, Any] = {
        "export_dir": args.codex_export_dir or "",
        "refresh": not args.no_session_refresh,
        "proxy": args.proxy,
        "timeout": args.refresh_timeout,
        "cpa_api_url": args.cpa_api_url or "",
        "cpa_api_token": args.cpa_api_token or "",
        "sub2api_url": args.sub2api_url or "",
        "sub2api_token": args.sub2api_token or "",
        "sub2api_email": args.sub2api_email or "",
        "sub2api_password": args.sub2api_password or "",
        "sub2api_group": args.sub2api_group or "",
        "sub2api_group_ids": args.sub2api_group_ids or "",
        "sub2api_proxy": args.sub2api_proxy or "",
        "sub2api_proxy_id": args.sub2api_proxy_id,
        "sub2api_priority": args.sub2api_priority,
        "sub2api_concurrency": args.sub2api_concurrency,
        "sub2api_auth_mode": getattr(args, "sub2api_auth_mode", "") or "",
        "sub2api_verify_after_import": getattr(args, "sub2api_verify_after_import", None),
    }
    if include_workers:
        kwargs["workers"] = args.workers
    return kwargs


def import_registered_accounts(args: Any, emails: list[str]) -> None:
    from ..import_targets import import_account_sessions

    emails = [str(email or "").strip() for email in emails if str(email or "").strip()]
    if not emails:
        print("[!] No successful registered account to import into CPA/SUB2API")
        return
    result = import_account_sessions(args.import_target, emails, **import_sessions_kwargs(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def print_paypal_links(email: str, ctx: AccountCommandContext) -> None:
    rows = ctx.list_paypal_accounts(email=email or "")
    if not rows:
        print("[*] No payment records found")
        return
    for row in rows:
        print(json.dumps({
            "email": row.get("email", ""),
            "payment_method": row.get("payment_method", ""),
            "paypal_url": row.get("paypal_url", ""),
            "paypal_status": row.get("paypal_status", ""),
            "refresh_token_status": row.get("refresh_token_status", ""),
            "json_path": row.get("json_path", ""),
        }, ensure_ascii=False))


def open_paypal_link(email: str, ctx: AccountCommandContext) -> None:
    email = (email or "").strip()
    if not email:
        print("[Error] --email is required with --open-paypal-link")
        return
    url = ctx.get_paypal_url(email)
    if not url:
        print(f"[Error] no PayPal URL found for {email}")
        return
    print(url)
    webbrowser.open(url)

def refresh_session(args: Any) -> None:
    from ..session_refresh import refresh_session as refresh_auth_session

    result = refresh_auth_session(
        email=args.email or "",
        session_file=args.session_file or "",
        timeout=args.refresh_timeout,
        proxy=args.proxy,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def export_codex_json(args: Any, ctx: AccountCommandContext) -> None:
    from ..codex_export import export_codex_session, export_codex_sessions

    emails = read_email_file(args.email_file)
    if args.email:
        emails = [(args.email or "").strip()]
    if emails:
        result = export_codex_sessions(
            emails,
            export_dir=args.codex_export_dir or "",
            workers=args.workers,
            refresh=not args.no_session_refresh,
            proxy=args.proxy,
            timeout=args.refresh_timeout,
        )
    elif args.session_file:
        result = export_codex_session(
            session_file=args.session_file,
            export_dir=args.codex_export_dir or "",
            refresh=not args.no_session_refresh,
            proxy=args.proxy,
            timeout=args.refresh_timeout,
        )
    else:
        rows = [
            row for row in ctx.list_paypal_accounts()
            if str(row.get("paypal_status") or "").strip().lower() == "completed"
        ]
        emails = [row.get("email", "") for row in rows if row.get("email")]
        result = export_codex_sessions(
            emails,
            export_dir=args.codex_export_dir or "",
            workers=args.workers,
            refresh=not args.no_session_refresh,
            proxy=args.proxy,
            timeout=args.refresh_timeout,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)


def importable_account_rows(ctx: AccountCommandContext) -> list[dict[str, Any]]:
    rows = []
    for row in ctx.list_paypal_accounts():
        email = str(row.get("email") or "").strip()
        access_token = str(row.get("access_token") or "").strip()
        if email and access_token:
            rows.append(row)
    return rows


def import_cpa(args: Any, ctx: AccountCommandContext) -> None:
    from ..import_targets import import_account_session, import_account_sessions

    emails = read_email_file(args.email_file)
    if args.email:
        emails = [(args.email or "").strip()]
    if emails:
        result = import_account_sessions(
            args.import_target,
            emails,
            **import_sessions_kwargs(args),
        )
    elif args.session_file:
        result = import_account_session(
            args.import_target,
            session_file=args.session_file,
            **import_sessions_kwargs(args, include_workers=False),
        )
    else:
        rows = importable_account_rows(ctx)
        emails = [row.get("email", "") for row in rows if row.get("email")]
        result = import_account_sessions(
            args.import_target,
            emails,
            **import_sessions_kwargs(args),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(3)
    try:
        from ..cpa_import import refresh_cpa_quota_statuses
        quota_emails = emails if emails else [str(item.get("email") or "") for item in (result.get("results") or []) if isinstance(item, dict)]
        if not quota_emails and isinstance(result, dict) and result.get("email"):
            quota_emails = [str(result.get("email") or "")]
        quota_result = refresh_cpa_quota_statuses(
            emails=quota_emails,
            workers=max(1, int(args.quota_workers or args.workers or 4)),
            api_url=args.cpa_api_url or "",
            api_token=args.cpa_api_token or "",
            timeout=max(5, int(args.refresh_timeout or 30)),
        )
        if quota_result.get("total", 0):
            print("[*] CPA quota refreshed after import:")
            print(json.dumps(quota_result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"[*] CPA quota refresh after import skipped: {exc}")


def check_promotion(args: Any, ctx: AccountCommandContext) -> None:
    from ..accounts.account_promotion import refresh_promotion_statuses

    emails = read_email_file(args.email_file)
    if args.email:
        emails = [(args.email or "").strip()]
    emails = unique_emails(emails)
    if not emails:
        emails = [str(row.get("email") or "").strip() for row in ctx.list_paypal_accounts()]
    result = refresh_promotion_statuses(
        emails=emails,
        workers=max(1, int(args.quota_workers or args.workers or 4)),
        proxy=args.proxy,
        proxy_pool=getattr(args, "proxy_pool", None),
        timeout=max(5, int(args.refresh_timeout or 20)),
    )
    from ..desktop_ipc import emit_result

    _print_promotion_summary(result)
    if bool(getattr(args, "desktop_ipc", False)):
        emit_result(result, enabled=True)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    logger.info(
        "promotion check finished: ok=%s success=%s/%s",
        bool(result.get("ok")), int(result.get("success") or 0), int(result.get("total") or 0),
    )
    if not result.get("ok"):
        raise SystemExit(3)


def refresh_cpa_quota(args: Any, ctx: AccountCommandContext) -> None:
    from ..accounts.account_recovery import refresh_local_quota_statuses
    from ..cpa_import import refresh_cpa_quota_statuses

    if bool(getattr(args, "mailbox_pool_repaired", False)):
        from ..mailbox_quarantine import mark_mailbox_pool_repaired

        mark_mailbox_pool_repaired()

    emails = read_email_file(args.email_file)
    if args.email:
        emails = [(args.email or "").strip()]
    emails = unique_emails(emails)
    if not emails:
        emails = [str(row.get("email") or "").strip() for row in ctx.list_paypal_accounts()]
    quota_mode = "local" if getattr(args, "refresh_local_quota", False) else str(getattr(args, "quota_mode", "local") or "local")
    if quota_mode == "cpa":
        result = refresh_cpa_quota_statuses(
            emails=emails,
            workers=max(1, int(args.quota_workers or args.workers or 4)),
            api_url=args.cpa_api_url or "",
            api_token=args.cpa_api_token or "",
            timeout=max(5, int(args.refresh_timeout or 30)),
        )
    else:
        result = refresh_local_quota_statuses(
            emails=emails,
            workers=max(1, int(args.quota_workers or args.workers or 4)),
            proxy=args.proxy,
            timeout=max(5, int(args.refresh_timeout or 30)),
            relogin_on_401=bool(getattr(args, "quota_auto_relogin", False)),
            relogin_timeout=max(30, int(getattr(args, "quota_relogin_timeout", 300) or 300)),
            relogin_mode=str(getattr(args, "scan_relogin_mode", "auto") or "auto"),
            batch_timeout=max(30, int(getattr(args, "quota_batch_timeout", 840) or 840)),
            account_timeout=max(30, int(getattr(args, "quota_account_timeout", 120) or 120)),
        )
        fallback_emails = [
            item.get("email")
            for item in result.get("results", [])
            if not item.get("ok")
            and str((item.get("probe") or {}).get("status") or "").strip().lower() != "account_deactivated"
            and not bool(
                (item.get("relogin") if isinstance(item.get("relogin"), dict) else {}).get("terminal")
            )
            and "account_deactivated" not in str(
                (item.get("relogin") if isinstance(item.get("relogin"), dict) else {}).get("error") or ""
            ).lower()
        ]
        if quota_mode == "auto" and fallback_emails:
            fallback = refresh_cpa_quota_statuses(
                emails=fallback_emails,
                workers=max(1, int(args.quota_workers or args.workers or 4)),
                api_url=args.cpa_api_url or "",
                api_token=args.cpa_api_token or "",
                timeout=max(5, int(args.refresh_timeout or 30)),
            )
            result["fallback_cpa"] = fallback
            result["ok"] = bool(fallback.get("ok"))
    from ..desktop_ipc import emit_result

    emit_result(result, enabled=bool(getattr(args, "desktop_ipc", False)))
    _print_quota_summary(result)
    if not result.get("ok"):
        raise SystemExit(3)


def _print_promotion_summary(result):
    """Staged operator lines for 账号优惠检测: one summary + failures only.

    Mirror of :func:`_print_quota_summary`. Without it the promotion check
    printed nothing between the task-start line and the folded result envelope,
    because every per-account detail lived only in the structured payload.
    """
    results = result.get("results") if isinstance(result.get("results"), list) else []
    rows = [item for item in results if isinstance(item, dict)]
    ok = sum(1 for item in rows if item.get("ok"))
    print(f"[*] 优惠检测完成：共 {len(rows)} 个账号，检测成功 {ok}，失败 {len(rows) - ok}")
    logger.info("promotion check finished: ok=%s/%s", ok, len(rows))
    for item in rows:
        if item.get("ok"):
            continue
        email = str(item.get("email") or "").strip()
        probe = item.get("probe") if isinstance(item.get("probe"), dict) else {}
        raw_reason = str(item.get("promotion_status") or probe.get("error") or item.get("error") or "failed")
        print(f"[!] {email}: {_probe_reason_label(raw_reason)}")
        logger.warning("promotion check failed for %s: %s", email, raw_reason)


def _print_quota_summary(result):
    """One staged summary line plus per-account failures for the log panel.

    The machine-readable result already went out through ``emit_result``;
    stdout only needs the operator story, not the full JSON dump.
    """
    results = result.get("results") if isinstance(result.get("results"), list) else []
    rows = [item for item in results if isinstance(item, dict)]
    ok = sum(1 for item in rows if item.get("ok"))
    deactivated = sum(
        1
        for item in rows
        if str((item.get("probe") or {}).get("status") or "").strip().lower()
        == "account_deactivated"
    )
    other_failed = len(rows) - ok - deactivated
    print(
        f"[*] 测活完成：共 {len(rows)} 个账号，正常 {ok}，掉号 {deactivated}，其他失败 {other_failed}"
    )
    logger.info("liveness check finished: ok=%s/%s deactivated=%s", ok, len(rows), deactivated)
    for item in rows:
        email = str(item.get("email") or "").strip()
        relogin = item.get("relogin") if isinstance(item.get("relogin"), dict) else {}
        if item.get("ok"):
            # A silent 401 -> relogin -> 200 recovery is otherwise invisible in
            # the panel; surface it so operators know the token rotated.
            if relogin.get("ok"):
                print(f"[+] {email}: 重登成功，令牌已刷新")
                logger.info("liveness relogin recovered %s", email)
            continue
        probe = item.get("probe") if isinstance(item.get("probe"), dict) else {}
        raw_reason = str(probe.get("status") or probe.get("error") or relogin.get("error") or item.get("error") or "failed")
        reason = _probe_reason_label(raw_reason)
        note = _relogin_panel_note(relogin)
        dropped = "（已标记掉号：令牌吊销且无可恢复凭据）" if str(probe.get("dropped") or "") == "token_revoked" else ""
        line = f"{reason}{note}{dropped}"
        print(f"[!] {email}: {line}")
        logger.warning("liveness check failed for %s: %s", email, line)


# Ordered longest-prefix-first: "curl: (28) ..." must win over a bare
# "timed out" so the operator sees 网络超时 and not a raw curl string.
_PROBE_REASON_LABELS = (
    ("account_deactivated", "账号已注销"),
    ("account_deatived", "账号已注销"),
    # 优惠检测 already hands us a Chinese badge (AT失效 / 缺少AT / 检测失败);
    # match those before the English needles so they do not fall through to the
    # "检测失败（...）" tail.
    ("at失效", "AT 失效（HTTP 401）"),
    ("缺少at", "缺少 Access Token"),
    ("检测失败", "检测失败"),
    ("token_invalid", "AT 失效（HTTP 401）"),
    ("health_timeout", "探测超时"),
    ("scan_failed", "探测失败"),
    ("scan_cancelled", "扫描已取消"),
    ("mailbox_transport", "邮箱链路失败"),
    ("mailbox_auth_invalid", "邮箱授权失效"),
    ("mailbox_pool_repair_required", "邮箱池熔断中"),
    ("remotedisconnected", "连接被远端断开"),
    ("proxyerror", "代理连接失败"),
    ("curl: (28)", "网络超时"),
    ("curl: (7)", "无法连接代理"),
    ("curl: (35)", "TLS 握手失败"),
    ("curl: (56)", "连接被中断"),
    ("timed out", "网络超时"),
    ("timeout", "网络超时"),
    ("unauthorized", "AT 失效（HTTP 401）"),
    ("401", "AT 失效（HTTP 401）"),
)


def _probe_reason_label(reason: str) -> str:
    """Chinese operator label for a raw probe/relogin reason string.

    The raw values (``token_invalid``, ``curl: (28) timed out``, ...) used to be
    printed verbatim, which is what filled the panel with English noise. The
    structured result dialog still carries the untranslated value, so nothing
    is lost for diagnosis.
    """
    text = str(reason or "").strip()
    if not text:
        return "检测失败"
    lowered = text.lower()
    for needle, label in _PROBE_REASON_LABELS:
        if needle in lowered:
            return label
    if lowered.startswith("http "):
        return f"HTTP {text.split()[1]}" if len(text.split()) > 1 else text
    # Unknown reasons keep a short raw tail; truncating avoids dumping a
    # Cloudflare HTML page into the panel.
    return f"检测失败（{text[:60]}）"


def _relogin_panel_note(relogin: dict) -> str:
    """Chinese operator note for a failed/skipped relogin on a 401 probe.

    Without this the relogin gate result only exists inside the structured
    result popup, which made a globally breaker-disabled recovery look like an
    unexplained mass token_invalid.
    """
    if not relogin or relogin.get("ok"):
        return ""
    error = str(relogin.get("error") or "")
    mode = str(relogin.get("mode") or "")
    if relogin.get("terminal") or "account_deactivated" in error:
        return " → 重登确认账号已注销，已标记掉号"
    if error == "mailbox_pool_repair_required" or mode == "disabled":
        return " → 重登未执行：邮箱池熔断中（修复后用 --mailbox-pool-repaired 确认）"
    if mode == "cooldown" or "cooldown" in error:
        return " → 重登跳过：冷却中"
    if mode == "concurrency_limited":
        return " → 重登跳过：并发槽已满"
    if error:
        return f" → 重登失败：{error[:80]}"
    return " → 重登未完成"


def quota_usage(args: Any) -> None:
    """Fetch wham/usage 5h/7d quota for a single account and return structured JSON."""
    from ..accounts.account_liveness import probe_account_liveness
    from ..storage import get_account_record

    email = (getattr(args, "email", None) or "").strip()
    if not email:
        print(json.dumps({"ok": False, "error": "missing --email"}))
        raise SystemExit(1)

    account = get_account_record(email)
    if not account:
        print(json.dumps({"ok": False, "error": "account_not_found", "email": email}))
        raise SystemExit(1)

    proxy = getattr(args, "proxy", None) or None
    timeout = max(5, int(getattr(args, "refresh_timeout", None) or 30))
    probe = probe_account_liveness(account, proxy=proxy, timeout=timeout)
    result = {
        "ok": probe.get("ok", False),
        "email": email,
        "status": probe.get("status", "unknown"),
        "quota_status": probe.get("quota_status", ""),
        "wham_usage": probe.get("wham_usage"),
        "status_code": probe.get("status_code"),
        "error": probe.get("error", ""),
    }
    from ..desktop_ipc import emit_result

    emit_result(result, enabled=bool(getattr(args, "desktop_ipc", False)))
    if not result["ok"]:
        raise SystemExit(3)


def convert_session_json(args: Any) -> None:
    from ..session_converter import convert_json_file

    result = convert_json_file(args.convert_session_json, fmt=args.convert_format)
    output_text = result.get("outputText") or ""
    if args.convert_output:
        target = Path(args.convert_output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output_text, encoding="utf-8")
        print(json.dumps({
            "ok": bool(result.get("converted")),
            "format": args.convert_format,
            "converted": len(result.get("converted") or []),
            "skipped": result.get("skipped") or [],
            "output": str(target),
        }, ensure_ascii=False, indent=2))
    else:
        print(output_text)
        if result.get("skipped"):
            print(json.dumps({"skipped": result.get("skipped")}, ensure_ascii=False, indent=2), file=sys.stderr)
    if not result.get("converted"):
        raise SystemExit(3)
